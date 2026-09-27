#!/usr/bin/env python3
"""Bot săn giftcode Telegram — BROWSER-ONLY.

Browser automation via Edge/CDP is the only submission route.

The bot never submits codes through HTTP APIs and does not use CAPTCHA-solving APIs.
Site-specific selectors and result handling live in browser_engine.py.
"""
from __future__ import annotations

import asyncio
import csv
import gc
import hashlib
import json
import os
import random
import re
import shutil
import socket
import tempfile
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

from telethon import TelegramClient, events
from telethon.utils import get_peer_id
from telethon.errors.rpcerrorlist import AuthKeyDuplicatedError
from telethon.network import ConnectionTcpAbridged
from telethon.tl.types import DocumentAttributeVideo, MessageEntitySpoiler, MessageMediaDocument

from config import Config
from logger_setup import logger, reset_log_context, set_log_context
from code_validator import CodeValidator
from image_code_extractor import (
    _OCR_EXECUTOR,
    VIDEO_FORMATS,
    extract_frames_from_video,
    get_image_extractor,
    shutdown_ocr_executor,
)
from database import init_database
from durable_inbox import DurableInbox
from monitoring import init_monitoring, stop_monitoring
from features import (
    BOT_VERSION,
    command_registry,
    get_shutdown_handler,
    print_version_info,
    register_default_commands,
    setup_admin_commands,
)
from timing import RequestTimer, get_current_timer, reset_current_timer, set_current_timer
from media_download_manager import MediaDownloadManager, cleanup_stale_files
from browser_adapter import BrowserEngineAdapter
from submission_outcomes import finalize_outcome
from queue_manager import get_queue_manager, init_queue_manager
from dashboard import (
    disable_console_logging,
    report_batch_submit,
    report_download_finished,
    report_download_started,
    start_dashboard,
    stop_dashboard,
    update_dashboard,
)

# ═══ Browser engine ═══
# Browser engine is mandatory: submissions cannot use an HTTP/API fallback.
try:
    import browser_engine

    _BROWSER_ENGINE_OK = True
except Exception as _browser_import_err:  # pragma: no cover
    browser_engine = None
    _BROWSER_ENGINE_OK = False
    logger.error(
        f"❌ [Browser] Không load được browser_engine.py ({_browser_import_err}) — "
        f"Bot không thể submit khi browser_engine lỗi. "
        f"Kiểm tra: pip install playwright --break-system-packages"
    )

# ✅ Executor riêng: tránh mặc định event loop share với OCR/DB/media.
# Nếu để None, nhiều task CPU-bound/IO-bound từ các domain cùng lúc sẽ chặn nhau.
_MEDIA_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="media-io")
_DB_EXECUTOR = ThreadPoolExecutor(max_workers=8, thread_name_prefix="db-io")
# ✅ Tách đường ghi ingress khỏi các thao tác state/maintenance của inbox.
# enqueue tin mới chỉ dùng pool 2 thread; claim/retry/complete/maintenance
# dùng pool state 4 thread nên retry-storm không làm chậm message mới.
_INBOX_INGRESS_EXECUTOR = ThreadPoolExecutor(
    max_workers=2,
    thread_name_prefix="inbox-ingress",
)
_INBOX_STATE_EXECUTOR = ThreadPoolExecutor(
    max_workers=6,
    thread_name_prefix="inbox-state",
)
# Alias tạm thời để các call-site state hiện hữu giữ nguyên và dễ rà soát.
_INBOX_EXECUTOR = _INBOX_STATE_EXECUTOR

_SITE_LOG_LABELS = {
    "xx88code.com": "XX88",
    "rr88code.com": "RR88",
    "gg88live.tv": "GG88",
    "livemm88.net": "MM88",
    "o8code.com": "O8",
    "tangquaqq88.com": "QQ88",
    "hi88-freecode.pages.dev": "HI88",
}
_PROMO_LABEL_INLINE_RE = re.compile(r"(?im)^\s*M(?:Ã|A|4){1,2}\s*[:\-]\s*[A-Za-z0-9_]{2,14}\s*$")
_PROMO_LABEL_ONLY_RE = re.compile(r"(?im)^\s*M(?:Ã|A|4){1,2}\s*[:\-]?\s*$")
_XX88_BIGWIN_RE = re.compile(r"^[A-Za-z0-9](\*[A-Za-z0-9]){4,}$")
_XX88_OCR_BANNER_TOKENS = frozenset(
    {
        "THUMAY", "TOPWIN", "MEGA", "LIVE", "MEGALIVE", "TANG", "QUA",
        "PHAT", "CODE", "FREE", "GIFTCODE", "GIFT", "BONUS", "PROMO",
        "PROMOTION", "KHUYENMAI", "VIP", "GAME", "WIN", "WINNER",
    }
)
_QQ88_OCR_NOISE_RE = re.compile(
    r"(?:SCATTER|OLYMPUS|ANUBIS|MAHJONG|AZTEC|OFAKIND|"
    r"DCHOIDPHTTI|DCHOI.*PHTTI|QUAY(?:MINPH|MIENPHI)|"
    r"WINMULTIPLIER|BETVND|PROFITVND|BALANCEVND|"
    r"BET(?:SIZE|LEVEL)\d*|ROUND\d+|\d+WAYS)",
    re.IGNORECASE,
)
_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_WWW_RE = re.compile(r"www\.\S+", re.IGNORECASE)
_TME_RE = re.compile(r"t\.me/\S+", re.IGNORECASE)
_DOMAIN_RE = re.compile(r"\b[a-zA-Z0-9.-]+\.(?:com|net|org|vn|app|info)\b", re.IGNORECASE)
_HASHTAG_RE = re.compile(r"#\S+")
_CURRENCY_RE = re.compile(r"\d[\d.,]{3,}\s*(?:VND|VNĐ)\b", re.IGNORECASE)
_CODE_MARKER_RE = re.compile(
    r"NHẬN\s+CODE(?:\s+NGAY)?|NHAN\s+CODE(?:\s+NGAY)?|"
    r"NHẬP\s+CODE|NHAP\s+CODE|PHÁT\s+CODE|PHAT\s+CODE|"
    r"CODE\s+FREE|FREE\s+CODE|GIFT\s*CODE|GIFTCODE|"
    r"TẶNG\s+CODE|TANG\s+CODE",
    re.IGNORECASE,
)
_NOISE_RE = re.compile(
    r"HTTP|WWW|\.COM|FACEBOOK|TELEGRAM|TIKTOK|ZALO|CSKH|BOT|CHECK\s+LINK|LINK",
    re.IGNORECASE,
)
_TOKEN_RE = re.compile(
    rf"[A-Za-z0-9{re.escape(getattr(Config, 'SPECIAL_CODE_CHARS_30', ''))}]"
    rf"{{{getattr(Config, 'CODE_MIN_LENGTH', 6)},{getattr(Config, 'CODE_MAX_LENGTH', 15) + 30}}}"
)
_ALNUM_TOKEN_RE = re.compile(r"[a-zA-Z0-9]{6,15}")
_KJC_LABEL_RE = re.compile(
    r"(?:NHẬN\s+CODE|NHAN\s+CODE|CODE)\s*(?:NGAY|NGÀY)?\s*[:\-–—]?\s*"
    r"(MM88|RR88|XX88|GG88)",
    re.IGNORECASE,
)
_KJC_SITE_RE = {
    key: re.compile(rf"\b{re.escape(key)}\b", re.IGNORECASE)
    for key in ("MM88", "RR88", "XX88", "GG88")
}


# ═══════════════════════════════════════════════════════════════
# BROWSER-ONLY SUBMISSION STATE
# ═══════════════════════════════════════════════════════════════
_browser_adapter = None
_durable_inbox = None
_inbox_drain_task = None
_inbox_wakeup = None
_ingress_tasks: set[asyncio.Task] = set()
_INGRESS_CONCURRENCY = asyncio.Semaphore(
    max(1, int(getattr(Config, "MAX_CONCURRENT_INGRESS_TASKS", 128)))
)
_inbox_message_cache: dict[int, object] = {}
_INBOX_MSG_CACHE_MAX = 4000
# Chặn các event NewMessage/MessageEdited trùng nhau đang đồng thời chờ
# SQLite. Nếu không có lớp này, nhiều callback cùng một message_id có thể
# đều INSERT OR IGNORE thành công về mặt logic nhưng cùng enqueue một row_id;
# worker sau đó phải claim các queue item dư thừa (một round-trip SQLite vô ích
# cho mỗi item). Đây là single-flight ở biên ingress, không thay thế durable
# dedup trong SQLite.
_ingress_inflight: set[tuple] = set()


def schedule_tracked_task(coro, task_set: set, name: str) -> asyncio.Task:
    """Create a tracked background task without an extra deployment module."""
    task = asyncio.create_task(coro, name=name)
    task_set.add(task)

    def _finish(done_task: asyncio.Task) -> None:
        task_set.discard(done_task)
        if done_task.cancelled():
            return
        try:
            error = done_task.exception()
        except asyncio.CancelledError:
            return
        if error is not None:
            logger.error("❌ Telegram ingress task lỗi: %s", error)

    task.add_done_callback(_finish)
    return task


def _get_ingress_semaphore() -> asyncio.Semaphore:
    return _INGRESS_CONCURRENCY

def _get_browser_adapter():
    global _browser_adapter
    if _browser_adapter is None:
        if not _BROWSER_ENGINE_OK:
            raise RuntimeError("browser_engine không sẵn sàng")
        _browser_adapter = BrowserEngineAdapter(browser_engine)
    return _browser_adapter


# ═══════════════════════════════════════════════════════════════
# BOT STATE
# ═══════════════════════════════════════════════════════════════
class BotState:
    def __init__(self):
        self.is_running = True
        self._site_code_seen: dict = {}
        self.handler_registered = False
        self._last_cleanup_time = time.time()
        self.bg_tasks: set = set()
        self._channel_account_index: dict = {}
        self._domain_account_cursor: dict[str, int] = {}
        self._daily_used: set = set()
        self._daily_date: str = datetime.now().strftime("%Y-%m-%d")
        self._inflight_codes: set = set()
        self._inflight_accounts: set = set()
        self._processed_message_hashes: dict = {}
        self._inbox_enqueued_ids: set[int] = set()
        self.last_raw_update_at: float | None = None
        self.last_accepted_message_at: float | None = None
        self.raw_update_count: int = 0
        self.accepted_message_count: int = 0
        self.event_loop_lag_ms: float = 0.0


bot_state = BotState()
BOT_START_TIME: datetime = datetime.now(timezone.utc)

_media_download_manager = MediaDownloadManager(
    max_concurrent=getattr(Config, "MAX_CONCURRENT_MEDIA_DOWNLOADS", 4),
    retries=getattr(Config, "MEDIA_DOWNLOAD_RETRIES", 1),
    retry_delay=getattr(Config, "MEDIA_DOWNLOAD_RETRY_DELAY", 0.5),
    max_size_bytes=(int(getattr(Config, "MEDIA_DOWNLOAD_MAX_SIZE_MB", 200)) * 1024 * 1024 if getattr(Config, "MEDIA_DOWNLOAD_MAX_SIZE_MB", 200) else None),
    fast_download=getattr(Config, "FAST_TELEGRAM_DOWNLOAD", False),
    fast_min_bytes=getattr(Config, "FAST_DOWNLOAD_MIN_BYTES", 4 * 1024 * 1024),
    fast_workers=getattr(Config, "FAST_DOWNLOAD_WORKERS", 6),
    fast_chunk_kb=getattr(Config, "FAST_DOWNLOAD_CHUNK_KB", 512),
    fast_max_connections=getattr(Config, "FAST_DOWNLOAD_MAX_CONNECTIONS", 8),
)

client = None
if Config.API_ID and Config.API_HASH.strip():
    client = TelegramClient(
        Config.SESSION_NAME,
        Config.API_ID,
        Config.API_HASH,
        device_model="Desktop Bot",
        system_version="Windows 10",
        app_version="1.0",
        connection=ConnectionTcpAbridged,
        connection_retries=getattr(Config, "TELEGRAM_CONNECTION_RETRIES", 0),
        retry_delay=1,
        auto_reconnect=getattr(Config, "TELEGRAM_AUTO_RECONNECT", False),
        # Telethon 1.35.0's timeout parameter is the socket/connect timeout;
        # per-operation timeouts are applied explicitly with asyncio.wait_for().
        timeout=getattr(Config, "TELEGRAM_CONNECT_TIMEOUT", 15.0),
        use_ipv6=False,
        flood_sleep_threshold=60,
        receive_updates=True,
        sequential_updates=False,
    )

_submit_success_notify_lock = asyncio.Lock()
_submit_success_notify_keys: set[tuple[str, str, str]] = set()


async def send_submit_success_notification(
    *, domain: str, user: str, code: str, has_points: bool, result_message: str = ""
) -> bool:
    """Notify one Telegram chat after a successful HI88/QQ88 submit.

    This uses the independent Bot API alert channel and runs the HTTP request
    off the event loop. The in-memory key prevents duplicate notifications
    when a success result is observed again during recovery/retry. A failed
    delivery releases the key so a later successful retry can notify again.
    """
    allowed = {"hi88-freecode.pages.dev", "tangquaqq88.com"}
    if domain not in allowed or not getattr(Config, "SUBMIT_SUCCESS_NOTIFY", True):
        return False
    token = str(getattr(Config, "ALERT_BOT_TOKEN", "") or "").strip()
    chat_id = getattr(Config, "SUBMIT_SUCCESS_CHAT_ID", 0)
    if not token or not chat_id:
        logger.warning("⚠️ [SubmitNotify] Thiếu ALERT_BOT_TOKEN hoặc SUBMIT_SUCCESS_CHAT_ID")
        return False

    clean_code = str(code or "").strip().upper()
    key = (domain, str(user or "").strip(), clean_code)
    async with _submit_success_notify_lock:
        if key in _submit_success_notify_keys:
            return True
        _submit_success_notify_keys.add(key)

    site_label = "HI88" if domain == "hi88-freecode.pages.dev" else "QQ88"
    detail = str(result_message or "").replace("\r", " ").replace("\n", " ").strip()
    if len(detail) > 180:
        detail = detail[:177] + "..."
    text = "\n".join(
        (
            f"✅ SUBMIT THÀNH CÔNG {site_label}",
            f"• Account: {user}",
            f"• Code: {clean_code}",
            f"• Điểm: {'Có' if has_points else 'Không/không xác định'}",
            f"• Domain: {domain}",
            *( [f"• Kết quả: {detail}"] if detail else [] ),
            f"• Thời gian: {datetime.now().astimezone().isoformat(timespec='seconds')}",
        )
    )
    endpoint = f"https://api.telegram.org/bot{token}/sendMessage"
    body = urlencode({
        "chat_id": str(chat_id),
        "text": text,
        "disable_web_page_preview": "true",
    }).encode("utf-8")
    request = Request(
        endpoint,
        data=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "giftcode-bot-submit-notify/1.0",
        },
        method="POST",
    )
    timeout = max(1.0, float(getattr(Config, "ALERT_HTTP_TIMEOUT", 10.0)))

    def _post() -> int:
        with urlopen(request, timeout=timeout) as response:
            response.read()
            return int(response.status)

    try:
        status = await asyncio.to_thread(_post)
        if 200 <= status < 300:
            logger.info("📣 [SubmitNotify] Đã gửi %s success tới chat_id=%s", site_label, chat_id)
            return True
    except Exception as exc:
        logger.warning("⚠️ [SubmitNotify] Gửi %s thất bại: %s", site_label, exc)
    async with _submit_success_notify_lock:
        _submit_success_notify_keys.discard(key)
    return False


async def send_auth_key_alert(exc: BaseException) -> bool:
    """Send a fatal Telegram-session alert through an independent Bot API bot.

    This deliberately does not use the Telethon ``client``: when
    AuthKeyDuplicatedError occurs, that client is the component that is no
    longer safe to reconnect.  The HTTP request runs in a worker thread so a
    slow/unreachable Telegram Bot API endpoint does not block the event loop.

    Returns:
        ``True`` when Telegram Bot API accepts the request (HTTP 2xx), else
        ``False``.  All failures are logged without exposing the bot token.
    """
    token = str(getattr(Config, "ALERT_BOT_TOKEN", "") or "").strip()
    chat_id = getattr(Config, "ALERT_CHAT_ID", 0)
    timeout = max(
        1.0,
        float(getattr(Config, "ALERT_HTTP_TIMEOUT", 10.0)),
    )

    if not token:
        logger.critical(
            "❌ [Alert] Thiếu ALERT_BOT_TOKEN — không thể gửi cảnh báo "
            "AuthKeyDuplicated"
        )
        return False

    if not chat_id:
        logger.critical(
            "❌ [Alert] Thiếu ALERT_CHAT_ID — không thể gửi cảnh báo "
            "AuthKeyDuplicated"
        )
        return False

    session_name = os.path.basename(str(getattr(Config, "SESSION_NAME", "")))
    error_text = str(exc).replace("\r", " ").replace("\n", " ").strip()
    if len(error_text) > 500:
        error_text = error_text[:497] + "..."

    text = "\n".join(
        (
            "🚨 BOT DỪNG KHẨN CẤP",
            "Lỗi: Telegram AuthKeyDuplicated",
            "",
            f"• Session: {session_name or '(unknown)' }",
            f"• Host: {socket.gethostname()}",
            f"• PID: {os.getpid()}",
            f"• Thời gian: {datetime.now().astimezone().isoformat()}",
            f"• Chi tiết: {error_text}",
            "",
            "Bot đã dừng retry tự động.",
            "Kiểm tra instance/IP đang dùng chung session, sau đó tạo "
            "session mới nếu cần.",
        )
    )

    endpoint = f"https://api.telegram.org/bot{token}/sendMessage"
    body = urlencode(
        {
            "chat_id": str(chat_id),
            "text": text,
            "disable_web_page_preview": "true",
        }
    ).encode("utf-8")

    request = Request(
        endpoint,
        data=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "giftcode-bot-alert/1.0",
        },
        method="POST",
    )

    def _post_alert() -> tuple[int, bytes]:
        with urlopen(request, timeout=timeout) as response:
            return int(response.status), response.read()

    try:
        status_code, response_body = await asyncio.to_thread(_post_alert)
        if 200 <= status_code < 300:
            logger.critical(
                "🚨 [Alert] Đã gửi cảnh báo AuthKeyDuplicated qua Bot API "
                "độc lập tới chat_id=%s",
                chat_id,
            )
            return True

        logger.critical(
            "❌ [Alert] Bot API trả HTTP %s: %s",
            status_code,
            response_body[:300].decode("utf-8", errors="replace"),
        )
        return False

    except Exception as alert_error:
        # Không log endpoint vì endpoint chứa bot token.
        logger.critical(
            "❌ [Alert] Gửi cảnh báo AuthKeyDuplicated thất bại: %s",
            alert_error,
        )
        return False


def backup_corrupt_telegram_session() -> dict[str, object]:
    """Backup the Telethon session and optionally remove the broken files.

    The session is never deleted before all existing session files have been
    copied successfully.  Besides the main SQLite file, SQLite sidecars are
    included when present (``-journal``, ``-wal`` and ``-shm``).

    Returns a small status dictionary for logging and alert text.  The
    operation is synchronous by design and should be called from the fatal
    error path before shutdown; it touches only local files.
    """
    enabled = bool(getattr(Config, "SESSION_AUTO_BACKUP", True))
    delete_after_backup = bool(
        getattr(Config, "SESSION_DELETE_AFTER_BACKUP", False)
    )
    session_value = str(getattr(Config, "SESSION_NAME", "") or "").strip()

    result: dict[str, object] = {
        "enabled": enabled,
        "deleted": False,
        "backup_dir": "",
        "files": [],
        "error": "",
    }

    if not enabled:
        logger.warning("⚠️ [Session] SESSION_AUTO_BACKUP=false — bỏ qua backup")
        return result

    if not session_value:
        result["error"] = "SESSION_NAME is empty"
        logger.error("❌ [Session] Không backup được: SESSION_NAME rỗng")
        return result

    session_path = Path(session_value)
    if session_path.suffix.lower() != ".session":
        session_path = session_path.with_name(session_path.name + ".session")

    source_files = [
        session_path,
        Path(str(session_path) + "-journal"),
        Path(str(session_path) + "-wal"),
        Path(str(session_path) + "-shm"),
    ]
    existing_files = [path for path in source_files if path.is_file()]

    if not existing_files:
        result["error"] = f"session files not found: {session_path}"
        logger.error(
            "❌ [Session] Không tìm thấy file session để backup: %s",
            session_path,
        )
        return result

    backup_root = Path(
        getattr(Config, "SESSION_BACKUP_DIR", "backups/sessions")
    )
    stamp = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S_%f")[:-3]
    backup_dir = backup_root / f"{session_path.stem}_{stamp}"
    result["backup_dir"] = str(backup_dir)

    copied_files: list[Path] = []
    try:
        backup_dir.mkdir(parents=True, exist_ok=False)

        for source in existing_files:
            destination = backup_dir / source.name
            shutil.copy2(source, destination)
            copied_files.append(source)

        result["files"] = [str(path) for path in copied_files]
        logger.warning(
            "📦 [Session] Đã backup %s file vào %s",
            len(copied_files),
            backup_dir,
        )

        # Xóa là opt-in. Chỉ xóa đúng các file đã copy thành công.
        if delete_after_backup:
            for source in copied_files:
                source.unlink()
            result["deleted"] = True
            logger.warning(
                "🗑️ [Session] Đã xóa %s file session sau khi backup thành công",
                len(copied_files),
            )
        else:
            logger.info(
                "ℹ️ [Session] Giữ nguyên session lỗi; bật "
                "SESSION_DELETE_AFTER_BACKUP=true nếu muốn xóa"
            )

        return result

    except Exception as backup_error:
        result["error"] = str(backup_error)
        logger.exception(
            "❌ [Session] Backup thất bại — không xóa session: %s",
            backup_error,
        )
        return result


_systems = None
message_queue: asyncio.Queue | None = None
message_workers: list = []
_history_queue: asyncio.Queue | None = None
_history_writer_task = None
_domain_semaphores: dict = {}
_submit_semaphore: asyncio.Semaphore | None = None
_active_submit_tasks: set = set()

_domain_queues: dict = {}
_domain_accounts: dict = {}
_domain_workers: dict = {}
_media_pipeline_queue: asyncio.Queue | None = None
_media_pipeline_workers: list[asyncio.Task] = []
_rate_limit_retry_handles: dict[asyncio.Handle, dict] = {}

_queue_full_counter: int = 0
_proc_semaphore: asyncio.Semaphore | None = None
_media_pipeline_semaphore: asyncio.Semaphore | None = None
_ocr_semaphore: asyncio.Semaphore | None = None
_domain_rate_limiters: dict = {}

# ✅ FIX LỖI DELAY NHẬN TIN: dedup ingress
_ingress_message_seen: dict = {}
_INGRESS_DEDUP_TTL = 300.0


# ═══════════════════════════════════════════════════════════════
# KJC SPECIAL SPOILER MODE
# ═══════════════════════════════════════════════════════════════
_KJC_SPECIAL_CHANNEL_IDS = {
    -1002528908352,
    -1003503954906,
}

_KJC_BROADCAST_DOMAINS = (
    "xx88code.com",
    "livemm88.net",
    "gg88live.tv",
    "rr88code.com",
)

_KJC_BROADCAST_URLS = {
    "xx88code.com": "https://xx88code.com",
    "livemm88.net": "https://livemm88.net/nhap-code",
    "gg88live.tv": "https://gg88live.tv/nhap-code",
    "rr88code.com": "https://rr88code.com",
}


def is_kjc_special_channel(chat_id) -> bool:
    try:
        return int(chat_id) in _KJC_SPECIAL_CHANNEL_IDS
    except Exception:
        return False


def _get_utf16_slice(text: str, offset: int, length: int) -> str:
    try:
        raw = text.encode("utf-16-le")
        start = int(offset) * 2
        end = start + int(length) * 2
        return raw[start:end].decode("utf-16-le", errors="ignore").strip()
    except Exception:
        return ""


def extract_kjc_spoiler_codes(event) -> list[str]:
    if not is_kjc_special_channel(getattr(event, "chat_id", None)):
        return []

    message = getattr(event, "message", None)
    if message is None:
        return []

    full_text = (
        getattr(message, "message", None)
        or getattr(message, "text", None)
        or ""
    )

    if not full_text:
        return []

    entities = getattr(message, "entities", None) or []
    codes = []
    for entity in entities:
        if not isinstance(entity, MessageEntitySpoiler):
            continue

        spoiler_text = _get_utf16_slice(
            full_text,
            getattr(entity, "offset", 0),
            getattr(entity, "length", 0),
        )

        if not spoiler_text:
            continue

        for line in spoiler_text.splitlines():
            line = line.strip()
            if not line:
                continue

            candidates = extract_tokens_from_line(line)
            if not candidates:
                candidates = [line]

            for candidate in candidates:
                cleaned = CodeValidator.clean_code(candidate)
                if not cleaned:
                    continue

                try:
                    result = validate_candidate(
                        cleaned,
                        "https://xx88code.com",
                        source="kjc_spoiler",
                    )
                except Exception as exc:
                    logger.warning(
                        "⚠️ [KJC] Không validate được '%s': %s",
                        cleaned,
                        exc,
                    )
                    continue

                if not result.get("valid"):
                    logger.warning(
                        "🚫 [KJC] Bỏ code không hợp lệ: %r",
                        cleaned,
                    )
                    continue

                code = result.get("clean_code") or cleaned
                if code.upper() not in {c.upper() for c in codes}:
                    codes.append(code)
                    logger.info("🎯 [KJC-SPOILER] phát hiện: %s", code)

    # KJC có thể đăng đồng thời spoiler và code text. Luôn chạy text fallback
    # rồi gộp, không trả sớm sau khi thấy spoiler.
    try:
        fallback = extract_codes_from_message(
            event,
            full_text,
            "https://xx88code.com",
            channel_name="KJC",
            include_text_after_spoiler=True,
        )
        return unique_keep_order(codes + (fallback or []))
    except Exception as exc:
        logger.debug("⚠️ [KJC] text fallback lỗi: %s", exc)
        return unique_keep_order(codes)


def detect_kjc_labeled_domains(event, raw_text: str = "") -> tuple[str, ...]:
    """Return a single site when KJC carries an unambiguous site label."""
    message = getattr(event, "message", None)
    text = " ".join(
        part for part in (
            raw_text,
            getattr(message, "message", None) if message else "",
            getattr(message, "text", None) if message else "",
        ) if part
    ).upper()

    aliases = {
        "MM88": "livemm88.net",
        "RR88": "rr88code.com",
        "XX88": "xx88code.com",
        "GG88": "gg88live.tv",
    }
    # Ưu tiên mẫu nhãn ngay sau cụm nhận code; tránh nhầm hashtag/banner
    # liệt kê cả bốn thương hiệu ở cuối caption.
    label_match = _KJC_LABEL_RE.search(text)
    if label_match:
        return (aliases[label_match.group(1).upper()],)

    found = tuple(dict.fromkeys(aliases[k] for k in aliases if _KJC_SITE_RE[k].search(text)))
    return found if len(found) == 1 else ()


def build_kjc_broadcast_items(
    codes: list[str], channel_name: str, target_domains: tuple[str, ...] = ()
) -> list[dict]:
    items = []
    domains = target_domains or _KJC_BROADCAST_DOMAINS
    for code in unique_keep_order(codes):
        for domain in domains:
            items.append(
                {
                    "code": code,
                    "channel_name": channel_name,
                    "target_url": _KJC_BROADCAST_URLS[domain],
                    "domain": domain,
                    "source": "kjc_spoiler",
                }
            )
    return items


# ═══════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════
def _log_separator():
    logger.info("─" * 70)


def normalize_domain(url: str) -> str:
    p = urlparse(url or "")
    return (p.netloc or p.path).lower().replace("www.", "").strip("/")


def _today_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def get_site_log_tag(target_url: str) -> str:
    d = normalize_domain(target_url)
    return _SITE_LOG_LABELS.get(d, d or "SYSTEM")


def _refresh_daily_state():
    today = _today_str()
    if bot_state._daily_date != today:
        bot_state._daily_used.clear()
        bot_state._channel_account_index.clear()
        bot_state._domain_account_cursor.clear()
        bot_state._daily_date = today
        logger.info(f"🗓️ Ngày mới ({today})")


def _mark_account_done_today(channel_key: str, username: str):
    _refresh_daily_state()
    bot_state._daily_used.add((_today_str(), channel_key, username))


def _is_account_done_today(channel_key: str, username: str) -> bool:
    _refresh_daily_state()
    return (_today_str(), channel_key, username) in bot_state._daily_used


def _result_indicates_account_limit(message: str) -> bool:
    text = str(message or "").upper()
    phrases = (
        "ĐẠT GIỚI HẠN",
        "DAT GIOI HAN",
        "ĐÃ ĐẠT GIỚI HẠN",
        "DA DAT GIOI HAN",
        "GIỚI HẠN NHẬN",
        "GIOI HAN NHAN",
        "HẾT LƯỢT",
        "HET LUOT",
        "LIMIT REACHED",
        "DAILY LIMIT",
        "ACCOUNT LIMIT",
        "MAXIMUM CLAIM",
    )
    return any(phrase in text for phrase in phrases)


def _get_next_available_account(channel_key: str, accounts: list):
    _refresh_daily_state()
    ordered = sorted(accounts, key=lambda a: a.get("priority", 999))
    if not ordered:
        return None

    # Round-robin theo domain: mỗi lần reserve thành công sẽ đẩy con trỏ
    # sang account kế tiếp. Account bận hoặc đã hết lượt trong ngày được
    # bỏ qua; vòng lặp vẫn tiếp tục để tìm fallback khả dụng.
    start = bot_state._domain_account_cursor.get(channel_key, 0) % len(ordered)
    for offset in range(len(ordered)):
        acc = ordered[(start + offset) % len(ordered)]
        u = acc["username"]
        if _is_account_done_today(channel_key, u):
            continue
        rk = (channel_key, u)
        if rk in bot_state._inflight_accounts:
            continue
        bot_state._inflight_accounts.add(rk)
        bot_state._domain_account_cursor[channel_key] = (start + offset + 1) % len(ordered)
        return acc
    return None


def _release_account_reservation(channel_key: str, username):
    if username:
        bot_state._inflight_accounts.discard((channel_key, username))


def _message_content_hash(text: str) -> str:
    return hashlib.md5((text or "").encode("utf-8", errors="ignore")).hexdigest()


def _message_event_fingerprint(message) -> str:
    """Hash Telegram content plus parsing-relevant entities.

    Telegram can edit a message's spoiler formatting without changing its
    visible text.  Text-only dedup treated that update as a duplicate, so the
    newly-spoilered code was never passed to the spoiler extractor.
    """
    text = (
        getattr(message, "message", None)
        or getattr(message, "text", None)
        or ""
    )
    media = getattr(message, "media", None)
    media_type = type(media).__name__ if media is not None else ""
    entity_parts = []
    for entity in getattr(message, "entities", None) or ():
        entity_type = type(entity).__name__
        entity_url = str(getattr(entity, "url", "") or "")
        # Only entity changes that can affect code/link extraction belong in
        # the dedup key; bold/italic formatting must not replay a submission.
        if "Spoiler" not in entity_type and "Url" not in entity_type and not entity_url:
            continue
        entity_parts.append(
            ":".join(
                (
                    entity_type,
                    str(getattr(entity, "offset", "")),
                    str(getattr(entity, "length", "")),
                    entity_url,
                )
            )
        )
    payload = f"{text}\x1f{media_type}\x1f{'|'.join(entity_parts)}"
    return _message_content_hash(payload)


def _ingress_fingerprint(event) -> tuple:
    message = getattr(event, "message", None)
    chat_id = getattr(event, "chat_id", None)
    message_id = getattr(message, "id", None)
    fingerprint = _message_event_fingerprint(message)
    return chat_id, message_id, fingerprint


def _on_message_queue_drop(item) -> None:
    """Allow a dropped queue item to be re-enqueued from durable inbox."""
    if isinstance(item, tuple) and len(item) == 2:
        try:
            row_id = int(item[1])
            bot_state._inbox_enqueued_ids.discard(row_id)
            _inbox_message_cache.pop(row_id, None)
        except (TypeError, ValueError):
            pass


async def _retry_dropped_domain_item(inbox_id: int, claim_token: str | None) -> None:
    """Retry a dropped domain item without blocking QueueManager's event loop."""
    if _durable_inbox is None:
        return
    try:
        await asyncio.get_running_loop().run_in_executor(
            _INBOX_STATE_EXECUTOR,
            _durable_inbox.retry_or_fail,
            inbox_id,
            "domain queue overflow/drop",
            5,
            claim_token,
        )
    except Exception as exc:
        logger.error("❌ Không thể retry inbox row=%s sau domain queue drop: %s", inbox_id, exc)


def _on_domain_queue_drop(item) -> None:
    """Schedule bounded inbox retry without blocking QueueManager's event loop."""
    inbox_id = item.get("inbox_id") if isinstance(item, dict) else None
    claim_token = item.get("claim_token") if isinstance(item, dict) else None
    if inbox_id and _durable_inbox is not None:
        try:
            task = asyncio.create_task(
                _retry_dropped_domain_item(int(inbox_id), claim_token),
                name=f"retry-dropped-domain-{inbox_id}",
            )
            bot_state.bg_tasks.add(task)
            task.add_done_callback(bot_state.bg_tasks.discard)
        except (TypeError, ValueError, RuntimeError) as exc:
            logger.error("❌ Không thể schedule retry inbox row=%s sau domain queue drop: %s", inbox_id, exc)


def _is_ocr_allowed_channel(chat_id) -> bool:
    """OCR theo channel: image-only list, hoặc fallback toàn bộ nếu bật env."""
    try:
        cfg = Config.CHANNEL_CONFIG.get(int(chat_id), {})
        filter_group = str(cfg.get("filter_group", "")).strip().lower()
        # Cờ theo từng channel có ưu tiên cao nhất. False tắt cả OCR ảnh và
        # video cho channel này, nhưng không ảnh hưởng ingress hoặc việc bắt
        # spoiler/caption/text đã chạy trước hàm này.
        if cfg.get("ocr_enabled") is False:
            return False
        disabled_groups = {
            str(item).strip().lower()
            for item in getattr(Config, "OCR_DISABLED_FILTER_GROUPS", set())
            if str(item).strip()
        }
        # Tắt OCR không tắt ingress hoặc text/spoiler extraction. Nó chỉ làm
        # media không có code trong spoiler/caption/text được bỏ qua thay vì
        # tải ảnh/video để OCR.
        if filter_group and filter_group in disabled_groups:
            return False
        if getattr(Config, "OCR_FALLBACK_ALL_MEDIA", False):
            return int(chat_id) in {int(x) for x in Config.CHANNEL_CONFIG}
        media_only = getattr(
            Config,
            "OCR_MEDIA_ONLY_CHANNEL_IDS",
            {-1002446066378, -1002272716520},
        )
        media_fallback = getattr(Config, "OCR_MEDIA_FALLBACK_CHANNEL_IDS", ())
        if int(chat_id) in {int(x) for x in (*media_only, *media_fallback)}:
            return True
        allowed = getattr(Config, "OCR_ALLOWED_CHANNEL_IDS", {-1002817093108})
        return int(chat_id) in {int(x) for x in allowed}
    except Exception:
        return False


def _has_hi88_code_link(event) -> bool:
    """HI88 chỉ OCR media khi bài có link nhập code, kể cả hidden URL."""
    message = getattr(event, "message", None)
    texts = [
        getattr(message, "text", "") or "",
        getattr(message, "message", "") or "",
    ]
    haystack = " ".join(texts).lower()
    if "hi88-freecode.pages.dev" in haystack:
        return True
    for entity in getattr(message, "entities", None) or []:
        url = getattr(entity, "url", "") or ""
        if "hi88-freecode.pages.dev" in url.lower():
            return True
    return False


def _should_enqueue(event) -> bool:
    key = _ingress_fingerprint(event)
    now = time.time()

    previous = _ingress_message_seen.get(key)
    if previous is not None and now - previous < _INGRESS_DEDUP_TTL:
        return False

    return True


def _mark_ingress_seen(event) -> None:
    """Record dedup only after the event is durably accepted."""
    _ingress_message_seen[_ingress_fingerprint(event)] = time.time()


def _prune_site_code_seen():
    ttl = float(getattr(Config, "SITE_CODE_DEDUP_TTL", 10.0))
    now = time.time()
    for k in [k for k, ts in bot_state._site_code_seen.items() if now - ts > ttl]:
        del bot_state._site_code_seen[k]


def _prune_ingress_message_seen():
    now = time.time()
    for k in [k for k, ts in _ingress_message_seen.items() if now - ts > _INGRESS_DEDUP_TTL]:
        del _ingress_message_seen[k]


def _prune_processed_message_hashes():
    ttl, now = 300.0, time.time()
    for k in [k for k, (_, ts) in bot_state._processed_message_hashes.items() if now - ts > ttl]:
        del bot_state._processed_message_hashes[k]


def is_site_code_duplicate(domain: str, user: str, code: str) -> bool:
    ttl = float(getattr(Config, "SITE_CODE_DEDUP_TTL", 10.0))
    now = time.time()
    k = (domain, user, code.upper())
    if bot_state._site_code_seen.get(k) is not None and now - bot_state._site_code_seen[k] < ttl:
        return True
    bot_state._site_code_seen[k] = now
    return False


def _clear_site_code_duplicate(domain: str, user: str, code: str) -> None:
    bot_state._site_code_seen.pop((domain, user, code.upper()), None)


def _get_proc_semaphore() -> asyncio.Semaphore:
    global _proc_semaphore
    if _proc_semaphore is None:
        _proc_semaphore = asyncio.Semaphore(int(getattr(Config, "MAX_CONCURRENT_PROCESSING", 24)))
    return _proc_semaphore


def _get_media_pipeline_semaphore() -> asyncio.Semaphore:
    global _media_pipeline_semaphore
    if _media_pipeline_semaphore is None:
        _media_pipeline_semaphore = asyncio.Semaphore(
            max(1, int(getattr(Config, "MAX_CONCURRENT_MEDIA_PIPELINES", 4)))
        )
    return _media_pipeline_semaphore


def _get_submit_semaphore() -> asyncio.Semaphore:
    global _submit_semaphore
    if _submit_semaphore is None:
        _submit_semaphore = asyncio.Semaphore(
            int(getattr(Config, "MAX_CONCURRENT_SUBMITS", 8))
        )
    return _submit_semaphore


def _get_ocr_semaphore() -> asyncio.Semaphore:
    global _ocr_semaphore
    if _ocr_semaphore is None:
        _ocr_semaphore = asyncio.Semaphore(max(1, int(getattr(Config, "MAX_CONCURRENT_OCR", 2))))
    return _ocr_semaphore


class _TokenBucket:
    def __init__(self, rpm: float, burst: float):
        self.rate_per_sec = max(float(rpm), 1.0) / 60.0
        self.capacity = max(float(burst), 1.0)
        self.tokens = self.capacity
        self.last_refill = time.time()
        self.lock = asyncio.Lock()

    async def acquire(self):
        while True:
            async with self.lock:
                now = time.time()
                self.tokens = min(self.capacity, self.tokens + (now - self.last_refill) * self.rate_per_sec)
                self.last_refill = now
                if self.tokens >= 1.0:
                    self.tokens -= 1.0
                    return
                wait = (1.0 - self.tokens) / self.rate_per_sec
            await asyncio.sleep(wait)


def get_domain_rate_limiter(domain: str) -> _TokenBucket:
    if domain not in _domain_rate_limiters:
        _domain_rate_limiters[domain] = _TokenBucket(
            float(getattr(Config, "REQUESTS_PER_MINUTE", 30)),
            float(getattr(Config, "MAX_BURST", 5)),
        )
    return _domain_rate_limiters[domain]


def get_domain_semaphore(domain: str) -> asyncio.Semaphore:
    if domain not in _domain_semaphores:
        limit = max(1, int(getattr(Config, "MAX_CONCURRENT_SUBMITS_PER_DOMAIN", 2)))
        _domain_semaphores[domain] = asyncio.Semaphore(limit)
    return _domain_semaphores[domain]


# ═══════════════════════════════════════════════════════════════
# CODE HISTORY
# ═══════════════════════════════════════════════════════════════
CODE_HISTORY_DIR = Path("logs/code_history")
CODE_HISTORY_DIR.mkdir(parents=True, exist_ok=True)


def _write_history_row(row: dict):
    try:
        fields = ["time", "event_type", "channel", "site", "account", "code", "source", "status", "telegram_delay", "submit_elapsed", "message", "screenshot"]
        csv_p = CODE_HISTORY_DIR / f"code_history_{_today_str()}.csv"
        jsonl_p = CODE_HISTORY_DIR / f"code_history_{_today_str()}.jsonl"
        header = not csv_p.exists()
        with csv_p.open("a", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            if header:
                w.writeheader()
            w.writerow(row)
        with jsonl_p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.debug(f"⚠️ write_history: {e}")


async def _history_writer_loop():
    while True:
        try:
            row = await _history_queue.get()
            if row is None:
                break
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(_DB_EXECUTOR, _write_history_row, row)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.debug(f"⚠️ history_writer: {e}")
        finally:
            try:
                _history_queue.task_done()
            except Exception:
                pass


def start_history_writer():
    global _history_queue, _history_writer_task
    _history_queue = asyncio.Queue(maxsize=2000)
    _history_writer_task = asyncio.create_task(_history_writer_loop())


def append_code_history(event_type, code="", target_url="", account="", channel="", source="", status="", telegram_delay=None, submit_elapsed=None, message="", screenshot=""):
    try:
        row = {
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "event_type": event_type,
            "channel": channel or "",
            "site": normalize_domain(target_url),
            "account": account or "",
            "code": str(code or ""),
            "source": source or "",
            "status": status or "",
            "telegram_delay": "" if telegram_delay is None else f"{float(telegram_delay):.2f}",
            "submit_elapsed": "" if submit_elapsed is None else f"{float(submit_elapsed):.2f}",
            "message": str(message or "").replace("\n", " ")[:300],
            "screenshot": str(screenshot or ""),
        }
        if _history_queue is not None:
            try:
                _history_queue.put_nowait(row)
            except asyncio.QueueFull:
                pass
        else:
            _write_history_row(row)
        return row
    except Exception as e:
        logger.debug(f"⚠️ append_history: {e}")
        return None


def build_daily_summary():
    try:
        csv_p = CODE_HISTORY_DIR / f"code_history_{_today_str()}.csv"
        if not csv_p.exists():
            return None
        summary = {}
        with csv_p.open("r", encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                if row.get("event_type") != "RESULT":
                    continue
                k = (row.get("site", ""), row.get("account", ""))
                summary.setdefault(k, {"SUCCESS": 0, "FAILED": 0, "UNKNOWN": 0})
                s = row.get("status") or "UNKNOWN"
                summary[k][s] = summary[k].get(s, 0) + 1
        out = CODE_HISTORY_DIR / f"daily_summary_{_today_str()}.csv"
        with out.open("w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=["date", "site", "account", "success", "failed", "unknown", "total"])
            w.writeheader()
            for (site, acc), c in sorted(summary.items()):
                s, fa, u = c.get("SUCCESS", 0), c.get("FAILED", 0), c.get("UNKNOWN", 0)
                w.writerow({"date": _today_str(), "site": site, "account": acc, "success": s, "failed": fa, "unknown": u, "total": s + fa + u})
        return str(out)
    except Exception as e:
        logger.warning(f"⚠️ daily_summary: {e}")
        return None


def measure_telegram_delay_fast(msg_ts):
    try:
        if msg_ts.tzinfo is None:
            msg_ts = msg_ts.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - msg_ts).total_seconds()
    except Exception:
        return None


# ═══════════════════════════════════════════════════════════════
# DOMAIN WORKER
# ═══════════════════════════════════════════════════════════════
def build_domain_accounts_map() -> dict:
    merged = {}
    for cfg in Config.CHANNEL_CONFIG.values():
        d = normalize_domain(cfg.get("url", ""))
        if not d:
            continue
        for acc in cfg.get("accounts", []):
            k = (d, acc["username"])
            if k not in merged or acc.get("priority", 999) < merged[k].get("priority", 999):
                merged[k] = acc
    result = {}
    for (d, _u), acc in merged.items():
        result.setdefault(d, []).append(acc)
    for d in result:
        result[d].sort(key=lambda a: a.get("priority", 999))
    for d, accounts in getattr(Config, "DOMAIN_ACCOUNT_OVERRIDES", {}).items():
        result[d] = [dict(acc) for acc in accounts]
    return result


def get_domain_queue(domain: str) -> asyncio.Queue:
    if domain not in _domain_queues:
        q = asyncio.Queue(maxsize=int(getattr(Config, "DOMAIN_QUEUE_MAXSIZE", 500)))
        _domain_queues[domain] = q
        qm = get_queue_manager()
        if qm is not None:
            qm.register(f"domain:{domain}", q, on_drop=_on_domain_queue_drop)
    return _domain_queues[domain]


def _code_fanout_count(domain: str, accounts: list | None = None) -> int:
    """Số tài khoản nhận cùng một code trong một đợt phát.

    Mặc định là 2 theo yêu cầu vận hành: 1 code được nhập đồng thời cho
    2 account; nếu domain thực tế chỉ có 1 account thì tự hạ xuống 1 để
    không tạo item vô nghĩa trong queue.
    """
    configured = max(1, int(getattr(Config, "ACCOUNTS_PER_CODE", 2)))
    available = accounts if accounts is not None else _domain_accounts.get(domain, [])
    return max(1, min(configured, len(available) or 1))


def _fanout_code_item(item: dict, count: int) -> list[dict]:
    """Nhân một code thành nhiều dispatch item, mỗi item giữ cùng inbox row."""
    return [
        {**item, "fanout": True, "fanout_index": index}
        for index in range(max(1, int(count)))
    ]


async def _submit_code_with_account_retries(
    account,
    code,
    target_url,
    domain,
    queue_wait_ms: float | None = None,
) -> str:
    user = account["username"]

    max_r = max(
        1,
        int(
            getattr(
                Config,
                "MAX_RETRIES_PER_ACCOUNT",
                1,
            )
        ),
    )

    retry = bool(
        getattr(
            Config,
            "RETRY_ON_TIMEOUT",
            True,
        )
    )

    for attempt in range(1, max_r + 1):
        result = (
            await submit_code_with_delay(
                user,
                code,
                target_url,
                _systems,
                queue_wait_ms=queue_wait_ms if attempt == 1 else None,
            )
            or {}
        )

        success = bool(result.get("success", False))
        infra_failure = bool(result.get("_infra_failure", False))
        has_pts = bool(result.get("has_points", False))
        wrong = bool(result.get("is_wrong_code", False))
        blocked = bool(result.get("is_account_blocked", False))
        if result.get("rate_limited"):
            append_code_history(
                event_type="SUBMIT_ATTEMPT",
                code=code,
                target_url=target_url,
                account=user,
                status="RATE_LIMITED",
                message=str(result.get("message", "rate limited"))[:120],
            )
            return "RATE_LIMITED"

        msg = str(
            result.get("message", "")
            or ""
        )[:120]

        result_code = str(
            result.get("code", "")
            or ""
        ).upper().strip()

        msg_upper = msg.upper()

        # A busy/unavailable browser tab is not a site result. Keep it
        # retryable and never classify it as a false NO_RESULT.
        if (infra_failure and not result.get("keep_page") and not result.get("_clicked")
                and "CLOUDFLARE" not in msg_upper and "TURNSTILE" not in msg_upper):
            if retry and attempt < max_r:
                logger.warning(
                    "🔁 [%s] %s — browser infrastructure retry %s/%s — %s",
                    domain, code, attempt, max_r, msg or "tab unavailable",
                )
                await asyncio.sleep(min(0.25 * attempt, 1.0))
                continue
            append_code_history(
                event_type="SUBMIT_ATTEMPT",
                code=code,
                target_url=target_url,
                account=user,
                status="INFRA_FAILURE",
                message=msg or "browser infrastructure unavailable",
            )
            return "INFRA_FAILURE"

        if result.get("_verification_pending"):
            append_code_history(
                event_type="SUBMIT_ATTEMPT",
                code=code,
                target_url=target_url,
                account=user,
                status="INFRA_FAILURE",
                message=msg,
            )
            return "INFRA_FAILURE"

        # Không retry/reload cùng widget khi Cloudflare/Turnstile đang chờ
        # người dùng. Retry ở đây sẽ mở lại form và làm mới captcha hiện tại.
        if result.get("keep_page") or "CLOUDFLARE" in msg_upper or "TURNSTILE" in msg_upper:
            logger.warning(
                "⏸️ [%s] %s — giữ nguyên page, không retry captcha",
                domain,
                msg or "Cloudflare verification pending",
            )
            return "FAILED"

        # Một số site trả đồng thời thông báo thành công và "đã đạt giới hạn".
        # Không đánh dấu code đã dùng trong trường hợp này; đánh dấu tài khoản
        # hết lượt để worker requeue code cho tài khoản kế tiếp.
        if blocked or _result_indicates_account_limit(msg_upper):
            _mark_account_done_today(domain, user)
            append_code_history(
                event_type="SUBMIT_ATTEMPT",
                code=code,
                target_url=target_url,
                account=user,
                status="ACCOUNT_BLOCKED",
                message=msg or result_code or "Account limit reached",
            )
            logger.warning("⏭️ [%s] %s đạt giới hạn — chuyển tài khoản kế tiếp", domain, user)
            return "ACCOUNT_BLOCKED"

        terminal_failure = (
            result_code in {
                "CAPTCHA_INVALID",
                "CODE_NOT_USED",
                "CODE_USED",
                "INVALID_CODE",
                "CAPTCHA_FAILED",
                "CAPTCHA_EXPIRED",
            }
            or "CAPTCHA_INVALID" in msg_upper
            or "CAPTCHA XÁC THỰC KHÔNG HỢP LỆ" in msg_upper
            or "CAPTCHA KHÔNG HỢP LỆ" in msg_upper
            or "CODE ĐÃ ĐƯỢC SỬ DỤNG" in msg_upper
            or "MÃ ĐÃ ĐƯỢC SỬ DỤNG" in msg_upper
            or "CODE NOT USED" in msg_upper
        )

        # CAPTCHA lỗi hoặc code đã được sử dụng:
        # không retry cùng code để tránh mất thêm thời gian.
        if terminal_failure:
            logger.warning(
                "⏭️ [%s] %s — %s — không retry",
                domain,
                code,
                msg or result_code or "terminal failure",
            )

            return "FAILED"

        if success and has_pts:
            await _mark_code_used_if_final(
                domain,
                code,
                "SUCCESS_POINTS",
            )

            append_code_history(
                event_type="FINAL_RESULT",
                code=code,
                target_url=target_url,
                account=user,
                status="SUCCESS_POINTS",
                message="OK có điểm",
            )

            return "SUCCESS_POINTS"

        if success and not has_pts:
            await _mark_code_used_if_final(
                domain,
                code,
                "SUCCESS_NO_POINTS",
            )

            append_code_history(
                event_type="FINAL_RESULT",
                code=code,
                target_url=target_url,
                account=user,
                status="SUCCESS_NO_POINTS",
                message="OK không điểm",
            )

            return "SUCCESS_NO_POINTS"

        if wrong:
            await _mark_code_used_if_final(
                domain,
                code,
                "FAILED",
            )

            append_code_history(
                event_type="SUBMIT_ATTEMPT",
                code=code,
                target_url=target_url,
                account=user,
                status="FAILED",
                message=msg,
            )

            return "FAILED"

        # Once a click was accepted by the page, a missing/ambiguous response
        # is not safe to retry automatically: the site may already have
        # processed the code. Preserve the unknown result for reconciliation.
        if result.get("_clicked"):
            append_code_history(
                event_type="FINAL_RESULT",
                code=code,
                target_url=target_url,
                account=user,
                status="UNKNOWN_CLICKED",
                message=msg or "Clicked; result not detected; automatic retry skipped",
            )
            return "NO_RESULT"

        # Chỉ retry lỗi tạm thời như timeout hoặc lỗi mạng.
        if retry and attempt < max_r:
            logger.warning(
                "🔁 [%s] %s — retry %s/%s — %s",
                domain,
                code,
                attempt,
                max_r,
                msg or "temporary error",
            )

            await asyncio.sleep(
                min(
                    0.3 * attempt,
                    1.0,
                )
            )

            continue

        append_code_history(
            event_type="FINAL_RESULT",
            code=code,
            target_url=target_url,
            account=user,
            status="NO_RESULT",
            message=(
                f"NO_RESULT sau {attempt} lần"
                f"{': ' + msg if msg else ''}"
            ),
        )

        return "NO_RESULT"

    return "NO_RESULT"


async def domain_code_worker(domain: str, target_url: str, worker_id: int = 1):
    queue = get_domain_queue(domain)
    accounts = _domain_accounts.get(domain, [])
    site_tag = get_site_log_tag(target_url)
    log_tok = set_log_context(site_tag)
    try:
        logger.info(f"👷 [Domain-Worker#{worker_id}] '{domain}' ready — {len(accounts)} accounts")
        while bot_state.is_running or not queue.empty():
            try:
                item = await asyncio.wait_for(queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break

            code = item["code"]
            inbox_id = item.get("inbox_id")
            claim_token = item.get("claim_token")
            queued_at_perf = item.get("_queue_enqueued_perf")
            queue_wait_ms = (
                max(0.0, (time.perf_counter() - queued_at_perf) * 1000.0)
                if isinstance(queued_at_perf, (int, float)) else None
            )

            item_target_url = item.get("target_url") or target_url
            item_domain = normalize_domain(item_target_url)


            item_tok = set_log_context(site_tag)
            account = None
            try:
                db = _systems.get("db") if _systems else None
                if db is not None:
                    try:
                        loop = asyncio.get_running_loop()
                        # Một code có thể được dispatch đồng thời cho 2 account.
                        # Lượt đầu có thể đã mark_code_used trước khi lượt thứ
                        # hai đọc DB; fanout item vẫn phải được submit.
                        if not item.get("fanout") and await loop.run_in_executor(_DB_EXECUTOR, db.is_code_used, item_domain, code):
                            if inbox_id and _durable_inbox is not None:
                                await loop.run_in_executor(
                                    _INBOX_STATE_EXECUTOR,
                                    _durable_inbox.complete_item,
                                    int(inbox_id),
                                    claim_token,
                                )
                            continue
                    except Exception:
                        pass

                account = _get_next_available_account(item_domain, _domain_accounts.get(item_domain, []))
                if account is None:
                    append_code_history(event_type="FINAL_RESULT", code=code, target_url=item_target_url, account="", status="NO_ACCOUNT", message="Hết tài khoản")
                    if inbox_id and _durable_inbox is not None:
                        # ✅ FIX: dùng retry_or_fail (có giới hạn số lần +
                        # backoff) thay vì retry() vô hạn — nếu domain hết
                        # tài khoản kéo dài (vd tất cả đã đạt giới hạn ngày),
                        # dòng inbox sẽ tự failed sau MAX_INBOX_ATTEMPTS lần
                        # thay vì replay mãi.
                        await asyncio.get_running_loop().run_in_executor(
                            _INBOX_EXECUTOR,
                            _durable_inbox.retry_or_fail,
                            int(inbox_id),
                            "no available account",
                            15,
                            claim_token,
                        )
                    continue

                if is_site_code_duplicate(item_domain, account["username"], code):
                    logger.info(
                        "⏭️ [%s#%s] duplicate domain-user-code: %s/%s/%s",
                        item_domain,
                        worker_id,
                        item_domain,
                        account["username"],
                        code,
                    )
                    if inbox_id and _durable_inbox is not None:
                        await asyncio.get_running_loop().run_in_executor(
                            _INBOX_STATE_EXECUTOR,
                            _durable_inbox.complete_item,
                            int(inbox_id),
                            claim_token,
                        )
                    continue

                logger.info(f"🎯 [{item_domain}#{worker_id}] {code} → {account['username']}")
                status = await _submit_code_with_account_retries(
                    account, code, item_target_url, item_domain, queue_wait_ms=queue_wait_ms
                )

                if status == "RATE_LIMITED":
                    delay = max(0.1, float(getattr(Config, "RATE_LIMIT_BACKOFF_SECONDS", 5.0)))
                    _clear_site_code_duplicate(item_domain, account.get("username"), code)
                    retry_count = int(item.get("_rate_limit_retries", 0))
                    retry_limit = max(0, int(getattr(Config, "MAX_RATE_LIMIT_RETRIES", 5)))
                    if retry_count >= retry_limit:
                        append_code_history(
                            event_type="FINAL_RESULT",
                            code=code,
                            target_url=item_target_url,
                            account=account.get("username", ""),
                            status="RATE_LIMITED_EXHAUSTED",
                            message=f"Rate-limit retries exhausted ({retry_count})",
                        )
                        if inbox_id and _durable_inbox is not None:
                            await asyncio.get_running_loop().run_in_executor(
                                _INBOX_STATE_EXECUTOR,
                                _durable_inbox.complete_item,
                                int(inbox_id), claim_token,
                            )
                        continue
                    retry_item = {**item, "_rate_limit_retries": retry_count + 1}
                    loop = asyncio.get_running_loop()

                    handle_ref = {}

                    def _enqueue_rate_limit_retry(retry_item=retry_item, retry_queue=queue):
                        handle = handle_ref.get("handle")
                        if handle is not None:
                            _rate_limit_retry_handles.pop(handle, None)
                        retry_item["_queue_enqueued_perf"] = time.perf_counter()
                        try:
                            retry_queue.put_nowait(retry_item)
                        except asyncio.QueueFull:
                            inbox_row = retry_item.get("inbox_id")
                            claim = retry_item.get("claim_token")
                            if inbox_row and _durable_inbox is not None:
                                schedule_tracked_task(
                                    _retry_dropped_domain_item(int(inbox_row), claim),
                                    bot_state.bg_tasks,
                                    name=f"rate-limit-queue-full-{inbox_row}",
                                )
                            else:
                                logger.warning("⚠️ [%s] rate-limit retry dropped: domain queue full", item_domain)

                    handle = loop.call_later(delay, _enqueue_rate_limit_retry)
                    handle_ref["handle"] = handle
                    _rate_limit_retry_handles[handle] = retry_item
                    logger.warning("⏳ [%s] %s rate-limited; requeue after %.1fs", item_domain, code, delay)
                    continue

                if status == "ACCOUNT_BLOCKED":
                    try:
                        queue.put_nowait({**item, "fanout": True, "_queue_enqueued_perf": time.perf_counter()})
                    except asyncio.QueueFull:
                        if inbox_id and _durable_inbox is not None:
                            # ✅ FIX: bound bằng retry_or_fail thay vì retry() vô hạn.
                            await asyncio.get_running_loop().run_in_executor(
                                _INBOX_EXECUTOR,
                                _durable_inbox.retry_or_fail,
                                int(inbox_id),
                                "account blocked and domain queue full",
                                15,
                                claim_token,
                            )
                    continue

                if status == "INFRA_FAILURE":
                    if account is not None:
                        _clear_site_code_duplicate(item_domain, account["username"], code)
                    retry_state = "retried"
                    if inbox_id and _durable_inbox is not None:
                        retry_state = await asyncio.get_running_loop().run_in_executor(
                            _INBOX_EXECUTOR,
                            _durable_inbox.retry_or_fail,
                            int(inbox_id),
                            f"browser infrastructure unavailable for {code}",
                            10,
                            claim_token,
                        )
                    # DurableInbox sẽ tự đưa row pending trở lại queue sau
                    # backoff. Chỉ requeue trực tiếp khi item không có inbox
                    # row, tránh tạo hai bản sao của cùng một code.
                    if retry_state == "retried" and not inbox_id:
                        try:
                            queue.put_nowait({**item, "_queue_enqueued_perf": time.perf_counter()})
                        except asyncio.QueueFull:
                            logger.warning(
                                "⚠️ [%s#%s] queue full while requeueing infrastructure failure for %s",
                                item_domain,
                                worker_id,
                                code,
                            )
                    continue

                # ✅ FIX RETRY-STORM + RESET TOÀN ROW:
                # _submit_code_with_account_retries() ĐÃ tự retry nội bộ
                # (theo MAX_RETRIES_PER_ACCOUNT) trước khi trả về đây, nên
                # mọi kết quả SUCCESS*/FAILED/NO_RESULT ở tầng này đều là
                # kết quả CUỐI CÙNG cho đúng 1 code trong dòng inbox (dòng
                # có thể chứa nhiều code nếu 1 tin nhắn Telegram có nhiều
                # mã). Trước đây:
                #   - FAILED  → mark_failed() ghi đè TOÀN BỘ dòng, xoá luôn
                #     tiến độ của các code anh em khác chưa xử lý xong.
                #   - còn lại (NO_RESULT) → retry() reset TOÀN BỘ dòng về
                #     'pending' + remaining_items=0, KHÔNG giới hạn số lần
                #     → bị replay lại (fetch lại message, extract lại code,
                #     submit lại) VÔ HẠN mỗi vài giây, làm nghẽn queue và
                #     trễ tin nhắn mới.
                # Giờ mọi outcome ở tầng code-đơn-lẻ này đều dùng
                # complete_item() để CHỈ giảm remaining_items của dòng
                # (không đụng tới code anh em khác, không replay lại toàn
                # bộ tin nhắn Telegram). Việc retry ở cấp submit đã được
                # _submit_code_with_account_retries() đảm nhiệm; muốn retry
                # nhiều hơn thì tăng MAX_RETRIES_PER_ACCOUNT trong .env.
                if inbox_id and _durable_inbox is not None:
                    await asyncio.get_running_loop().run_in_executor(
                        _INBOX_EXECUTOR, _durable_inbox.complete_item, int(inbox_id), claim_token
                    )

                icon = "✅" if status.startswith("SUCCESS") else ("❌" if status == "FAILED" else "⏰")
                logger.info(f"{icon} [{item_domain}#{worker_id}] {code} → {status}")
            except asyncio.CancelledError:
                # ✅ FIX: row đã được claim() ở đầu vòng lặp (status='processing')
                # trước khi submit; nếu task này bị task.cancel() giữa chừng lúc
                # shutdown (xem main()'s domain_tasks cancel sau timeout 8s), row
                # phải được đưa lại 'pending' KÈM đúng claim_token — nếu không,
                # ở TELEGRAM_INBOX_RECOVERY_MODE=live_only (mặc định) sẽ không có
                # reclaim_stale() nào chạy để nhặt lại row 'processing' bị bỏ dở,
                # khiến code đang submit dở khi tắt bot bị mất vĩnh viễn cho tới
                # khi lease hết hạn (300s) mà vẫn không ai reclaim ở chế độ này.
                if inbox_id and _durable_inbox is not None:
                    try:
                        requeued = await asyncio.get_running_loop().run_in_executor(
                            _INBOX_EXECUTOR,
                            _durable_inbox.requeue_claimed_for_shutdown,
                            int(inbox_id),
                            claim_token,
                            "domain worker cancelled during shutdown",
                        )
                        if requeued:
                            logger.warning(
                                "♻️ [Inbox] requeue row=%s (domain=%s) vì domain worker bị hủy",
                                inbox_id, item_domain,
                            )
                    except Exception:
                        logger.exception(
                            "❌ [Inbox] không requeue được row=%s khi domain worker bị hủy",
                            inbox_id,
                        )
                if account is not None:
                    _clear_site_code_duplicate(item_domain, account["username"], code)
                raise
            except Exception as e:
                logger.error(f"❌ [{item_domain}#{worker_id}] {code}: {e}")
                if account is not None:
                    _clear_site_code_duplicate(item_domain, account["username"], code)
                if inbox_id and _durable_inbox is not None:
                    try:
                        # ✅ FIX: retry_or_fail — bound bởi MAX_INBOX_ATTEMPTS.
                        await asyncio.get_running_loop().run_in_executor(
                            _INBOX_EXECUTOR,
                            _durable_inbox.retry_or_fail,
                            int(inbox_id),
                            f"domain worker: {e}",
                            5,
                            claim_token,
                        )
                    except Exception:
                        logger.exception("❌ Không thể retry inbox row=%s", inbox_id)
            finally:
                if account is not None:
                    _release_account_reservation(item_domain, account.get("username"))
                reset_log_context(item_tok)
                queue.task_done()
    finally:
        reset_log_context(log_tok)


def start_domain_workers():
    global _domain_accounts
    _domain_accounts = build_domain_accounts_map()
    dmap = {}
    for cfg in Config.CHANNEL_CONFIG.values():
        if not cfg.get("enabled", True):
            continue
        d = normalize_domain(cfg.get("url", ""))
        if d and d not in dmap:
            dmap[d] = cfg["url"]

    total = 0
    for d, url in dmap.items():
        if d in _domain_workers:
            continue
        n_acc = len(_domain_accounts.get(d, []))
        cap = max(1, int(getattr(Config, "MAX_CONCURRENT_SUBMITS_PER_DOMAIN", 3)))
        wc = max(1, min(cap, n_acc or 1))
        ws = []
        for i in range(1, wc + 1):
            t = asyncio.create_task(domain_code_worker(d, url, worker_id=i), name=f"domain-{d}-{i}")
            ws.append(t)
            bot_state.bg_tasks.add(t)
        _domain_workers[d] = ws
        total += wc
    logger.info(f"🚀 {total} domain workers (browser-only) cho {len(dmap)} domain")


async def _mark_code_used_if_final(domain: str, code: str, status: str):
    if status not in ("SUCCESS_POINTS", "SUCCESS_NO_POINTS", "FAILED"):
        return
    db = _systems.get("db") if _systems else None
    if db is None:
        return
    try:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(_DB_EXECUTOR, db.mark_code_used, domain, code)
    except Exception as e:
        logger.debug(f"⚠️ mark_code_used: {e}")


# ═══════════════════════════════════════════════════════════════
# EXTRACT CODE
# ═══════════════════════════════════════════════════════════════
def validate_candidate(code: str, target_url: str, source: str = "normal"):
    try:
        return CodeValidator.validate_code(code, target_url, source=source)
    except TypeError:
        return CodeValidator.validate_code(code, target_url)


def get_filter_group_name(target_url: str) -> str:
    name, _ = CodeValidator.get_filter_group(target_url)
    return name


def unique_keep_order(items):
    seen, result = set(), []
    for it in items:
        c = CodeValidator.clean_code(it)
        if not c:
            continue
        u = c.upper()
        if u not in seen:
            seen.add(u)
            result.append(c)
    return result


def remove_noise_from_text(text: str) -> str:
    t = text or ""
    t = _URL_RE.sub(" ", t)
    t = _WWW_RE.sub(" ", t)
    t = _DOMAIN_RE.sub(" ", t)
    t = _HASHTAG_RE.sub(" ", t)
    return t.replace("：", ":").replace("|", " ").replace("•", " ")


def line_has_code_marker(line: str) -> bool:
    return bool(_CODE_MARKER_RE.search(line or ""))


def line_is_noise(line: str) -> bool:
    u = line.strip()
    if not u:
        return True
    if _NOISE_RE.search(u):
        return True
    if _CURRENCY_RE.search(u):
        return True
    return False


_VIETNAMESE_DIACRITIC_RE = re.compile(r"[àáảãạăằắẳẵặâầấẩẫậèéẻẽẹêềếểễệìíỉĩịòóỏõọôồốổỗộơờớởỡợùúủũụưừứửữựỳýỷỹỵđ]", re.IGNORECASE)


def _line_has_vietnamese(line: str) -> bool:
    if not line:
        return False
    if _VIETNAMESE_DIACRITIC_RE.search(line):
        return True
    lower = line.lower()
    return any(w in lower for w in CodeValidator.VIETNAMESE_TEXT_WORDS)


def extract_tokens_from_line(line: str):
    mn = getattr(Config, "CODE_MIN_LENGTH", 6)
    mx = getattr(Config, "CODE_MAX_LENGTH", 15)
    return [c for c in _TOKEN_RE.findall(line or "") if mn <= len(CodeValidator.clean_code(c)) <= mx]


def extract_spoiler_codes(event, target_url: str):
    codes = []
    if not event.message.entities:
        return codes
    full = event.message.message or event.message.text or ""
    if not full:
        return codes
    try:
        for ent in event.message.entities:
            if not isinstance(ent, MessageEntitySpoiler):
                continue
            try:
                rb = full.encode("utf-16-le")
                s, e = ent.offset * 2, ent.offset * 2 + ent.length * 2
                sp = rb[s:e].decode("utf-16-le", errors="ignore").strip()
            except Exception:
                sp = full[ent.offset: ent.offset + ent.length].strip()
            if not sp:
                continue
            for line in (sp.splitlines() if "\n" in sp else [sp]):
                line = line.strip()
                if not line:
                    continue
                for tok in (extract_tokens_from_line(line) or [line]):
                    v = validate_candidate(tok, target_url, source="spoiler")
                    if v["valid"]:
                        codes.append(v["clean_code"])
                        logger.info(f"🔒 Spoiler: {v['clean_code']}")
    except Exception as e:
        logger.warning(f"⚠️ spoiler: {e}")
    return unique_keep_order(codes)


def extract_marker_near_codes(text: str, target_url: str):
    lines = [l.strip() for l in remove_noise_from_text(text).splitlines()]
    codes = []
    _, group_config = CodeValidator.get_filter_group(target_url)
    marker_scan_lines = max(
        0,
        int(group_config.get("marker_scan_lines", 3) or 0),
    )
    for i, line in enumerate(lines):
        if not line_has_code_marker(line):
            continue
        scan = [line] if line else []
        for off in range(1, marker_scan_lines + 1):
            if i + off < len(lines):
                scan.append(lines[i + off])
        for sl in scan:
            if line_is_noise(sl):
                continue
            for tok in extract_tokens_from_line(sl):
                v = validate_candidate(CodeValidator.clean_code(tok), target_url, source="marker")
                if v["valid"]:
                    codes.append(v["clean_code"])
                    logger.info(f"🎯 Marker: {v['clean_code']}")
    return unique_keep_order(codes)


_PLAIN_TEXT_ORDER_PHONE_CONTEXT_RE = re.compile(
    r"(?:mã\s*(?:đơn|don|dh)|đơn\s*hàng|don\s*hang|order|invoice|"
    r"transaction|tracking|mã\s*giao\s*dịch|ma\s*giao\s*dich|"
    r"sđt|sdt|điện\s*thoại|dien\s*thoai|phone|hotline|liên\s*hệ|lien\s*he)",
    re.IGNORECASE,
)


def _plain_text_token_is_safe(token: str, line: str) -> bool:
    """Conservative guard for unmarked text; markers/spoilers use stricter paths."""
    clean = CodeValidator.clean_code(token)
    if not clean or not any(ch.isalpha() for ch in clean):
        # Không tự suy đoán số thuần trong text thường: tránh số điện thoại,
        # mã OTP, ngày tháng và mã đơn chỉ gồm chữ số.
        return False

    digit_count = sum(ch.isdigit() for ch in clean)
    if digit_count >= 7 or (len(clean) >= 10 and digit_count / len(clean) >= 0.7):
        return False

    context = line[max(0, line.find(token) - 40): line.find(token) + len(token) + 40]
    if _PLAIN_TEXT_ORDER_PHONE_CONTEXT_RE.search(context):
        return False

    return True


def extract_plain_text_codes(text: str, target_url: str):
    """Scan ordinary text conservatively when no spoiler/marker code was found."""
    if not text:
        return []

    codes = []
    for raw_line in remove_noise_from_text(text).splitlines():
        line = raw_line.strip()
        if not line or line_has_code_marker(line) or line_is_noise(line):
            continue
        for token in extract_tokens_from_line(line):
            if not _plain_text_token_is_safe(token, line):
                continue
            result = validate_candidate(
                CodeValidator.clean_code(token), target_url, source="plain_text"
            )
            if result.get("valid"):
                codes.append(result.get("clean_code") or token)
                logger.info("🎯 Plain-text: %s", result.get("clean_code") or token)
    return unique_keep_order(codes)


def extract_hi88_near_link_codes(text: str, target_url: str) -> list:
    if not text:
        return []
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    codes = []
    for i, line in enumerate(lines):
        if "hi88-freecode.pages.dev" not in line.lower():
            continue
        nearby = []
        for off in range(1, 4):
            if i - off >= 0:
                nearby.append(lines[i - off])
            if i + off < len(lines):
                nearby.append(lines[i + off])
        for candidate_line in nearby:
            if line_is_noise(candidate_line):
                continue
            if _line_has_vietnamese(candidate_line):
                continue
            for tok in extract_tokens_from_line(candidate_line):
                v = validate_candidate(tok, target_url, source="marker")
                if v["valid"]:
                    codes.append(v["clean_code"])
                    logger.info(f"🎯 [HI88-LINK] {v['clean_code']}")
    return unique_keep_order(codes)


def extract_codes_by_regex(text: str, site_type: str = "qq88") -> list:
    if not text:
        return []
    codes = []
    if site_type == "qq88":
        BL = {"QQ88", "CODE", "DANGNHAP", "GAMEBAI", "NOHU", "CASINO", "REVIEWPHIM", "TINTUC", "KHUYENMAI", "GIFTCODE", "FREECODE", "CAMERA", "TROLL", "BONGDA", "THETHAO", "MINIGAME"}
        for m in _ALNUM_TOKEN_RE.findall(text):
            if any(k in m.upper() for k in BL):
                continue
            hl = any(c.isalpha() for c in m)
            hd = any(c.isdigit() for c in m)
            hll = any(c.islower() for c in m)
            hu = any(c.isupper() for c in m)
            if hl and (hd or (hll and hu)):
                codes.append(m)
    return list(dict.fromkeys(codes))


def extract_codes_from_message(
    event,
    raw_text: str,
    target_url: str,
    channel_name: str = "",
    include_text_after_spoiler: bool = False,
):
    # Contract: spoiler is always attempted before marker/regex/OCR paths.
    group = get_filter_group_name(target_url)
    logger.debug(f"[EXTRACT] group={group} | url={target_url}")

    spoiler = extract_spoiler_codes(event, target_url)
    if spoiler:
        logger.warning(f"🎯 [SPOILER] {len(spoiler)}: {spoiler}")
        if not include_text_after_spoiler:
            # Fast path tuyệt đối: spoiler hợp lệ là nguồn ưu tiên cao nhất.
            # Trả ngay để không chạy caption/link/marker/plain-text filtering
            # trước khi đưa code vào queue.
            return unique_keep_order(spoiler)
        collected = list(spoiler)
    else:
        collected = []


    if group == "qq88":
        caption = (
            getattr(event.message, "message", None)
            or getattr(event.message, "text", None)
            or raw_text
            or ""
        ).strip().lower()
        # Telethon event thường proxy thuộc tính media, nhưng một số update
        # channel/media chỉ expose nó trên message. Dùng cả hai để không đánh
        # nhầm bài ảnh/video thành bài text rỗng rồi bỏ qua trước OCR.
        has_media = bool(
            getattr(event, "media", None)
            or getattr(getattr(event, "message", None), "media", None)
        )
        has_text = bool((raw_text or caption).strip())
        if collected:
            logger.info("✅ [QQ88] spoiler/text đã nhận; không yêu cầu link khi đã có mã hợp lệ")
        elif has_text:
            logger.info("✅ [QQ88] text")
        elif has_media:
            if "tangquaqq88.com" in caption:
                logger.info("⏭️ [QQ88] chỉ có caption/link, không có spoiler code → bỏ qua, không OCR")
            else:
                logger.info("⏭️ [QQ88] no link → skip")
                return []
        else:
            logger.info("⏭️ [QQ88] nothing → skip")
            return []


    if group == "hi88":
        hc = extract_hi88_near_link_codes(raw_text, target_url)
        if hc:
            logger.warning(f"🎯 [HI88-LINK] {hc}")
            collected.extend(hc)

    mc = extract_marker_near_codes(raw_text, target_url)
    if mc:
        logger.warning(f"🎯 [MARKER] {len(mc)}: {mc}")
        collected.extend(mc)

    # Chỉ quét text thường khi spoiler/marker/link-near chưa tìm được code.
    # Nhánh này có guard riêng để không biến số điện thoại/mã đơn thành code.
    if not collected:
        pc = extract_plain_text_codes(raw_text, target_url)
        if pc:
            logger.warning(f"🎯 [PLAIN-TEXT] {len(pc)}: {pc}")
            collected.extend(pc)

    if collected:
        merged = unique_keep_order(collected)
        logger.warning(f"🎯 [EXTRACT-MERGED] {len(merged)}: {merged}")
        return merged


    if group == "qq88" and "tangquaqq88.com" in raw_text.lower():
        cleaned = _URL_RE.sub("", raw_text)
        cleaned = _TME_RE.sub("", cleaned)
        rq = [validate_candidate(r, target_url, source="regex")["clean_code"] for r in extract_codes_by_regex(cleaned, "qq88") if validate_candidate(r, target_url, source="regex")["valid"]]
        if rq:
            logger.info(f"🎯 [QQ88-REGEX] {rq}")
            return list(dict.fromkeys(rq))

    return []


# ═══════════════════════════════════════════════════════════════
# SUBMIT CODE — BROWSER-ONLY
# ═══════════════════════════════════════════════════════════════
async def submit_code_safe(user: str, code: str, target_url: str, systems: dict):
    domain = normalize_domain(target_url)
    logger.info(f"🚀 [Browser] SUBMIT | {user} | {code} | {domain}")
    started = time.monotonic()
    try:
        adapter = _get_browser_adapter()
        result = await adapter.submit_for_target(user, code, target_url, systems)
        raw = dict(result.raw)
        raw.setdefault("success", result.success)
        raw.setdefault("message", result.message)
        raw.update({
            "route": "browser",
            "browser_kind": result.kind.value,
            "elapsed_seconds": result.elapsed_seconds or (time.monotonic() - started),
        })
        if raw.get("success") and domain in {"hi88-freecode.pages.dev", "tangquaqq88.com"}:
            schedule_tracked_task(
                send_submit_success_notification(
                    domain=domain,
                    user=user,
                    code=code,
                    has_points=bool(raw.get("has_points", False)),
                    result_message=str(raw.get("message", "") or ""),
                ),
                bot_state.bg_tasks,
                name=f"submit-success-notify-{domain}-{user}-{code}",
            )
        return raw
    except Exception as exc:
        logger.error(f"❌ [Browser|{domain}] submit lỗi: {exc}")
        return {
            "success": False,
            "message": str(exc),
            "route": "browser",
            "_infra_failure": True,
            "elapsed_seconds": time.monotonic() - started,
        }

async def submit_code_with_delay(
    user: str, code: str, target_url: str, systems: dict,
    queue_wait_ms: float | None = None,
):
    domain = normalize_domain(target_url)
    timer = RequestTimer(
        request_id=f"submit:{domain}:{id(asyncio.current_task())}",
        kind="submit_request",
    )

    ik = (domain, user, code.upper())
    if ik in bot_state._inflight_codes:
        timer.finish("duplicate")
        return {"success": False, "message": "In-flight duplicate"}
    bot_state._inflight_codes.add(ik)

    try:
        async with timer.stage_async("rate_limit_wait"):
            await get_domain_rate_limiter(domain).acquire()

        result = {"success": False, "message": "Not started"}
        submit_started = time.monotonic()
        # Global cap bảo vệ CPU/RAM Edge; semaphore domain vẫn giữ cân bằng
        # giữa các site bên trong global budget.
        global_sem = _get_submit_semaphore()
        domain_sem = get_domain_semaphore(domain)
        async with timer.stage_async("global_semaphore_wait"):
            await global_sem.acquire()
        try:
            async with timer.stage_async("domain_semaphore_wait"):
                await domain_sem.acquire()
            try:
                try:
                    async with timer.stage_async("browser_submit"):
                        submit_timeout = (
                            50.0 if domain in {"tangquaqq88.com", "hi88-freecode.pages.dev"}
                            else 25.0
                        )
                        result = await asyncio.wait_for(
                            submit_code_safe(user, code, target_url, systems),
                            timeout=submit_timeout,
                        )
                except asyncio.TimeoutError:
                    result = {
                        "success": False,
                        "message": f"Timeout {submit_timeout:.0f}s",
                        "_infra_failure": True,
                    }
                except Exception as e:
                    result = {"success": False, "message": str(e), "_infra_failure": True}
            finally:
                domain_sem.release()
        finally:
            global_sem.release()

        if result.get("_infra_failure"):
            elapsed_ms = float(result.get("elapsed_seconds") or (time.monotonic() - submit_started)) * 1000.0
            update_dashboard(
                domain=domain,
                account=user,
                code=code,
                status="INFRA_FAILURE",
                rtt_ms=elapsed_ms,
                raw_response=str(result.get("message") or "browser infrastructure failure"),
            )

        postprocess = result.pop("_outcome_postprocess", None)
        if postprocess is not None:
            try:
                await finalize_outcome(postprocess, systems)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Outcome persistence must not rewrite a result already read
                # from the site or keep browser capacity occupied.
                logger.exception("⚠️ Deferred outcome persistence failed for %s/%s: %s", domain, code, exc)

        if getattr(Config, "SUBMIT_STAGE_TIMING_LOG", True):
            raw_browser_timing = result.get("_browser_timing", {})
            record = {
                "domain": domain,
                "status": ("success" if result.get("success") else
                           "infra_failure" if result.get("_infra_failure") else "not_success"),
                "queue_wait_ms": round(queue_wait_ms, 2) if queue_wait_ms is not None else None,
                "submit_stage_ms": {k: round(v * 1000.0, 2) for k, v in timer.stages.items()},
                "browser_stage_ms": raw_browser_timing.get("stage_ms", {}),
                "browser_network": raw_browser_timing.get("network", {}),
            }
            logger.info("⏱️ [SUBMIT_STAGE] %s", json.dumps(record, ensure_ascii=False, separators=(",", ":")))

        # ✅ RTT FIX: không còn sleep nhân tạo sau submit.
        # Một sleep ở đây chạy SAU KHI domain semaphore đã được release — tức là
        # KHÔNG throttle được gì (task kế tiếp trên cùng domain đã có thể
        # acquire semaphore ngay lập tức), chỉ cộng thêm ~150ms vào MỌI lần
        # đo RTT một cách vô ích. Việc giới hạn tốc độ submit trên mỗi
        # domain đã được đảm nhiệm đầy đủ bởi get_domain_rate_limiter()
        # (token bucket, acquire ở đầu hàm) + get_domain_semaphore()
        # (giới hạn concurrency) — không cần thêm sleep thừa ở cuối.
        return result
    finally:
        timer.finish("done")
        bot_state._inflight_codes.discard(ik)


def track_submit_task(task: asyncio.Task, label: str = ""):
    _active_submit_tasks.add(task)

    def done(t):
        _active_submit_tasks.discard(t)
        try:
            r = t.result()
            if isinstance(r, dict):
                logger.info(f"{'✅' if r.get('success') else '⚠️'} [TASK] {label}")
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"❌ [TASK] {label}: {e}")
        finally:
            _log_separator()

    task.add_done_callback(done)
    return task


# ═══════════════════════════════════════════════════════════════
# ĐĂNG KÝ KÊNH/TÀI KHOẢN
# ═══════════════════════════════════════════════════════════════
def build_unique_account_targets():
    items, seen = [], set()
    for cid, cfg in sorted(Config.CHANNEL_CONFIG.items(), key=lambda x: x[1].get("priority", 999)):
        if not cfg.get("enabled", True):
            continue
        url = cfg["url"]
        d = normalize_domain(url)
        accs = cfg.get("accounts", [])
        if not accs:
            continue
        for a in sorted(accs, key=lambda x: x.get("priority", 999)):
            k = (d, a["username"])
            if k in seen:
                continue
            seen.add(k)
            if _is_account_done_today(d, a["username"]):
                continue
            items.append({"chat_id": cid, "channel_name": cfg.get("name", ""), "target_url": url, "domain": d, "key": f"{d}|{a['username']}", "accounts": [a]})
    return items


async def init_channels_and_accounts():
    bot_state._site_code_seen.clear()
    targets = build_unique_account_targets()
    if not targets:
        logger.error("❌ No channels")
        return
    logger.info(
        "✅ %s target domain+tài khoản duy nhất đăng ký từ %s channel cấu hình",
        len(targets),
        len(Config.CHANNEL_CONFIG),
    )
    logger.info("🤖 BOT RUNNING (BROWSER-ONLY)")


# ═══════════════════════════════════════════════════════════════
# INIT SYSTEMS
# ═══════════════════════════════════════════════════════════════
async def init_systems():
    print_version_info()
    db = init_database(Config.DATABASE_PATH)
    hm, pm = init_monitoring()
    try:
        hm.set_high_memory_callback(_emergency_ram_cleanup)
    except Exception:
        pass
    get_shutdown_handler().setup(bot_state)
    start_history_writer()
    global _durable_inbox
    _durable_inbox = DurableInbox(
        getattr(Config, "TELEGRAM_INBOX_DB_PATH", "data/telegram_inbox.db"),
        lease_seconds=getattr(Config, "TELEGRAM_INBOX_LEASE_SECONDS", 300),
        # ✅ FIX retry-storm: giới hạn số lần thử + backoff tăng dần —
        # xem retry_or_fail() trong durable_inbox.py.
        max_attempts=getattr(Config, "MAX_INBOX_ATTEMPTS", 5),
        retry_base_delay=getattr(Config, "INBOX_RETRY_BASE_DELAY", 2.0),
        retry_max_delay=getattr(Config, "INBOX_RETRY_MAX_DELAY", 120.0),
    )
    recovery_mode = getattr(
        Config,
        "TELEGRAM_INBOX_RECOVERY_MODE",
        "at_least_once",
    )
    loop = asyncio.get_running_loop()
    if recovery_mode == "live_only":
        discarded = await loop.run_in_executor(
            _INBOX_STATE_EXECUTOR,
            _durable_inbox.discard_unfinished,
            "startup_live_only_discard_unfinished",
        )
        logger.warning(
            "🧹 [Inbox] live-only: bỏ qua %s row pending/processing từ phiên trước",
            discarded,
        )
    else:
        reclaimed = await loop.run_in_executor(
            _INBOX_STATE_EXECUTOR,
            _durable_inbox.reclaim_stale,
        )
        if reclaimed:
            logger.warning("♻️ [Inbox] at-least-once: thu hồi %s row bị kẹt", reclaimed)
    logger.info("📥 [Inbox] recovery_mode=%s", recovery_mode)
    return {
        "db": db,
        "inbox": _durable_inbox,
        "performance_monitor": pm,
        "health_monitor": hm,
        # ✅ FIX: finalize_outcome() (submission_outcomes.py) gọi
        # db.record_submission qua run_in_executor(None, ...) — dùng
        # default executor của event loop (chỉ 1 worker mặc định trên
        # nhiều bản Python, dùng CHUNG với mọi run_in_executor(None, ...)
        # khác trong codebase) thay vì pool _DB_EXECUTOR riêng cho DB (8
        # worker) → tranh chấp pool mặc định, có thể làm nghẽn các tác vụ
        # I/O khác đang dùng chung default executor. Truyền _DB_EXECUTOR
        # qua systems để finalize_outcome dùng đúng pool.
        "db_executor": _DB_EXECUTOR,
    }


def _emergency_ram_cleanup():
    try:
        n2 = len(bot_state._site_code_seen)
        bot_state._site_code_seen.clear()
        gc.collect()
        logger.warning(f"🧹 [RAM] seen={n2}")
    except Exception:
        pass


async def verify_telegram_session():
    try:
        me = await client.get_me()
        dc = client.session.dc_id
        logger.info(f"✅ Session: @{me.username or me.id} | DC{dc}")
        return True
    except AuthKeyDuplicatedError:
        raise
    except Exception as e:
        logger.error(f"❌ session: {e}")
        return False


async def sync_telegram_dialogs() -> bool:
    """Load all dialogs so Telethon caches channel entities/access_hashes."""
    if not getattr(Config, "TELEGRAM_SYNC_DIALOGS", True):
        logger.info("⏭️ Skip Telegram dialog sync (TELEGRAM_SYNC_DIALOGS=false)")
        return True

    timeout = max(
        10.0,
        float(getattr(Config, "TELEGRAM_DIALOG_SYNC_TIMEOUT", 120.0)),
    )
    try:
        logger.info("📥 Syncing dialogs để kích hoạt update stream (limit=None)...")
        dialogs = await asyncio.wait_for(
            client.get_dialogs(limit=None),
            timeout=timeout,
        )
        dialog_ids = {
            int(get_peer_id(dialog.entity))
            for dialog in dialogs
            if getattr(dialog, "entity", None) is not None
            and getattr(dialog.entity, "id", None) is not None
        }
        configured = {int(chat_id) for chat_id in Config.CHANNEL_CONFIG}
        cached = len(configured & dialog_ids)
        logger.info(
            "✅ Telegram dialogs synced | total=%s configured_cached=%s/%s",
            len(dialogs),
            cached,
            len(configured),
        )
        missing = sorted(configured - dialog_ids)
        if missing:
            logger.warning(
                "⚠️ Dialog cache còn thiếu %s channel; thử resolve trực tiếp "
                "để làm nóng entity cache",
                len(missing),
            )
            sem = asyncio.Semaphore(8)

            async def resolve_missing(chat_id: int) -> bool:
                async with sem:
                    try:
                        entity = await asyncio.wait_for(
                            client.get_entity(chat_id),
                            timeout=float(
                                getattr(Config, "TELEGRAM_CHANNEL_TIMEOUT", 10.0)
                            ),
                        )
                        return getattr(entity, "id", None) is not None
                    except AuthKeyDuplicatedError:
                        raise
                    except Exception as exc:
                        logger.warning(
                            "⚠️ Không resolve được channel %s trong sync: %s",
                            chat_id,
                            exc,
                        )
                        return False

            resolved = await asyncio.gather(
                *(resolve_missing(chat_id) for chat_id in missing)
            )
            resolved_count = sum(bool(item) for item in resolved)
            logger.info(
                "✅ Direct entity resolve trong sync: %s/%s",
                resolved_count,
                len(missing),
            )

            if resolved_count < len(missing):
                logger.warning(
                    "⚠️ Dialog cache chưa đủ sau direct resolve; tiếp tục khởi động "
                    "để bước verify_channels kiểm tra quyền truy cập thực tế"
                )
        return True
    except Exception as exc:
        logger.error("❌ Telegram dialog sync lỗi: %s", exc)
        return False


async def verify_channels_and_get_ids():
    sem = asyncio.Semaphore(8)
    valid = {}

    async def chk(cid, cfg):
        async with sem:
            try:
                entity = await asyncio.wait_for(
                    client.get_entity(cid),
                    timeout=float(
                        getattr(Config, "TELEGRAM_CHANNEL_TIMEOUT", 10.0)
                    ),
                )
                logger.debug(
                    "✅ [Telegram channel] id=%s name=%s entity=%s",
                    cid,
                    cfg.get("name", ""),
                    getattr(entity, "title", None) or getattr(entity, "username", None) or type(entity).__name__,
                )
                return cid, cfg
            except AuthKeyDuplicatedError:
                raise
            except Exception as e:
                logger.error(
                    "❌ [Telegram channel INVALID/NO ACCESS] id=%s name=%s error=%s",
                    cid,
                    cfg.get("name", ""),
                    e,
                )
                return cid, None

    res = await asyncio.gather(*[chk(c, cfg) for c, cfg in Config.CHANNEL_CONFIG.items()])
    for cid, cfg in res:
        if cfg is not None:
            valid[cid] = cfg
    logger.info(
        "📋 %s/%s channels valid | handler will subscribe by numeric chat_id (not name)",
        len(valid),
        len(Config.CHANNEL_CONFIG),
    )
    return valid


# ═══════════════════════════════════════════════════════════════
# OCR
# ═══════════════════════════════════════════════════════════════
def _strip_promo_label_lines(t: str) -> str:
    lines = t.split("\n")
    kept = []
    skip = False
    for ln in lines:
        s = ln.strip()
        if skip:
            skip = False
            continue
        if _PROMO_LABEL_INLINE_RE.match(s):
            continue
        if _PROMO_LABEL_ONLY_RE.match(s):
            skip = True
            continue
        kept.append(ln)
    return "\n".join(kept)


def _ocr_candidates_for_line(line: str, *, strict_xx88: bool = False) -> list[str]:
    """Return code-shaped candidates without concatenating banner text.

    OCR commonly returns lines such as ``CODE: ABC123`` or ``MÃ KHUYẾN MÃI
    ABC123``. Validating the whole line would either join label+code or allow
    a long banner to reach the validator. XX88 therefore validates each
    alphanumeric token independently; the normal path keeps the legacy
    line-oriented behavior for sites that may use special characters.
    """
    raw = (line or "").strip()
    if not raw:
        return []
    if not strict_xx88:
        return [raw]
    candidates = _ALNUM_TOKEN_RE.findall(raw)
    if not candidates and raw.isalnum():
        candidates = [raw]
    return list(dict.fromkeys(candidates))


def _is_xx88_ocr_banner_token(candidate: str) -> bool:
    """Reject known XX88 promo/banner words before generic code validation."""
    return str(candidate or "").strip().upper() in _XX88_OCR_BANNER_TOKENS


def _is_qq88_ocr_noise_token(candidate: str) -> bool:
    """Reject QQ88 game names and UI labels after OCR normalization.

    OCR may split labels (``Bet VND``) or join them (``BetVND``), so matching
    is performed on a compact uppercase representation before applying the
    regex. This is scoped to QQ88 image OCR only.
    """
    compact = re.sub(r"[^A-Z0-9]", "", str(candidate or "").upper())
    return bool(compact and _QQ88_OCR_NOISE_RE.search(compact))


async def process_image_from_telegram(event, channel_config, systems):
    if not _is_ocr_allowed_channel(getattr(event, "chat_id", None)):
        logger.info("⏭️ [OCR] Bỏ qua: channel không nằm trong OCR_ALLOWED_CHANNEL_IDS")
        return {"success": False, "codes": [], "message": "OCR channel not allowed", "text": ""}
    if getattr(Config, "PAUSE_OCR_ON_HIGH_CPU", True):
        try:
            import monitoring as mon

            while True:
                hm = getattr(mon, "_health_monitor", None)
                if not hm or not getattr(hm, "pause_ocr", False):
                    break
                await asyncio.sleep(0.5)
        except Exception:
            pass
    async with _get_ocr_semaphore():
        return await _process_image_inner(event, channel_config, systems)


async def _process_image_inner(event, channel_config, systems):
    target_url = channel_config.get("url", "")
    req_t = get_current_timer()
    image_started = time.perf_counter()
    ocr_started = None
    size_bytes = None
    dl = None
    img_path = None

    try:
        logger.info("📸 [OCR] processing")
        tmp = tempfile.mkdtemp(prefix="ocr_")
        try:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            report_download_started()
            try:
                if req_t:
                    async with req_t.stage_async("telegram_download"):
                        dl = await _media_download_manager.download(event, tmp)
                else:
                    dl = await _media_download_manager.download(event, tmp)
            finally:
                report_download_finished()

            img_path = dl.path
            size_bytes = dl.size_bytes

            if dl.dedup_hit:
                return {"success": False, "codes": [], "message": "dedup", "text": ""}
            if not img_path:
                return {"success": False, "codes": [], "message": "download fail", "text": ""}

            orig_ext = Path(img_path).suffix or ".jpg"
            uniq = Path(tmp) / f"ocr_{ts}{orig_ext}"
            Path(img_path).rename(uniq)
            img_path = str(uniq)

            loop = asyncio.get_running_loop()
            # RapidOCR/ONNXRuntime có thể mất 1–3 giây khi khởi tạo lần đầu.
            # Không gọi đồng bộ trên event loop vì sẽ làm trễ Telegram updates.
            ext = await loop.run_in_executor(_OCR_EXECUTOR, get_image_extractor)
            if ext is None:
                return {"success": False, "codes": [], "message": "OCR not init", "text": ""}

            crop = channel_config.get("ocr_crop")
            ocr_crops = channel_config.get("ocr_crops") or ([crop] if crop else [None])
            is_video = orig_ext.lower() in VIDEO_FORMATS

            if is_video:
                fs = channel_config.get("ocr_frame_seconds")

                def _frames():
                    return extract_frames_from_video(
                        img_path,
                        tmp,
                        max_frames=max(2, int(getattr(Config, "VIDEO_OCR_MAX_FRAMES", 3))),
                        # Khi có nhiều vùng, giữ frame đầy đủ rồi crop riêng
                        # từng vùng ở bước OCR bên dưới.
                        crop_box=None if len(ocr_crops) > 1 else crop,
                        frame_seconds=fs,
                    )

                if req_t:
                    async with req_t.stage_async("video_frame_extraction"):
                        frame_paths = await loop.run_in_executor(_MEDIA_EXECUTOR, _frames)
                else:
                    frame_paths = await loop.run_in_executor(_MEDIA_EXECUTOR, _frames)

                if not frame_paths:
                    return {"success": False, "codes": [], "message": "no frames", "text": ""}
                targets = frame_paths
            else:
                targets = [img_path]

            async def run_ocr(paths, already_cropped):
                crop_boxes = [None] if already_cropped else ocr_crops
                tasks = [
                    loop.run_in_executor(
                        _OCR_EXECUTOR,
                        ext.extract_code_from_image,
                        path,
                        "eng",
                        crop_box,
                    )
                    for path in paths
                    for crop_box in crop_boxes
                ]
                results = await asyncio.gather(*tasks, return_exceptions=True)
                return [
                    result.strip()
                    for result in results
                    if isinstance(result, str) and result.strip()
                ]

            use_fast = (
                bool(getattr(Config, "OCR_FAST_PATH", True))
                and bool((channel_config or {}).get("ocr_single_code_mode", False))
                and len(targets) > 1
                and len(ocr_crops) == 1
            )

            ocr_started = time.perf_counter()
            if use_fast:
                min_chars = max(1, int(getattr(Config, "OCR_FAST_MIN_CHARS", 8)))
                crop_for_ocr = None if is_video else crop

                _, text = await ext.extract_first_valid_code(
                    targets,
                    crop_box=crop_for_ocr,
                    already_cropped=is_video,
                    is_valid_fn=lambda value: len((value or "").replace("\n", "")) >= min_chars,
                )

                per_frame = [text] if text else []
            else:
                if req_t:
                    async with req_t.stage_async("ocr"):
                        per_frame = await run_ocr(targets, is_video)
                else:
                    per_frame = await run_ocr(targets, is_video)

            extracted = "\n".join(per_frame)

            if not extracted:
                return {"success": False, "codes": [], "message": "no text", "text": ""}

            group = get_filter_group_name(target_url)
            is_xx88 = normalize_domain(target_url) == "xx88code.com"
            is_qq88 = normalize_domain(target_url) == "tangquaqq88.com"

            logger.info(f"✅ [OCR] {len(extracted)} chars")

            codes = []

            if not codes:
                use_cons = (
                    len(per_frame) >= 2
                    and bool((channel_config or {}).get("ocr_require_consensus", True))
                )
                seen_idx = {}
                sample = {}
                for i, ft in enumerate(per_frame):
                    ft = _strip_promo_label_lines(ft)
                    for ln in ft.split("\n"):
                        ln = ln.strip()
                        if len(ln) < 4:
                            continue
                        for candidate in _ocr_candidates_for_line(ln, strict_xx88=is_xx88):
                            c = CodeValidator.clean_code(candidate)
                            if not c or len(c) < 4:
                                continue
                            if is_xx88 and _is_xx88_ocr_banner_token(c):
                                logger.debug("⏭️ [OCR|XX88] bỏ token banner/promo: %s", c)
                                continue
                            if is_qq88 and _is_qq88_ocr_noise_token(c):
                                logger.debug("⏭️ [OCR|QQ88] bỏ tên game/nhãn giao diện: %s", c)
                                continue
                            if group == "multi_site_strict":
                                c = c.upper()
                            try:
                                v = CodeValidator.validate_code(c, target_url=target_url, source="image_ocr")
                            except TypeError:
                                v = CodeValidator.validate_code(c, target_url)
                            if not v["valid"]:
                                continue
                            k = c.upper()
                            seen_idx.setdefault(k, set()).add(i)
                            sample.setdefault(k, (c, ln))
                for k, idxs in seen_idx.items():
                    c, ln = sample[k]
                    n = len(idxs)
                    if use_cons and n < 2:
                        continue
                    codes.append({"code": c, "raw": ln, "confidence": 0.9 if n < 2 else 0.97})
                    logger.info(f"✅ [OCR] {c}")

            single = (channel_config or {}).get("ocr_single_code_mode", False)
            if single and len(codes) > 1:
                codes = [codes[0]]

            if codes:
                max_ocr = int(
                    (channel_config or {}).get(
                        "max_ocr_codes_per_batch",
                        getattr(Config, "MAX_OCR_CODES_PER_BATCH", 4),
                    )
                )
                if len(codes) > max_ocr:
                    codes = codes[:max_ocr]
                if not codes:
                    return {"success": False, "codes": [], "message": "too many", "text": extracted}
                return {"success": True, "codes": codes, "message": f"{len(codes)} codes", "text": extracted}
            return {"success": False, "codes": [], "message": "no valid", "text": extracted}
        finally:
            total_elapsed_ms = (time.perf_counter() - image_started) * 1000.0
            ocr_elapsed_ms = (
                (time.perf_counter() - ocr_started) * 1000.0
                if ocr_started is not None
                else None
            )
            logger.debug(
                "⏱️ [OCR-TIMING] image=%s domain=%s total_ms=%.1f ocr_ms=%s "
                "download_ms=%s frames=%s size_bytes=%s",
                Path(img_path).name if img_path else "(download-failed)",
                normalize_domain(target_url),
                total_elapsed_ms,
                f"{ocr_elapsed_ms:.1f}" if ocr_elapsed_ms is not None else "n/a",
                f"{getattr(dl, 'elapsed_ms', 0.0):.1f}" if dl is not None else "n/a",
                len(locals().get("targets", []) or []),
                size_bytes if size_bytes is not None else "n/a",
            )
            if req_t:
                req_t.emit("media_complete", media=True, file_size_bytes=size_bytes, media_extension=Path(img_path).suffix.lower() if img_path else None, download_elapsed_ms=getattr(dl, "elapsed_ms", None), download_bytes_per_sec=getattr(dl, "bytes_per_sec", None), download_attempts=getattr(dl, "attempts", None), download_dedup_hit=getattr(dl, "dedup_hit", None))
            try:
                shutil.rmtree(tmp)
            except Exception:
                pass
    except Exception as e:
        logger.error(f"❌ [OCR] {e}\n{traceback.format_exc()}")
        return {"success": False, "codes": [], "message": f"OCR error: {e}", "text": ""}


async def _submit_one_ocr_code(idx, code, user, target_url, domain, systems):
    try:
        r = await submit_code_with_delay(user, code, target_url, systems)
        s = r.get("success", False) if r else False
        hp = r.get("has_points", False) if r else False
        wc = r.get("is_wrong_code", False) if r else False
        if s and hp:
            st = "SUCCESS_POINTS"
        elif s and not hp:
            st = "SUCCESS_NO_POINTS"
        elif s is False and wc:
            st = "FAILED"
        elif r and r.get("rate_limited"):
            st = "RATE_LIMITED"
        elif r and r.get("_infra_failure"):
            st = "INFRA_FAILURE"
        else:
            st = "NO_RESULT"
        await _mark_code_used_if_final(domain, code, st)
        logger.info(f"  {'✅' if s else '❌'} [OCR#{idx}] {code} | {st}")
        return st
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.error(f"  ❌ [OCR#{idx}] {e}")
        return "INFRA_FAILURE"


async def submit_codes_from_image(
    user,
    codes_data,
    target_url,
    channel_config,
    systems,
    inbox_id=None,
    claim_token=None,
):
    if not codes_data:
        if inbox_id and _durable_inbox is not None:
            await asyncio.get_running_loop().run_in_executor(
                _INBOX_EXECUTOR,
                _durable_inbox.mark_ignored,
                int(inbox_id),
                "ocr_no_code",
                claim_token,
            )
        return
    domain = normalize_domain(target_url)
    db = systems.get("db") if systems else None
    domain_accounts = sorted(
        _domain_accounts.get(domain, []),
        key=lambda account: account.get("priority", 999),
    )
    fanout_users = [
        account.get("username")
        for account in domain_accounts[:_code_fanout_count(domain, domain_accounts)]
        if account.get("username")
    ] or ([user] if user else [])
    logger.info(f"📤 [IMG] {len(codes_data)} codes × {len(fanout_users)} account")
    code_values = [str(it.get("code", "")).strip() for it in codes_data if str(it.get("code", "")).strip()]
    unused_codes = None
    if db is not None and code_values:
        try:
            loop = asyncio.get_running_loop()
            unused_codes = await loop.run_in_executor(_DB_EXECUTOR, db.unused_codes, domain, code_values)
        except Exception as e:
            logger.error("❌ [IMG] Không kiểm tra được batch dedup, bỏ qua toàn bộ batch: %s", e)
            unused_codes = set()
    tasks = []
    for i, it in enumerate(codes_data, 1):
        code = it.get("code", "").strip()
        if not code:
            continue
        if unused_codes is not None and code.upper() not in unused_codes:
            continue
        for account_index, account_user in enumerate(fanout_users, 1):
            tasks.append(
                _submit_one_ocr_code(
                    f"{i}.{account_index}",
                    code,
                    account_user,
                    target_url,
                    domain,
                    systems,
                )
            )
    if not tasks:
        if inbox_id and _durable_inbox is not None:
            await asyncio.get_running_loop().run_in_executor(
                _INBOX_EXECUTOR,
                _durable_inbox.mark_ignored,
                int(inbox_id),
                "ocr_no_code",
                claim_token,
            )
        return
    start = time.time()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    report_batch_submit(len(tasks), (time.time() - start) * 1000)
    if inbox_id and _durable_inbox is not None:
        statuses = [r for r in results if isinstance(r, str)]
        loop = asyncio.get_running_loop()
        if statuses and all(r.startswith("SUCCESS") for r in statuses):
            await loop.run_in_executor(
                _INBOX_EXECUTOR,
                _durable_inbox.complete_item,
                int(inbox_id),
                claim_token,
            )
        elif any(r == "INFRA_FAILURE" for r in statuses):
            await loop.run_in_executor(
                _INBOX_EXECUTOR,
                _durable_inbox.retry_or_fail,
                int(inbox_id),
                "ocr/browser infrastructure unavailable",
                10,
                claim_token,
            )
        elif any(r == "RATE_LIMITED" for r in statuses):
            await loop.run_in_executor(
                _INBOX_EXECUTOR,
                _durable_inbox.retry_or_fail,
                int(inbox_id),
                "media submit rate-limited",
                None,
                claim_token,
            )
        elif any(r == "NO_RESULT" for r in statuses) or len(statuses) != len(tasks):
            # NO_RESULT thường là captcha/không có popup kết quả hoặc mã đã
            # hết hạn. Retry durable vô hạn sẽ tải lại cùng media và spam cùng
            # mã cũ. Kết thúc row ở trạng thái failed để không replay
            # (mark_failed ở đây là chủ ý — khác domain_code_worker, cả lô
            # code của 1 ảnh được coi là 1 đơn vị công việc duy nhất).
            await loop.run_in_executor(
                _INBOX_EXECUTOR,
                _durable_inbox.mark_failed,
                int(inbox_id),
                "ocr/browser no result - terminal, no replay",
                claim_token,
            )
        else:
            await loop.run_in_executor(
                _INBOX_EXECUTOR,
                _durable_inbox.mark_failed,
                int(inbox_id),
                "ocr/browser failed",
                claim_token,
            )


# ═══════════════════════════════════════════════════════════════
# MESSAGE PROCESSING
# ═══════════════════════════════════════════════════════════════
async def _ignore_inbox_event(event, reason: str) -> None:
    inbox_id = getattr(event, "inbox_id", None)
    claim_token = getattr(event, "claim_token", None)
    if inbox_id and _durable_inbox is not None:
        await asyncio.get_running_loop().run_in_executor(
            _INBOX_EXECUTOR,
            _durable_inbox.mark_ignored,
            int(inbox_id),
            reason,
            claim_token,
        )


async def _run_media_pipeline_item(item: dict) -> None:
    event = item["event"]
    cfg = item["channel_config"]
    account = item["account"]
    target_url = item["target_url"]
    inbox_id = getattr(event, "inbox_id", None)
    claim_token = getattr(event, "claim_token", None)
    async with _get_media_pipeline_semaphore():
        try:
            result = await process_image_from_telegram(event, cfg, _systems)
            if result.get("success"):
                await submit_codes_from_image(
                    account, result["codes"], target_url, cfg, _systems,
                    inbox_id, claim_token,
                )
            else:
                await _ignore_inbox_event(event, "ocr_no_result")
        except asyncio.CancelledError:
            # ✅ FIX: a cancellation during shutdown is an operational
            # requeue, not a submit failure — retry_or_fail() would count
            # against max_attempts and could eventually mark_failed() a row
            # that never actually failed to submit, purely because the bot
            # happened to restart several times while it was in flight.
            if inbox_id and _durable_inbox is not None:
                try:
                    requeued = await asyncio.get_running_loop().run_in_executor(
                        _INBOX_STATE_EXECUTOR,
                        _durable_inbox.requeue_claimed_for_shutdown,
                        int(inbox_id),
                        claim_token,
                        "media pipeline cancelled during shutdown",
                    )
                    if requeued:
                        logger.warning(
                            "♻️ [Inbox] requeue media row=%s vì pipeline bị hủy",
                            inbox_id,
                        )
                except Exception:
                    logger.exception("❌ cannot requeue cancelled media inbox row=%s", inbox_id)
            raise
        except Exception as exc:
            logger.exception("❌ media pipeline %s failed: %s", cfg.get("name", ""), exc)
            if inbox_id and _durable_inbox is not None:
                try:
                    await asyncio.get_running_loop().run_in_executor(
                        _INBOX_EXECUTOR, _durable_inbox.mark_failed,
                        int(inbox_id), f"media pipeline: {exc}", claim_token,
                    )
                except Exception:
                    logger.exception("❌ cannot mark media inbox row=%s failed", inbox_id)


async def _media_pipeline_worker(worker_id: int) -> None:
    queue = _media_pipeline_queue
    if queue is None:
        return
    logger.info("🖼️ Media pipeline worker #%s ready", worker_id)
    while bot_state.is_running or not queue.empty():
        try:
            item = await asyncio.wait_for(queue.get(), timeout=1.0)
        except asyncio.TimeoutError:
            continue
        except asyncio.CancelledError:
            break
        try:
            await _run_media_pipeline_item(item)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("❌ media pipeline worker #%s error", worker_id)
        finally:
            queue.task_done()


async def _retry_dropped_media_pipeline_item(item: dict) -> None:
    event = item.get("event") if isinstance(item, dict) else None
    inbox_id = getattr(event, "inbox_id", None)
    claim_token = getattr(event, "claim_token", None)
    if inbox_id and _durable_inbox is not None:
        try:
            await asyncio.get_running_loop().run_in_executor(
                _INBOX_STATE_EXECUTOR, _durable_inbox.retry_or_fail,
                int(inbox_id), "media pipeline queue overflow/drop", 5, claim_token,
            )
        except Exception:
            logger.exception("❌ cannot retry dropped media inbox row=%s", inbox_id)


def _on_media_pipeline_drop(item: dict) -> None:
    try:
        schedule_tracked_task(
            _retry_dropped_media_pipeline_item(item),
            bot_state.bg_tasks,
            name="retry-dropped-media-pipeline",
        )
    except RuntimeError:
        logger.error("❌ cannot schedule retry for dropped media queue item")


def _ensure_media_pipeline_workers() -> None:
    global _media_pipeline_queue
    if _media_pipeline_queue is None:
        _media_pipeline_queue = asyncio.Queue(
            maxsize=max(1, int(getattr(Config, "MEDIA_PIPELINE_QUEUE_MAXSIZE", 200)))
        )
        qm = get_queue_manager()
        if qm is not None:
            qm.register("media_pipeline", _media_pipeline_queue, on_drop=_on_media_pipeline_drop)
    if any(not task.done() for task in _media_pipeline_workers):
        return
    worker_count = max(1, int(getattr(Config, "MAX_CONCURRENT_MEDIA_PIPELINES", 4)))
    _media_pipeline_workers.clear()
    for worker_id in range(1, worker_count + 1):
        task = asyncio.create_task(
            _media_pipeline_worker(worker_id), name=f"media-pipeline-{worker_id}"
        )
        _media_pipeline_workers.append(task)
        bot_state.bg_tasks.add(task)
        task.add_done_callback(bot_state.bg_tasks.discard)


async def _enqueue_media_pipeline(event, channel_config: dict, account: str, target_url: str) -> None:
    _ensure_media_pipeline_workers()
    assert _media_pipeline_queue is not None
    await _media_pipeline_queue.put({
        "event": event,
        "channel_config": channel_config,
        "account": account,
        "target_url": target_url,
        "enqueued_at": time.perf_counter(),
    })


async def _process_telegram_message_impl(event):
    if not _systems:
        await _ignore_inbox_event(event, "systems_not_ready")
        return
    if event.chat_id not in Config.CHANNEL_CONFIG:
        await _ignore_inbox_event(event, "channel_not_configured")
        return
    cfg = Config.CHANNEL_CONFIG.get(event.chat_id)
    if not cfg:
        await _ignore_inbox_event(event, "channel_config_missing")
        return
    if not cfg.get("enabled", True):
        await _ignore_inbox_event(event, "channel_disabled")
        return

    msg_date = getattr(event.message, "date", None)
    if msg_date is None:
        await _ignore_inbox_event(event, "message_date_missing")
        return

    if msg_date.tzinfo is None:
        msg_date = msg_date.replace(tzinfo=timezone.utc)

    target_url = cfg["url"]
    accounts = cfg["accounts"]
    raw_text = (event.message.text or event.message.message or "").strip()
    group = get_filter_group_name(target_url)

    _log_separator()
    logger.info(f"📨 [{cfg['name']}] | group={group} | {len(raw_text)} chars")

    # KJC SPECIAL MODE
    if (
        getattr(Config, "KJC_SPECIAL_MODE", True)
        and is_kjc_special_channel(event.chat_id)
    ):
        kjc_codes = extract_kjc_spoiler_codes(event)
        if not kjc_codes:
            # Không kết thúc sớm: tin KJC không có spoiler vẫn phải đi qua
            # luồng chung để đọc text/caption. Nếu có media, luồng chung sẽ
            # chỉ tải/OCR khi channel nằm trong whitelist OCR; nếu không thì
            # đánh dấu đã kiểm tra mà không tải file.
            logger.info("ℹ️ [KJC] Không có spoiler code — chuyển sang text/caption/media policy")
        else:
            channel_name = cfg.get("name", "")
            labeled_domains = detect_kjc_labeled_domains(event, raw_text)
            kjc_items = build_kjc_broadcast_items(kjc_codes, channel_name, labeled_domains)

            logger.info(
                "📡 [KJC] phát hiện %s code, route=%s, sang %s domain",
                len(kjc_codes),
                ",".join(labeled_domains) if labeled_domains else "broadcast-all",
                len(labeled_domains) if labeled_domains else len(_KJC_BROADCAST_DOMAINS),
            )

            eligible_kjc = []
            for item in kjc_items:
                item["inbox_id"] = getattr(event, "inbox_id", None)
                item["claim_token"] = getattr(event, "claim_token", None)
                eligible_kjc.append(item)

            inbox_id = getattr(event, "inbox_id", None)
            fanout_kjc = []
            for item in eligible_kjc:
                fanout_kjc.extend(
                    _fanout_code_item(
                        item,
                        _code_fanout_count(item["domain"]),
                    )
                )

            if inbox_id and fanout_kjc and _durable_inbox is not None:
                await asyncio.get_running_loop().run_in_executor(
                    _INBOX_EXECUTOR,
                    _durable_inbox.set_remaining,
                    int(inbox_id),
                    len(fanout_kjc),
                    getattr(event, "claim_token", None),
                )

            queued_kjc = len(fanout_kjc)
            for item in fanout_kjc:
                item_domain = item["domain"]

                q = get_domain_queue(item_domain)
                item["_queue_enqueued_perf"] = time.perf_counter()
                await q.put(item)
                logger.info("📥 [KJC] %s -> %s", item["code"], item_domain)

            if inbox_id and not queued_kjc:
                await _ignore_inbox_event(event, "kjc_queue_empty")
            return

    cached_codes = None
    # Message text-only không đi qua nhánh media/caption bên dưới nhưng vẫn
    # dùng biến này khi quyết định có cần OCR hay không.
    found_caption = False

    event_media = getattr(event, "media", None) or getattr(
        getattr(event, "message", None), "media", None
    )
    if event_media:
        raw_text = ""
        is_video_ch = bool(cfg.get("has_video", False))
        _m = getattr(event.message, "media", None)
        is_vid = (
            isinstance(_m, MessageMediaDocument)
            and any(isinstance(a, DocumentAttributeVideo) for a in getattr(getattr(_m, "document", None), "attributes", []))
        )
        # Ảnh/video + Telegram spoiler: bắt entity spoiler trước mọi
        # caption/text và trước khi xét OCR. Đây là fast path cho mọi media
        # có mã che spoiler, không phân biệt loại media.
        media_spoiler_started = time.perf_counter()
        media_spoiler_codes = extract_spoiler_codes(event, target_url)
        if media_spoiler_codes:
            cached_codes = unique_keep_order(media_spoiler_codes)
            media_spoiler_elapsed_ms = (time.perf_counter() - media_spoiler_started) * 1000.0
            logger.warning(
                "🎯 [MEDIA-SPOILER FAST] %.1f ms | %s: %s codes %s — bỏ qua caption/OCR",
                media_spoiler_elapsed_ms,
                cfg.get("name", ""),
                len(cached_codes),
                cached_codes,
            )
            # Không đọc caption ở nhánh này; phần dedup/queue dùng cached_codes
            # bên dưới giống đường text thông thường.
            event_media = None

        if event_media is None and cached_codes is not None:
            pass
        elif event_media:
            
            # Telethon có thể đặt caption ở .text thay vì .message, nhất là
            # media/channel post có spoiler entity. Chỉ đọc caption khi
            # fast path spoiler không tìm thấy code.
            caption = (
                getattr(event.message, "message", None)
                or getattr(event.message, "text", None)
                or ""
            ).strip()
            if caption:
                ex = extract_codes_from_message(event, caption, target_url, channel_name=cfg.get("name", ""))
                if ex:
                    raw_text = caption
                    found_caption = True
                    cached_codes = ex

        if cached_codes is None:
            # Mọi channel đều ưu tiên spoiler/text. OCR media chỉ chạy theo
            # danh sách channel fallback và điều kiện link riêng của HI88.
            ocr_allowed = _is_ocr_allowed_channel(event.chat_id)
            if found_caption:
                pass
            elif "hi88-freecode.pages.dev" in target_url.lower() and not _has_hi88_code_link(event):
                logger.info(f"⏭️ [{cfg.get('name')}] HI88 media khuyến mãi không có link nhập code — bỏ qua OCR")
                await _ignore_inbox_event(event, "hi88_media_without_code_link")
                return
            elif not ocr_allowed:
                logger.info(f"⏭️ [{cfg.get('name')}] media/OCR không được phép — chỉ xử lý spoiler/text")
                await _ignore_inbox_event(event, "media_not_ocr_allowed")
                return
            else:
                # Ảnh của kênh OCR whitelist luôn được xử lý. Cờ video chỉ chặn
                # video, không được chặn ảnh thường của PHÁT CODE XX88.
                if is_vid and cfg.get("ocr_video_enabled") is False:
                    await _ignore_inbox_event(event, "ocr_video_disabled")
                    return
                acc = accounts[0]["username"] if accounts else None
                if not acc:
                    await _ignore_inbox_event(event, "ocr_no_account")
                    return
                if is_video_ch or is_vid:
                    if getattr(event, "inbox_id", None) and _durable_inbox is not None:
                        await asyncio.get_running_loop().run_in_executor(
                            _INBOX_EXECUTOR,
                            _durable_inbox.set_remaining,
                            int(event.inbox_id),
                            1,
                            getattr(event, "claim_token", None),
                        )
                    await _enqueue_media_pipeline(event, cfg, acc, target_url)
                    return
                if not raw_text:
                    c2 = (getattr(event.message, "message", None) or "").strip()
                    if c2:
                        ex = extract_codes_from_message(event, c2, target_url, channel_name=cfg.get("name", ""))
                        if ex:
                            raw_text = c2
                        else:
                            if getattr(event, "inbox_id", None) and _durable_inbox is not None:
                                await asyncio.get_running_loop().run_in_executor(
                                    _INBOX_EXECUTOR,
                                    _durable_inbox.set_remaining,
                                    int(event.inbox_id),
                                    1,
                                    getattr(event, "claim_token", None),
                                )
                            await _enqueue_media_pipeline(event, cfg, acc, target_url)
                            return
                    else:
                        if getattr(event, "inbox_id", None) and _durable_inbox is not None:
                            await asyncio.get_running_loop().run_in_executor(
                                _INBOX_EXECUTOR,
                                _durable_inbox.set_remaining,
                                int(event.inbox_id),
                                1,
                                getattr(event, "claim_token", None),
                            )
                        await _enqueue_media_pipeline(event, cfg, acc, target_url)
                        return
    msg_ts = event.message.date
    delay = measure_telegram_delay_fast(msg_ts)
    mk = (event.chat_id, event.message.id)
    # Keep processing-level dedup aligned with ingress dedup.  In particular,
    # media messages may set raw_text="" on the spoiler fast path, while the
    # caption/entity signature still distinguishes a newly edited spoiler.
    ch = _message_event_fingerprint(event.message)
    prev = bot_state._processed_message_hashes.get(mk)
    if prev and prev[0] == ch:
        return
    bot_state._processed_message_hashes[mk] = (ch, time.time())

    final_codes = (cached_codes if cached_codes is not None else extract_codes_from_message(event, raw_text, target_url, channel_name=cfg.get("name", "")))
    if not final_codes:
        logger.info(
            "⏭️ [No code] chat_id=%s name=%s group=%s media=%s text_chars=%s "
            "ocr_allowed=%s",
            getattr(event, "chat_id", None),
            cfg.get("name", ""),
            group,
            bool(
                getattr(event, "media", None)
                or getattr(getattr(event, "message", None), "media", None)
            ),
            len(raw_text),
            _is_ocr_allowed_channel(getattr(event, "chat_id", None)),
        )
        await _ignore_inbox_event(event, "no_code")
        return

    logger.info(f"✅ [{cfg['name']}] codes={final_codes}")
    for c in final_codes:
        append_code_history(event_type="DETECTED", code=c, target_url=target_url, channel=cfg.get("name", ""), source="telegram", status="PENDING", telegram_delay=delay)

    domain = normalize_domain(target_url)
    dedup = []
    db = _systems["db"] if _systems else None
    if db is not None:
        try:
            loop = asyncio.get_running_loop()
            unused = await loop.run_in_executor(_DB_EXECUTOR, db.unused_codes, domain, final_codes)
            dedup = [c for c in final_codes if str(c).strip().upper() in unused]
        except Exception as e:
            logger.error(
                "❌ Không kiểm tra được batch dedup, bỏ qua code để tránh submit trùng: %s",
                e,
            )
    else:
        dedup = list(final_codes)
    if not dedup:
        await _ignore_inbox_event(event, "duplicate_or_used_code")
        return

    avail = sorted(accounts, key=lambda a: a.get("priority", 999))
    if not avail:
        await _ignore_inbox_event(event, "no_account")
        return

    q = get_domain_queue(domain)
    cn = cfg.get("name", "")
    inbox_id = getattr(event, "inbox_id", None)
    fanout_count = _code_fanout_count(domain, accounts)
    dispatch_items = []
    for c in dedup:
        dispatch_items.extend(
            _fanout_code_item(
                {
                    "code": c,
                    "channel_name": cn,
                    "inbox_id": inbox_id,
                    "claim_token": getattr(event, "claim_token", None),
                },
                fanout_count,
            )
        )
    n = len(dispatch_items)
    if inbox_id and n and _durable_inbox is not None:
        await asyncio.get_running_loop().run_in_executor(
            _INBOX_EXECUTOR,
            _durable_inbox.set_remaining,
            int(inbox_id),
            n,
            getattr(event, "claim_token", None),
        )
    for item in dispatch_items:
        # Cùng một code được đưa vào queue fanout_count lần để các worker
        # reserve các account khác nhau và submit song song.
        item["_queue_enqueued_perf"] = time.perf_counter()
        await q.put(item)
    logger.info(
        f"📥 {len(dedup)} code × {fanout_count} account = {n} lượt → '{domain}' (q={q.qsize()})"
    )
    if inbox_id and not n:
        # ✅ FIX: retry_or_fail (bound) thay vì retry() vô hạn.
        await asyncio.get_running_loop().run_in_executor(
            _INBOX_EXECUTOR,
            _durable_inbox.retry_or_fail,
            int(inbox_id),
            "domain queue full",
            None,
            getattr(event, "claim_token", None),
        )


async def process_telegram_message(event):
    timer = RequestTimer.from_event(event, kind="telegram_request")
    tok = set_current_timer(timer)
    cfg = Config.CHANNEL_CONFIG.get(getattr(event, "chat_id", None))
    tag = get_site_log_tag(cfg.get("url", "")) if cfg else f"chat_{getattr(event,'chat_id','?')}"
    lt = set_log_context(tag)
    try:
        with timer.stage("process_message"):
            r = await _process_telegram_message_impl(event)
        timer.finish("ok")
        return r
    except asyncio.CancelledError:
        timer.finish("cancelled")
        raise
    except Exception as e:
        timer.finish("error", error_type=type(e).__name__)
        raise
    finally:
        reset_current_timer(tok)
        reset_log_context(lt)


# ═══════════════════════════════════════════════════════════════
# MESSAGE WORKERS
# ═══════════════════════════════════════════════════════════════
async def message_worker(wid: int):
    logger.info(f"👷 Worker #{wid}")

    # During graceful shutdown, finish items already present in the queue;
    # new Telegram ingress is stopped by the shutdown path.  A hard timeout
    # below still prevents shutdown from waiting forever on a stuck browser.
    while bot_state.is_running or (message_queue is not None and not message_queue.empty()):
        row_id = None
        claimed_row_id = None
        row = None
        claimed_token = None
        try:
            item = await asyncio.wait_for(message_queue.get(), timeout=1.0)
            if isinstance(item, tuple) and len(item) == 2:
                queued_at, row_id = item
                queue_age_ms = (time.perf_counter() - queued_at) * 1000
                if queue_age_ms > 500:
                    logger.warning(
                        "🐌 Telegram message queue delay %.0fms | qsize=%s/%s",
                        queue_age_ms,
                        message_queue.qsize(),
                        message_queue.maxsize,
                    )
            else:
                row_id = item
        except asyncio.TimeoutError:
            continue
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"❌ worker#{wid} lấy message lỗi: {e}")
            await asyncio.sleep(0.2)
            continue

        try:
            async with _get_proc_semaphore():
                inbox = _durable_inbox
                if inbox is None:
                    bot_state._inbox_enqueued_ids.discard(int(row_id))
                    logger.warning(
                        "⚠️ [Worker #%s] DurableInbox chưa sẵn sàng — "
                        "giữ row=%s để drain lại sau khi inbox khởi tạo",
                        wid,
                        row_id,
                    )
                    continue
                bot_state._inbox_enqueued_ids.discard(int(row_id))
                row = await asyncio.get_running_loop().run_in_executor(_INBOX_EXECUTOR, inbox.claim, int(row_id))
                if not row:
                    continue
                claimed_row_id = int(row_id)
                claimed_token = row.get("claim_token")
                # A row in DurableInbox is proof that this process already
                # accepted the event.  Process it even when
                # TELEGRAM_CATCH_UP=false; that flag only prevents replaying
                # Telegram history, not recovery of locally durable events.
                # Luồng realtime đã giữ message object từ event Telethon;
                # chỉ gọi lại Telegram API khi cache miss (recovery sau restart
                # hoặc item đã nằm quá lâu trong durable inbox).
                msg = _inbox_message_cache.pop(int(row_id), None)
                if msg is None:
                    msg = await client.get_messages(int(row["chat_id"]), ids=int(row["message_id"]))
                if not msg:
                    # ✅ FIX: retry_or_fail (bound) thay vì retry() vô hạn —
                    # tránh 1 message_id không lấy lại được lặp mãi.
                    await asyncio.get_running_loop().run_in_executor(
                        _INBOX_EXECUTOR,
                        inbox.retry_or_fail,
                        int(row_id),
                        "Telegram message unavailable",
                        None,
                        claimed_token,
                    )
                    continue
                ev = SimpleNamespace(
                    chat_id=int(row["chat_id"]),
                    message=msg,
                    media=getattr(msg, "media", None),
                    inbox_id=int(row_id),
                    claim_token=claimed_token,
                )
                await process_telegram_message(ev)
                await asyncio.get_running_loop().run_in_executor(
                    _INBOX_EXECUTOR,
                    inbox.mark_ignored_if_empty,
                    int(row_id),
                    "no_code_or_not_routed",
                    claimed_token,
                )
        except asyncio.CancelledError:
            # A claimed row is marked ``processing`` before browser/OCR work.
            # Requeue it before allowing cancellation to propagate; otherwise
            # a graceful stop can strand it until the lease expires.
            if claimed_row_id is not None and _durable_inbox is not None:
                try:
                    requeued = await asyncio.get_running_loop().run_in_executor(
                        _INBOX_EXECUTOR,
                        _durable_inbox.requeue_claimed_for_shutdown,
                        claimed_row_id,
                        claimed_token,
                        "worker cancelled during shutdown",
                    )
                    if requeued:
                        logger.warning(
                            "♻️ [Inbox] requeue row=%s vì worker bị hủy",
                            claimed_row_id,
                        )
                except Exception:
                    logger.exception(
                        "❌ [Inbox] không requeue được row=%s khi worker bị hủy",
                        claimed_row_id,
                    )
            raise
        except Exception as e:
            logger.error(f"❌ worker#{wid}: {e}")
            try:
                if _durable_inbox is not None:
                    # ✅ FIX: retry_or_fail (bound) thay vì retry() vô hạn.
                    await asyncio.get_running_loop().run_in_executor(
                        _INBOX_EXECUTOR,
                        _durable_inbox.retry_or_fail,
                        int(row_id),
                        str(e),
                        None,
                        claimed_token,
                    )
            except Exception:
                pass
        finally:
            try:
                message_queue.task_done()
            except Exception:
                pass


async def _inbox_drain_loop():
    global _inbox_wakeup
    if _inbox_wakeup is None:
        _inbox_wakeup = asyncio.Event()
    while bot_state.is_running:
        try:
            inbox = _durable_inbox
            if inbox is not None and message_queue is not None:
                ids = await asyncio.get_running_loop().run_in_executor(
                    _INBOX_EXECUTOR,
                    inbox.pending_ids,
                    int(getattr(Config, "TELEGRAM_INBOX_DRAIN_BATCH", 250)),
                )
                for row_id in ids:
                    if message_queue.full():
                        break
                    if int(row_id) in bot_state._inbox_enqueued_ids:
                        continue
                    try:
                        message_queue.put_nowait((time.perf_counter(), int(row_id)))
                        bot_state._inbox_enqueued_ids.add(int(row_id))
                    except asyncio.QueueFull:
                        break
            # New-message ingress signals this event after the durable insert,
            # so normal traffic is drained immediately. The timeout remains as
            # a recovery sweep for rows left pending after a crash/reconnect.
            interval = max(0.05, float(getattr(Config, "TELEGRAM_INBOX_DRAIN_INTERVAL", 0.2)))
            try:
                await asyncio.wait_for(_inbox_wakeup.wait(), timeout=interval)
                _inbox_wakeup.clear()
            except asyncio.TimeoutError:
                pass
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.debug("⚠️ [Inbox] drain lỗi: %s", exc)
            await asyncio.sleep(1.0)


def start_message_workers():
    global message_queue, _inbox_wakeup

    mx = int(getattr(Config, "MESSAGE_QUEUE_MAXSIZE", 2000))
    n = max(1, int(getattr(Config, "MESSAGE_WORKERS", 4)))

    if message_queue is None:
        message_queue = asyncio.Queue(maxsize=mx)
        qm = get_queue_manager()
        if qm is not None:
            qm.register("message", message_queue, on_drop=_on_message_queue_drop)

    if message_workers:
        return

    if _inbox_wakeup is None:
        _inbox_wakeup = asyncio.Event()

    for i in range(1, n + 1):
        task = asyncio.create_task(message_worker(i), name=f"worker-{i}")
        message_workers.append(task)

    global _inbox_drain_task
    if _inbox_drain_task is None or _inbox_drain_task.done():
        _inbox_drain_task = asyncio.create_task(_inbox_drain_loop(), name="inbox-drain")

    logger.info(f"🚀 {n} workers started")


def clear_old_message_queue() -> int:
    """
    Xóa các item đã nằm trong queue trước khi handler hoạt động.
    Không xóa tin trên Telegram.
    """
    if message_queue is None:
        return 0

    removed = 0
    while True:
        try:
            item = message_queue.get_nowait()
        except asyncio.QueueEmpty:
            break

        try:
            if isinstance(item, tuple) and len(item) == 2:
                _inbox_message_cache.pop(int(item[1]), None)
        except (TypeError, ValueError):
            pass
        try:
            message_queue.task_done()
        except Exception:
            pass

        removed += 1

    if removed:
        logger.warning(
            "🧹 Đã xóa %s tin cũ khỏi message queue",
            removed,
        )

    return removed


async def _clear_runtime_state(**ctx) -> str:
    """Dọn queue/cache đang chạy, giữ nguyên code_history.db."""
    lines = ["🧹 DỌN DẸP RUNTIME"]

    mq_removed = clear_old_message_queue()
    lines.append(f"• Message queue: đã xoá {mq_removed} item")

    domain_removed_total = 0
    for domain, q in list(_domain_queues.items()):
        removed = 0
        while True:
            try:
                q.get_nowait()
            except asyncio.QueueEmpty:
                break
            try:
                q.task_done()
            except Exception:
                pass
            removed += 1
        domain_removed_total += removed
        if removed:
            lines.append(f"  - domain '{domain}': {removed} item")
    lines.append(f"• Domain queues: đã xoá {domain_removed_total} item")

    counts = {
        "site_code_seen": len(bot_state._site_code_seen),
        "processed_hash": len(bot_state._processed_message_hashes),
        "inflight_codes": len(bot_state._inflight_codes),
        "inflight_accounts": len(bot_state._inflight_accounts),
        "inbox_ids": len(bot_state._inbox_enqueued_ids),
        "message_cache": len(_inbox_message_cache),
    }
    bot_state._site_code_seen.clear()
    bot_state._processed_message_hashes.clear()
    bot_state._inflight_codes.clear()
    bot_state._inflight_accounts.clear()
    bot_state._inbox_enqueued_ids.clear()
    _inbox_message_cache.clear()
    lines.append(
        "• Cache RAM: " + ", ".join(f"{key}={value}" for key, value in counts.items())
    )

    if _durable_inbox is not None:
        n = await asyncio.get_running_loop().run_in_executor(
            _INBOX_EXECUTOR,
            _durable_inbox.discard_unfinished,
            "manual_clear_command",
        )
        lines.append(f"• Durable inbox: đã ignore {n} dòng pending/processing")
    else:
        lines.append("• Durable inbox: chưa sẵn sàng — bỏ qua")

    lines.append("✅ Xong — code_history.db được GIỮ NGUYÊN")
    return "\n".join(lines)


async def setup_telegram_handler():
    if bot_state.handler_registered:
        return

    chats = []
    for k in Config.CHANNEL_CONFIG.keys():
        try:
            chats.append(int(k))
        except Exception:
            continue

    if not chats:
        logger.error("❌ Không có chat nào trong CHANNEL_CONFIG")
        return
    configured_chats = set(chats)
    source_filter = getattr(Config, "TELEGRAM_FILTER_AT_SOURCE", False)

    # Dọn queue trước khi tạo worker để không có worker lấy nhầm event cũ
    # trong lúc khởi động. Handler chỉ nhận event có message.date >= BOT_START_TIME.
    clear_old_message_queue()
    start_message_workers()

    await asyncio.sleep(0)

    def quick(ev, edited: bool = False) -> bool:
        raw_chat_id = getattr(ev, "chat_id", None)
        try:
            chat_id = int(raw_chat_id)
        except (TypeError, ValueError):
            return False
        # Khi source filtering tắt, mọi message có chat_id hợp lệ đều được
        # ghi vào DurableInbox trước; worker phía sau mới lọc channel chưa
        # cấu hình bằng channel_not_configured.
        if source_filter and chat_id not in configured_chats:
            return False

        message = getattr(ev, "message", None)
        if message is None:
            return False

        message_date = getattr(message, "date", None)
        if edited:
            edit_date = getattr(message, "edit_date", None)
            if edit_date is not None:
                if message_date is None:
                    message_date = edit_date
                else:
                    if message_date.tzinfo is None:
                        message_date = message_date.replace(tzinfo=timezone.utc)
                    if edit_date.tzinfo is None:
                        edit_date = edit_date.replace(tzinfo=timezone.utc)
                    if edit_date > message_date:
                        message_date = edit_date
        if message_date is None:
            return False
        if message_date.tzinfo is None:
            message_date = message_date.replace(tzinfo=timezone.utc)

        cutoff = BOT_START_TIME

        if getattr(Config, "TELEGRAM_CATCH_UP", False):
            cutoff = BOT_START_TIME - timedelta(minutes=5)

        return message_date >= cutoff

    async def _enqueue_event_inner(
        ev, edited: bool = False, deferred_retry: int = 0
    ) -> bool:
        global _queue_full_counter

        if not quick(ev, edited=edited):
            try:
                diagnostic_chat_id = int(getattr(ev, "chat_id", None))
            except (TypeError, ValueError):
                diagnostic_chat_id = None
            if (getattr(Config, "TELEGRAM_LOG_ALL_INGRESS", False)
                    and diagnostic_chat_id in configured_chats):
                message = getattr(ev, "message", None)
                event_date = getattr(message, "edit_date", None) if edited else None
                event_date = event_date or getattr(message, "date", None)
                logger.info(
                    "⏭️ [Ingress filtered] chat_id=%s message_id=%s edited=%s "
                    "event_date=%s cutoff=%s source_filter=%s",
                    diagnostic_chat_id,
                    getattr(message, "id", "?"),
                    edited,
                    event_date,
                    BOT_START_TIME.isoformat(),
                    source_filter,
                )
            return False

        if not _should_enqueue(ev):
            try:
                duplicate_chat_id = int(getattr(ev, "chat_id", None))
            except (TypeError, ValueError):
                duplicate_chat_id = None
            if (getattr(Config, "TELEGRAM_LOG_ALL_INGRESS", False)
                    and duplicate_chat_id in configured_chats):
                logger.info(
                    "⏭️ [Ingress duplicate] chat_id=%s message_id=%s edited=%s",
                    duplicate_chat_id,
                    getattr(getattr(ev, "message", None), "id", "?"),
                    edited,
                )
            return False

        # Reserve fingerprint trước mọi await để các callback trùng trong
        # cùng burst không cùng đi vào executor ghi inbox.
        ingress_key = _ingress_fingerprint(ev)
        if ingress_key in _ingress_inflight:
            return False
        _ingress_inflight.add(ingress_key)

        try:
            chat_id = int(getattr(ev, "chat_id", None))
        except (TypeError, ValueError):
            return False

        if message_queue is None:
            start_message_workers()

        msg = getattr(ev, "message", None)
        inbox_id = None
        enqueue_status = "not_available"
        enqueue_error = ""
        enqueue_retries = 0
        if _durable_inbox is not None and msg is not None:
            loop = asyncio.get_running_loop()
            # ✅ Ingress dùng pool riêng, không xếp hàng sau claim/retry/mark
            # của các domain worker.
            # hàng loạt lệnh retry cũ trên cùng threadpool, gây delay nhận
            # tin thực tế dù event Telethon đã tới kịp thời.
            enqueue_args = (
                chat_id,
                int(getattr(msg, "id", 0) or 0),
                getattr(msg, "date", None),
                edited,
                str(getattr(msg, "text", None) or getattr(msg, "message", None) or ""),
                bool(getattr(msg, "media", None)),
                _message_event_fingerprint(msg),
            )
            # DurableInbox.enqueue converts transient SQLite failures into
            # ``None`` so callers cannot receive a DB exception. Retry here
            # before treating the event as rejected; this covers brief WAL
            # contention or an executor backlog during a message burst.
            enqueue_retries = max(
                1,
                int(getattr(Config, "TELEGRAM_INBOX_ENQUEUE_RETRIES", 3)),
            )
            enqueue_status = "sqlite_error"
            enqueue_error = ""
            for attempt in range(enqueue_retries):
                enqueue_result = await loop.run_in_executor(
                    _INBOX_INGRESS_EXECUTOR,
                    _durable_inbox.enqueue_detailed,
                    *enqueue_args,
                )
                enqueue_status = str(enqueue_result.get("status", "sqlite_error"))
                enqueue_error = str(enqueue_result.get("error", "") or "")
                inbox_id = enqueue_result.get("id")
                if inbox_id:
                    break
                if enqueue_status == "duplicate":
                    logger.debug(
                        "⏭️ [Inbox] duplicate delivery chat=%s message=%s",
                        chat_id,
                        getattr(msg, "id", "?"),
                    )
                    break
                if attempt + 1 < enqueue_retries:
                    await asyncio.sleep(min(0.5, 0.05 * (2 ** attempt)))
                    logger.debug(
                        "🔁 [Inbox] SQLite enqueue retry %s/%s "
                        "chat=%s message=%s",
                        attempt + 1,
                        enqueue_retries - 1,
                        chat_id,
                        getattr(msg, "id", "?"),
                    )
        if not inbox_id:
            _ingress_inflight.discard(ingress_key)
            if enqueue_status == "duplicate":
                logger.debug(
                    "⏭️ [Inbox] bỏ qua event trùng chat=%s message=%s",
                    chat_id, getattr(msg, "id", "?"),
                )
            elif enqueue_status == "not_available":
                logger.error(
                    "❌ [Inbox] DurableInbox chưa sẵn sàng chat=%s message=%s",
                    chat_id, getattr(msg, "id", "?"),
                )
            else:
                logger.error(
                    "❌ [Inbox] SQLite không ghi được sau %s lần thử "
                    "chat=%s message=%s error=%s",
                    enqueue_retries,
                    chat_id,
                    getattr(msg, "id", "?"),
                    enqueue_error or "unknown",
                )
                max_deferred = max(
                    0,
                    int(getattr(Config, "TELEGRAM_INBOX_SQLITE_DEFERRED_RETRIES", 3)),
                )
                if deferred_retry < max_deferred:
                    delay = max(
                        0.5,
                        float(getattr(Config, "TELEGRAM_INBOX_SQLITE_DEFERRED_RETRY_DELAY", 2.0)),
                    )

                    async def _retry_later():
                        await asyncio.sleep(delay)
                        await enqueue_event(ev, edited=edited, deferred_retry=deferred_retry + 1)

                    schedule_tracked_task(
                        _retry_later(),
                        _ingress_tasks,
                        name=f"telegram-inbox-retry-{getattr(msg, 'id', 'unknown')}",
                    )
                    logger.warning(
                        "♻️ [Inbox] đã xếp retry SQLite trì hoãn %s/%s "
                        "chat=%s message=%s",
                        deferred_retry + 1,
                        max_deferred,
                        chat_id,
                        getattr(msg, "id", "?"),
                    )
            return False

        # Do not poison the in-memory dedup window before durable acceptance.
        # If SQLite is temporarily unavailable and all enqueue retries fail,
        # a later delivery must still get another chance.
        _mark_ingress_seen(ev)
        _ingress_inflight.discard(ingress_key)

        # Giữ message object của event realtime để worker không phải gọi lại
        # client.get_messages() qua mạng. Chỉ giữ giới hạn nhỏ trong RAM;
        # durable inbox vẫn là nguồn phục hồi khi cache miss sau restart.
        _inbox_message_cache[int(inbox_id)] = msg
        if len(_inbox_message_cache) > _INBOX_MSG_CACHE_MAX:
            stale_count = max(1, _INBOX_MSG_CACHE_MAX // 10)
            for cache_id in list(_inbox_message_cache)[:stale_count]:
                _inbox_message_cache.pop(cache_id, None)

        try:
            # FIFO ingress: không thay thế/drop item cũ. Nếu RAM queue đầy,
            # row đã nằm trong DurableInbox và _inbox_drain_loop sẽ đưa lại
            # vào queue sau; nhờ đó không làm mất thứ tự nhận tin hoặc bỏ sót
            # message khi một đợt channel phát dồn.
            message_queue.put_nowait((time.perf_counter(), int(inbox_id)))
            bot_state._inbox_enqueued_ids.add(int(inbox_id))
            if _inbox_wakeup is not None:
                _inbox_wakeup.set()
            cfg = Config.CHANNEL_CONFIG.get(chat_id, {})
            logger.info(
                "📨 [Telegram accepted] chat_id=%s name=%s message_id=%s media=%s",
                chat_id,
                cfg.get("name", ""),
                getattr(getattr(ev, "message", None), "id", "?"),
                bool(getattr(getattr(ev, "message", None), "media", None)),
            )
            bot_state.last_accepted_message_at = time.monotonic()
            bot_state.accepted_message_count += 1
            _queue_full_counter = 0
            return True
        except asyncio.QueueFull:
            _queue_full_counter += 1
            _inbox_message_cache.pop(int(inbox_id), None)
            if _inbox_wakeup is not None:
                _inbox_wakeup.set()
            logger.warning(
                "⚠️ Telegram message queue full %s lần | row=%s giữ trong durable inbox | qsize=%s/%s",
                _queue_full_counter, int(inbox_id), message_queue.qsize(), message_queue.maxsize,
            )
            return False
    async def enqueue_event(
        ev, edited: bool = False, deferred_retry: int = 0
    ) -> bool:
        async with _get_ingress_semaphore():
            try:
                return await _enqueue_event_inner(
                    ev, edited=edited, deferred_retry=deferred_retry
                )
            finally:
                # Không để một exception bất ngờ làm kẹt fingerprint vĩnh viễn
                # trong single-flight set; durable SQLite vẫn là lớp dedup cuối.
                _ingress_inflight.discard(_ingress_fingerprint(ev))

    def _schedule_ingress_enqueue(ev, edited: bool = False) -> None:
        """Schedule durable ingress without blocking Telethon's callback."""
        schedule_tracked_task(
            enqueue_event(ev, edited=edited),
            _ingress_tasks,
            name=f"telegram-ingress-{getattr(getattr(ev, 'message', None), 'id', 'unknown')}",
        )

    async def h_new(ev):
        try:
            message = getattr(ev, "message", None)
            try:
                diagnostic_chat_id = int(getattr(ev, "chat_id", None))
            except (TypeError, ValueError):
                diagnostic_chat_id = None
            if (getattr(Config, "TELEGRAM_LOG_ALL_INGRESS", False)
                    and diagnostic_chat_id in configured_chats):
                logger.info(
                    "🧪 [Ingress event] type=%s chat_id=%s message_id=%s date=%s",
                    type(ev).__name__,
                    getattr(ev, "chat_id", None),
                    getattr(message, "id", None),
                    getattr(message, "date", None),
                )
            _schedule_ingress_enqueue(ev, edited=False)
        except Exception as e:
            logger.error(f"❌ NewMessage handler lỗi: {e}")

    async def h_edit(ev):
        try:
            message = getattr(ev, "message", None)
            try:
                diagnostic_chat_id = int(getattr(ev, "chat_id", None))
            except (TypeError, ValueError):
                diagnostic_chat_id = None
            if (getattr(Config, "TELEGRAM_LOG_ALL_INGRESS", False)
                    and diagnostic_chat_id in configured_chats):
                logger.info(
                    "🧪 [Ingress edit] type=%s chat_id=%s message_id=%s date=%s",
                    type(ev).__name__,
                    getattr(ev, "chat_id", None),
                    getattr(message, "id", None),
                    getattr(message, "date", None),
                )
            _schedule_ingress_enqueue(ev, edited=True)
        except Exception as e:
            logger.error(f"❌ MessageEdited handler lỗi: {e}")

    async def h_raw(update):
        """Lightweight update probe; records ingress before message filters."""
        bot_state.last_raw_update_at = time.monotonic()
        bot_state.raw_update_count += 1

    # Lọc ngay ở Telethon khi được bật để giảm event/task thừa từ các chat
    # ngoài CHANNEL_CONFIG. quick() bên trong vẫn là lớp bảo vệ thứ hai.
    new_event = events.NewMessage(chats=list(configured_chats)) if source_filter else events.NewMessage()
    edit_event = events.MessageEdited(chats=list(configured_chats)) if source_filter else events.MessageEdited()
    client.add_event_handler(h_new, new_event)
    client.add_event_handler(h_edit, edit_event)
    client.add_event_handler(h_raw, events.Raw())

    bot_state.handler_registered = True

    domain_counts = {}
    for _cid, _cfg in Config.CHANNEL_CONFIG.items():
        _d = normalize_domain(_cfg.get("url", ""))
        domain_counts[_d] = domain_counts.get(_d, 0) + 1

    logger.info(
        "✅ Handler ready | %s channels | QQ88=%s HI88=%s | OCR-only=%s | "
        "deferred ingress filter=%s + dedup + raw-update probe enabled",
        len(chats),
        domain_counts.get("tangquaqq88.com", 0),
        domain_counts.get("hi88-freecode.pages.dev", 0),
        sorted(getattr(Config, "OCR_ALLOWED_CHANNEL_IDS", set())),
        source_filter,
    )
# ═══════════════════════════════════════════════════════════════
# WATCHDOGS
# ═══════════════════════════════════════════════════════════════
def _cleanup_stale_ocr_tmp() -> int:
    base = Path(tempfile.gettempdir())
    mx = float(getattr(Config, "OCR_TEMP_DIR_MAX_AGE_SECONDS", 3600))
    n = 0
    now = time.time()
    try:
        entries = list(base.iterdir())
    except OSError:
        return 0
    for e in entries:
        try:
            if not e.is_dir() or not e.name.startswith("ocr_"):
                continue
            if now - e.stat().st_mtime <= mx:
                continue
            cleanup_stale_files(e, max_age_seconds=0)
            shutil.rmtree(e, ignore_errors=True)
            n += 1
        except Exception:
            continue
    return n


async def _cleanup_scheduler():
    last_media_cleanup = 0.0
    media_cleanup_interval = max(
        3600.0,
        float(getattr(Config, "MEDIA_CLEANUP_INTERVAL_SECONDS", 3 * 24 * 60 * 60)),
    )
    # ✅ FIX: quét dọn định kỳ các dòng inbox 'pending' bị kẹt quá lâu (lỗi
    # không xác định, crash giữa chừng, hoặc sót lại sau khi mark_failed
    # không được gọi đúng chỗ...) — dùng đúng DurableInbox.ignore_pending_before()
    # vốn đã được viết sẵn nhưng trước đây KHÔNG hề được gọi ở đâu cả.
    # Giftcode gần như luôn hết hạn rất nhanh nên an toàn khi bỏ qua sau
    # INBOX_MAX_PENDING_AGE_SECONDS (mặc định 15 phút).
    last_inbox_sweep = 0.0
    inbox_sweep_interval = max(
        30.0,
        float(getattr(Config, "INBOX_PENDING_SWEEP_INTERVAL_SECONDS", 300.0)),
    )
    inbox_max_pending_age = max(
        30.0,
        float(getattr(Config, "INBOX_MAX_PENDING_AGE_SECONDS", 900.0)),
    )
    while bot_state.is_running:
        try:
            await asyncio.sleep(float(getattr(Config, "INPUT_CACHE_CLEANUP_INTERVAL", 30)))
            _prune_ingress_message_seen()
            _prune_site_code_seen()
            _prune_processed_message_hashes()
            now = time.monotonic()
            if now - last_media_cleanup >= media_cleanup_interval:
                loop = asyncio.get_running_loop()
                removed = await loop.run_in_executor(_MEDIA_EXECUTOR, _cleanup_stale_ocr_tmp)
                last_media_cleanup = now
                logger.info(
                    "🧹 [MEDIA CLEANUP] đã xóa %s thư mục OCR/media cũ (chu kỳ %.0f ngày)",
                    removed,
                    media_cleanup_interval / 86400.0,
                )
            if _durable_inbox is not None and now - last_inbox_sweep >= inbox_sweep_interval:
                cutoff = datetime.now(timezone.utc) - timedelta(seconds=inbox_max_pending_age)
                loop = asyncio.get_running_loop()
                purged = await loop.run_in_executor(
                    _INBOX_EXECUTOR,
                    _durable_inbox.ignore_pending_before,
                    cutoff,
                    "stale_pending_purge",
                )
                last_inbox_sweep = now
                if purged:
                    logger.warning(
                        "🧹 [Inbox-Sweep] Đã bỏ %s dòng 'pending' kẹt quá %.0f phút "
                        "(giftcode chắc chắn đã hết hạn)",
                        purged,
                        inbox_max_pending_age / 60.0,
                    )
        except asyncio.CancelledError:
            break
        except Exception:
            pass


async def _db_maintenance_loop():
    while bot_state.is_running:
        try:
            now = datetime.now()
            next_run = now.replace(hour=3, minute=0, second=0, microsecond=0)
            if next_run <= now:
                next_run += timedelta(days=1)
            await asyncio.sleep(max(1.0, (next_run - now).total_seconds()))
            if _systems and _systems.get("db"):
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(_DB_EXECUTOR, _systems["db"].vacuum)
            if _durable_inbox is not None:
                loop = asyncio.get_running_loop()
                purged = await loop.run_in_executor(_INBOX_EXECUTOR, _durable_inbox.purge_completed, 7)
                if purged:
                    logger.info("🧹 [Inbox-Maintenance] Đã dọn %s row completed/ignored cũ (>7 ngày)", purged)
        except asyncio.CancelledError:
            break
        except Exception:
            pass


async def daily_reset_watchdog():
    while bot_state.is_running:
        try:
            await asyncio.sleep(60)
            _refresh_daily_state()
        except asyncio.CancelledError:
            break
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════
async def main():
    global _systems, BOT_START_TIME, _durable_inbox

    if client is None:
        raise RuntimeError(
            "Thiếu API_ID hoặc API_HASH. Hãy điền hai biến này trong file .env "
            "trước khi chạy bot."
        )

    try:
        logger.info("🚀 BOT v%s — BROWSER-ONLY", BOT_VERSION)

        BOT_START_TIME = datetime.now(timezone.utc)

        logger.info(
            f"⏰ START: "
            f"{datetime.now().strftime('%H:%M:%S %d/%m/%Y')}"
        )
        logger.info(
            f"🆕 New-message-only mode | cutoff={BOT_START_TIME.isoformat()} | "
            f"catch_up={getattr(Config, 'TELEGRAM_CATCH_UP', False)} | "
            f"inbox_recovery={getattr(Config, 'TELEGRAM_INBOX_RECOVERY_MODE', 'at_least_once')}"
        )

        _systems = await init_systems()
        # Nạp RapidOCR trước khi nhận tin để ảnh đầu tiên không phải trả
        # thêm 1–3 giây khởi tạo model trong luồng xử lý Telegram.
        if getattr(Config, "MAX_CONCURRENT_OCR", 0) > 0:
            try:
                await asyncio.get_running_loop().run_in_executor(
                    _OCR_EXECUTOR,
                    get_image_extractor,
                )
                logger.info("✅ OCR warm-up hoàn tất")
            except Exception as exc:
                logger.warning("⚠️ OCR warm-up thất bại, sẽ thử lại khi có ảnh: %s", exc)

        logger.warning("🔥 client.start()...")
        await client.start()
        logger.warning("🔥 OK")
        # Signal handlers must wake ``run_until_disconnected()`` without
        # stopping the event loop, so main() can execute its async finally.
        get_shutdown_handler().set_stop_callback(client.disconnect)

        dialogs_synced = await sync_telegram_dialogs()
        if not dialogs_synced:
            logger.warning(
                "⚠️ Telegram dialog sync không hoàn tất; tiếp tục bằng "
                "verify_channels_and_get_ids() để kiểm tra quyền truy cập thực tế"
            )

        if not await verify_telegram_session():
            raise RuntimeError("Telegram session verification failed")

        disable_console_logging()
        start_dashboard()

        qm = init_queue_manager(
            soft_pct=float(
                getattr(
                    Config,
                    "QUEUE_SOFT_LIMIT_PCT",
                    0.75,
                )
            ),
            hard_pct=float(
                getattr(
                    Config,
                    "QUEUE_HARD_LIMIT_PCT",
                    0.95,
                )
            ),
            check_interval=float(
                getattr(
                    Config,
                    "QUEUE_CHECK_INTERVAL",
                    5.0,
                )
            ),
            cleanup_on_soft_limit=bool(
                getattr(
                    Config,
                    "QUEUE_CLEANUP_ON_SOFT_LIMIT",
                    False,
                )
            ),
            cleanup_target_pct=float(
                getattr(
                    Config,
                    "QUEUE_CLEANUP_TARGET_PCT",
                    0.50,
                )
            ),
        )

        await qm.start()

        logger.info(
            f"🧹 Queue manager ready "
            f"(soft={qm.soft_pct * 100:.0f}%, "
            f"hard={qm.hard_pct * 100:.0f}%, "
            f"interval={qm.check_interval:.0f}s)"
        )

        verified_channels = await verify_channels_and_get_ids()
        configured_channels = dict(Config.CHANNEL_CONFIG)
        missing_channels = sorted(
            set(configured_channels) - set(verified_channels)
        )
        if missing_channels:
            logger.error(
                "⚠️ verify_channels chưa xác nhận được %s channel: %s",
                len(missing_channels),
                ", ".join(str(chat_id) for chat_id in missing_channels),
            )
            logger.warning(
                "✅ Giữ nguyên toàn bộ %s channel trong Config; handler vẫn "
                "subscribe bằng numeric chat_id để tránh mất event do timeout/peer cache",
                len(configured_channels),
            )

        logger.warning("⭐ Init channels/accounts (browser-only)...")

        await init_channels_and_accounts()

        # Register ingress before the potentially slow browser preload. The
        # durable inbox and domain queues are already initialized, so messages
        # arriving during preload can be accepted and wait safely in queues.
        start_domain_workers()
        await setup_telegram_handler()

        # Preload every configured browser domain while already-accepted
        # messages wait safely in the durable/RAM queues.
        if _BROWSER_ENGINE_OK:
            browser_targets = [
                t for t in build_unique_account_targets()
                if t["domain"] in browser_engine.BROWSER_DOMAINS
            ]
            try:
                await browser_engine.preload_browsers_and_accounts(browser_targets)
            except Exception as e:
                logger.error(
                    f"❌ [Browser] preload lỗi ({e}) — submit sẽ được đánh dấu lỗi hạ tầng"
                )

        register_default_commands()
        command_registry.register("clear", _clear_runtime_state, timeout=15.0)

        setup_admin_commands(
            client,
            admin_id=Config.TELEGRAM_ADMIN_ID,
            context_provider=lambda: {
                "bot_state": bot_state,
                "systems": _systems,
            },
        )

        if getattr(
            Config,
            "TELEGRAM_CATCH_UP",
            False,
        ):
            await client.catch_up()
        else:
            # Fill the startup gap between client connection and handler
            # registration. quick() still requires message.date >=
            # BOT_START_TIME, so this does not replay older history.
            logger.info("🔄 Startup catch_up có cutoff — chỉ nhận message sau thời điểm start")
            await client.catch_up()

        logger.info(
            f"✅ BOT READY! "
            f"{datetime.now().strftime('%H:%M:%S')}"
        )

        async def heartbeat():
            """
            Kiểm tra kết nối định kỳ. Nếu get_me() thất bại nhiều lần liên
            tiếp trong khi client.is_connected() vẫn báo True, đây là dấu
            hiệu kết nối "treo im lặng" (socket còn sống nhưng update
            stream của Telegram không còn hoạt động) — bot sẽ không nhận
            được tin nhắn kênh nào nữa dù is_connected() vẫn True. Trước
            đây heartbeat chỉ log cảnh báo mà không hành động gì, khiến bot
            có thể "câm" hàng giờ mà không tự phục hồi.
            ✅ FIX: chủ động disconnect() sau ngưỡng lỗi liên tiếp để
            run_until_disconnected() ở main() trả về → vòng lặp chính tự
            connect() lại + catch_up() (xem fix reconnect phía trên) —
            khôi phục luồng nhận tin nhắn mà không cần khởi động lại bot.
            """
            consecutive_heartbeat_failures = 0
            heartbeat_failure_threshold = 2
            while bot_state.is_running:
                try:
                    await asyncio.sleep(
                        float(
                            getattr(
                                Config,
                                "HEARTBEAT_INTERVAL",
                                300.0,
                            )
                        )
                    )

                    now_mono = time.monotonic()
                    raw_age = (
                        "-" if bot_state.last_raw_update_at is None
                        else f"{now_mono - bot_state.last_raw_update_at:.0f}s"
                    )
                    accepted_age = (
                        "-" if bot_state.last_accepted_message_at is None
                        else f"{now_mono - bot_state.last_accepted_message_at:.0f}s"
                    )
                    logger.info(
                        f"💓 {datetime.now().strftime('%H:%M:%S')} "
                        f"| connected={client.is_connected()} "
                        f"| raw_updates={bot_state.raw_update_count} "
                        f"(last={raw_age}) "
                        f"| accepted={bot_state.accepted_message_count} "
                        f"(last={accepted_age}) "
                        f"| loop_lag={bot_state.event_loop_lag_ms:.0f}ms "
                        f"| tasks={len(_active_submit_tasks)} "
                        f"| q={message_queue.qsize() if message_queue else 0}"
                    )

                    try:
                        me = await asyncio.wait_for(
                            client.get_me(),
                            timeout=float(
                                getattr(
                                    Config,
                                    "TELEGRAM_HEARTBEAT_TIMEOUT",
                                    8.0,
                                )
                            ),
                        )

                        if not me:
                            consecutive_heartbeat_failures += 1
                            logger.warning(
                                "⚠️ [Heartbeat] Telegram session không phản hồi "
                                "(lỗi liên tiếp=%s)",
                                consecutive_heartbeat_failures,
                            )
                        else:
                            consecutive_heartbeat_failures = 0

                    except asyncio.CancelledError:
                        raise

                    except Exception as e:
                        consecutive_heartbeat_failures += 1
                        logger.warning(
                            "⚠️ [Heartbeat] Kiểm tra kết nối lỗi (lỗi liên tiếp=%s): %s",
                            consecutive_heartbeat_failures,
                            e,
                        )

                    if (
                        consecutive_heartbeat_failures >= heartbeat_failure_threshold
                        and client.is_connected()
                    ):
                        logger.critical(
                            "🚨 [Heartbeat] Nghi ngờ kết nối Telegram 'treo im lặng' "
                            "(get_me thất bại %s lần liên tiếp dù is_connected()=True) "
                            "— chủ động disconnect để buộc reconnect + catch_up()",
                            consecutive_heartbeat_failures,
                        )
                        consecutive_heartbeat_failures = 0
                        try:
                            await client.disconnect()
                        except Exception as disconnect_err:
                            logger.warning(
                                "⚠️ [Heartbeat] disconnect() cưỡng bức lỗi: %s",
                                disconnect_err,
                            )

                except asyncio.CancelledError:
                    break

                except Exception as e:
                    logger.warning(
                        f"⚠️ [Heartbeat] Lỗi không mong muốn: {e}"
                    )

        async def event_loop_probe():
            """Detect a blocked asyncio loop independently of Telegram RPC."""
            expected = time.monotonic() + 1.0
            while bot_state.is_running:
                await asyncio.sleep(1.0)
                now = time.monotonic()
                lag_ms = max(0.0, (now - expected) * 1000.0)
                bot_state.event_loop_lag_ms = lag_ms
                if lag_ms >= 1000.0:
                    logger.warning(
                        "⚠️ [EventLoop] lag=%.0fms | raw_updates=%s | connected=%s",
                        lag_ms,
                        bot_state.raw_update_count,
                        client.is_connected(),
                    )
                expected = now + 1.0

        _bg = {
            asyncio.create_task(
                heartbeat(),
                name="hb",
            ),
            asyncio.create_task(
                event_loop_probe(),
                name="event-loop-probe",
            ),
            asyncio.create_task(
                _cleanup_scheduler(),
                name="cleanup",
            ),
            asyncio.create_task(
                daily_reset_watchdog(),
                name="daily",
            ),
            asyncio.create_task(
                _db_maintenance_loop(),
                name="db",
            ),
        }

        if _BROWSER_ENGINE_OK and getattr(Config, "USE_BROWSER_FOR_MULTI_SITE", True):
            _bg.add(asyncio.create_task(browser_engine.browser_watchdog(), name="browser-watchdog"))

        reconnect_attempts = 0
        max_reconnect_attempts = max(
            0,
            int(getattr(Config, "TELEGRAM_MAX_RECONNECT_ATTEMPTS", 5)),
        )
        reconnect_base_delay = max(
            0.5,
            float(getattr(Config, "TELEGRAM_RECONNECT_BASE_DELAY", 2.0)),
        )
        reconnect_max_delay = max(
            reconnect_base_delay,
            float(getattr(Config, "TELEGRAM_RECONNECT_MAX_DELAY", 60.0)),
        )
        reconnect_jitter = max(
            0.0,
            float(getattr(Config, "TELEGRAM_RECONNECT_JITTER", 1.0)),
        )
        stable_connection_seconds = max(
            0.0,
            float(getattr(Config, "TELEGRAM_STABLE_CONNECTION_SECONDS", 30.0)),
        )
        connect_timeout = max(
            1.0,
            float(getattr(Config, "TELEGRAM_CONNECT_TIMEOUT", 15.0)),
        )
        connected_since = time.monotonic() if client.is_connected() else None

        def reconnect_delay(attempt: int) -> float:
            exponential = min(
                reconnect_max_delay,
                reconnect_base_delay * (2 ** max(0, attempt - 1)),
            )
            return exponential + random.uniform(0.0, reconnect_jitter)

        while bot_state.is_running:
            try:
                if not client.is_connected():
                    reconnect_attempts += 1
                    if reconnect_attempts > max_reconnect_attempts:
                        logger.critical(
                            "🛑 [Telegram] Hết giới hạn reconnect (%s lần)",
                            max_reconnect_attempts,
                        )
                        bot_state.is_running = False
                        return

                    delay = reconnect_delay(reconnect_attempts)
                    logger.warning(
                        "🔄 [Telegram] Mất kết nối — thử lần %s/%s sau %.1fs",
                        reconnect_attempts,
                        max_reconnect_attempts,
                        delay,
                    )
                    await asyncio.sleep(delay)
                    await asyncio.wait_for(
                        client.connect(),
                        timeout=connect_timeout,
                    )
                    connected_since = time.monotonic()
                    # ✅ FIX [mất tin nhắn khi reconnect]: catch_up() trước đây
                    # chỉ được gọi 1 LẦN DUY NHẤT lúc khởi động bot (trước vòng
                    # lặp này). Với TELEGRAM_AUTO_RECONNECT=false, mọi lần rớt
                    # kết nối sau đó đều do CHÍNH vòng lặp này tự connect() lại
                    # — nhưng không catch_up() lại, nên mọi update Telegram gửi
                    # trong lúc mất kết nối (channel đăng code) bị bỏ lỡ VĨNH
                    # VIỄN: handler NewMessage/MessageEdited chỉ nhận update
                    # LIVE từ thời điểm kết nối lại, không tự lùi lại lấy update
                    # cũ. Gọi lại catch_up() ngay sau mỗi lần connect() thành
                    # công để Telegram đẩy bù các update đã tích lũy trong lúc
                    # bot mất kết nối — handler quick() vẫn lọc theo
                    # message.date >= BOT_START_TIME như bình thường nên không
                    # replay lịch sử cũ trước lúc bot start.
                    try:
                        await asyncio.wait_for(
                            client.catch_up(),
                            timeout=connect_timeout,
                        )
                        logger.info(
                            "♻️ [Telegram] catch_up() sau reconnect — đã lấy update bị lỡ lúc mất kết nối"
                        )
                    except asyncio.CancelledError:
                        raise
                    except Exception as catch_up_err:
                        logger.warning(
                            "⚠️ [Telegram] catch_up() sau reconnect lỗi (sẽ vẫn tiếp tục "
                            "nhận update live): %s",
                            catch_up_err,
                        )

                await client.run_until_disconnected()

                if not bot_state.is_running:
                    break

                connection_age = (
                    time.monotonic() - connected_since
                    if connected_since is not None
                    else 0.0
                )
                if connection_age >= stable_connection_seconds:
                    reconnect_attempts = 0
                connected_since = None

            except asyncio.CancelledError:
                raise

            except AuthKeyDuplicatedError as e:
                logger.critical(
                    "🚨 [Telegram] AuthKeyDuplicated — dừng retry ngay: %s",
                    e,
                )
                bot_state.is_running = False
                try:
                    await client.disconnect()
                except Exception:
                    pass
                backup_corrupt_telegram_session()
                await send_auth_key_alert(e)
                raise SystemExit(78)

            except (asyncio.TimeoutError, ConnectionError, OSError) as e:
                reconnect_attempts += 1
                if reconnect_attempts > max_reconnect_attempts:
                    logger.critical(
                        "🛑 [Telegram] Hết giới hạn reconnect (%s lần): %s",
                        max_reconnect_attempts,
                        e,
                    )
                    bot_state.is_running = False
                    return

                delay = reconnect_delay(reconnect_attempts)
                logger.warning(
                    "⚠️ [Telegram] Lỗi kết nối: %s — thử lại sau %.1fs "
                    "(lần %s/%s)",
                    e,
                    delay,
                    reconnect_attempts,
                    max_reconnect_attempts,
                )
                await asyncio.sleep(delay)

            except Exception as e:
                logger.exception(
                    "❌ [Telegram] Lỗi không mong muốn — dừng bot, không retry: %s",
                    e,
                )
                bot_state.is_running = False
                return

    except AuthKeyDuplicatedError as e:
        logger.critical(
            "🚨 [Telegram] AuthKeyDuplicated — dừng bot, không retry: %s",
            e,
        )
        bot_state.is_running = False
        try:
            await client.disconnect()
        except Exception:
            pass
        backup_corrupt_telegram_session()
        await send_auth_key_alert(e)
        # Giữ mã lỗi sau khi finally hoàn tất để run.bat/supervisor biết
        # đây là lỗi session cần xử lý thủ công, không phải shutdown bình thường.
        raise SystemExit(78)

    except Exception as e:
        logger.critical(f"❌ Critical: {e}\n{traceback.format_exc()}")
        raise SystemExit(1)
    finally:
        logger.info("\n🛑 Shutting down...")
        bot_state.is_running = False
        if _history_queue is not None:
            try:
                await asyncio.wait_for(_history_queue.join(), timeout=5.0)
            except Exception:
                pass
            if _history_writer_task:
                _history_writer_task.cancel()
        # Ingress callbacks enqueue durably in tracked background tasks so the
        # Telethon update loop is never held up by SQLite.  Let already
        # received updates finish their insert before closing the connection;
        # otherwise a shutdown during a burst could leave an event only in
        # memory and make it unrecoverable.
        if _ingress_tasks:
            pending_ingress = list(_ingress_tasks)
            try:
                await asyncio.wait_for(
                    asyncio.gather(*pending_ingress, return_exceptions=True),
                    timeout=5.0,
                )
            except Exception:
                for task in pending_ingress:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*pending_ingress, return_exceptions=True)

        # Workers are allowed to finish rows already in RAM.  This also lets
        # them create their final domain submissions before we wait for the
        # active submit set below.  Rows that do not finish before the bound
        # remain durable and are requeued by the worker cancellation handler.
        if message_queue is not None:
            try:
                await asyncio.wait_for(
                    message_queue.join(),
                    timeout=float(getattr(Config, "SHUTDOWN_MESSAGE_DRAIN_TIMEOUT", 8.0)),
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "⚠️ Shutdown: message queue chưa drain hết (q=%s); "
                    "các row còn lại sẽ được giữ trong DurableInbox",
                    message_queue.qsize(),
                )
            except Exception as exc:
                logger.warning("⚠️ Shutdown: lỗi drain message queue: %s", exc)

        if _media_pipeline_queue is not None:
            try:
                await asyncio.wait_for(
                    _media_pipeline_queue.join(),
                    timeout=float(getattr(Config, "SHUTDOWN_MESSAGE_DRAIN_TIMEOUT", 8.0)),
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "⚠️ Shutdown: media queue chưa drain hết (q=%s); "
                    "đang hủy worker để DurableInbox retry các row còn xử lý",
                    _media_pipeline_queue.qsize(),
                )
        for task in _media_pipeline_workers:
            if not task.done():
                task.cancel()
        if _media_pipeline_workers:
            await asyncio.gather(*_media_pipeline_workers, return_exceptions=True)
        if _media_pipeline_queue is not None:
            while True:
                try:
                    queued_item = _media_pipeline_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                try:
                    await _retry_dropped_media_pipeline_item(queued_item)
                finally:
                    _media_pipeline_queue.task_done()

        if _active_submit_tasks:
            try:
                await asyncio.wait_for(asyncio.gather(*list(_active_submit_tasks), return_exceptions=True), timeout=8.0)
            except Exception:
                for t in list(_active_submit_tasks):
                    t.cancel()
        for w in message_workers:
            w.cancel()
        global _inbox_drain_task
        if _inbox_drain_task is not None:
            _inbox_drain_task.cancel()
            await asyncio.gather(_inbox_drain_task, return_exceptions=True)
            _inbox_drain_task = None
        domain_tasks = [task for workers in _domain_workers.values() for task in workers]
        if domain_tasks:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*domain_tasks, return_exceptions=True), timeout=8.0
                )
            except asyncio.TimeoutError:
                for task in domain_tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*domain_tasks, return_exceptions=True)
        for handle, retry_item in list(_rate_limit_retry_handles.items()):
            handle.cancel()
            inbox_row = retry_item.get("inbox_id") if isinstance(retry_item, dict) else None
            claim = retry_item.get("claim_token") if isinstance(retry_item, dict) else None
            if inbox_row and _durable_inbox is not None:
                # ✅ FIX: cancelling this pending rate-limit retry on shutdown
                # is not a submit failure — it's a scheduled in-RAM callback
                # (call_later) that never got to run. Using retry_or_fail()
                # here counted every bot restart against max_attempts, which
                # could eventually fail a row that was only ever waiting out
                # a rate limit, never actually rejected by the site.
                try:
                    await asyncio.get_running_loop().run_in_executor(
                        _INBOX_STATE_EXECUTOR,
                        _durable_inbox.requeue_claimed_for_shutdown,
                        int(inbox_row),
                        claim,
                        "shutdown while waiting for rate-limit backoff",
                    )
                except Exception:
                    logger.exception("❌ cannot requeue rate-limited inbox row=%s on shutdown", inbox_row)
        _rate_limit_retry_handles.clear()

        for ws in _domain_workers.values():
            for t in ws:
                t.cancel()
        try:
            qm = get_queue_manager()
            if qm is not None:
                await qm.stop()
        except Exception:
            pass

        # Hủy các task nền trước khi tạo báo cáo tổng kết
        background_tasks = locals().get("_bg", set())

        for task in list(background_tasks):
            if not task.done():
                task.cancel()

        if background_tasks:
            await asyncio.gather(
                *background_tasks,
                return_exceptions=True,
            )

        try:
            shutdown_ocr_executor(wait=True, cancel_futures=True)
        except Exception as e:
            logger.debug(f"⚠️ [OCR] shutdown executor lỗi (bỏ qua): {e}")

        if _BROWSER_ENGINE_OK:
            browser_engine.shutdown()
            try:
                await browser_engine.cleanup_browsers()
            except Exception as e:
                logger.debug(f"⚠️ [Browser] cleanup lỗi (bỏ qua): {e}")

        if _durable_inbox is not None:
            try:
                _durable_inbox.close()
            except Exception:
                pass
            _durable_inbox = None

        build_daily_summary()
        try:
            stop_monitoring()
        except Exception as e:
            logger.debug(f"⚠️ [Health] shutdown monitor lỗi (bỏ qua): {e}")
        stop_dashboard()
        get_shutdown_handler().notify_cleanup_done()
        logger.info("✅ Done")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n🛑 Stopped")