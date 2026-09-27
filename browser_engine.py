"""
🌐 BROWSER ENGINE — trình duyệt (Playwright/Edge CDP) cho các domain KHÔNG có
API client riêng: MM88, RR88, XX88, O8, GG88.

Đây là bản khôi phục/thu gọn từ bot đời trước (trước khi migrate sang
browser-only — giữ lại phần cần cho các domain cấu hình:
  - Kết nối Edge đang chạy sẵn qua CDP (KHÔNG launch Chromium riêng)
  - TabPool: mỗi domain giữ (các) tab riêng, không tự mở tràn lan
  - Tìm ô nhập tài khoản/code, bấm nút submit, đọc kết quả (nhiều tầng
    fallback: selector riêng domain → selector chung SweetAlert/toast →
    quét từ khoá toàn trang → diff text trước/sau khi bấm)
  - Chụp màn hình + lưu HTML khi kết quả không rõ ràng (SCREENSHOT_ON_UNKNOWN)
    để dễ debug khi giao diện site đã đổi so với lúc code cũ chạy.

Module này CỐ TÌNH không import main_script.py ở cấp module (tránh import
vòng, vì main_script.py phải import module này để gọi). Vài hàm dùng
DEFERRED IMPORT (import main_script bên trong thân hàm) để gọi ngược lại
các tiện ích đã có sẵn ở đó (append_code_history, client Telegram) — an
toàn vì lúc các hàm này thực sự được GỌI, main_script đã import xong.
"""
from __future__ import annotations

import asyncio
import ctypes
import ctypes.wintypes
import gc
import re as _re
import time
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import async_playwright

from config import Config
from logger_setup import logger
from media_helpers import capture_result_screenshot
from submission_outcomes import record_outcome, classify_result
from browser_site_profiles import browser_domains, get_site_profile

# The page-side probe records only cross-origin metadata. It never returns
# full URLs, query strings, headers, request bodies, or response bodies.
try:
    _CROSS_DOMAIN_PROBE_JS = Path(__file__).with_name(
        "cross_domain_request_probe.js"
    ).read_text(encoding="utf-8")
except OSError:
    _CROSS_DOMAIN_PROBE_JS = ""

# ============================================================
# DOMAIN SCOPE
# Browser automation remains on one asyncio event loop. Do not call
# Playwright Page/Browser objects from raw threading.Thread workers; the
# TabPool locks and per-domain async workers provide safe parallelism instead.
# ============================================================
BROWSER_DOMAINS = browser_domains()


def _normalize_domain(url: str) -> str:
    p = urlparse(url or "")
    return (p.netloc or p.path).lower().replace("www.", "").strip("/")


def _should_navigate_to_target(current_url: str, target_url: str, domain: str = "") -> bool:
    """Check host and configured path so same-host home tabs reach code forms."""
    current = urlparse(current_url or "")
    target = urlparse(target_url or "")
    expected_host = (target.hostname or domain or "").lower().removeprefix("www.")
    current_host = (current.hostname or "").lower().removeprefix("www.")
    if not current_host or current_host == "google.com":
        return True
    if current_host != expected_host:
        return True
    expected_path = (target.path or "/").rstrip("/") or "/"
    current_path = (current.path or "/").rstrip("/") or "/"
    return expected_path != "/" and current_path != expected_path


_RESULT_DOM_CHANGED_JS = r"""(args) => {
    const normalize = (value) => String(value || '').replace(/\s+/g, ' ').trim().toLowerCase();
    const before = normalize(args.beforeText);
    const selectors = [...new Set([
        ...(args.selectors || []),
        '.swal2-container', '[role="alertdialog"]', '[role="alert"]',
        '[role="status"]', '.toast', '[class*="toast" i]',
        '[class*="result" i]', '[class*="message" i]', '[class*="error" i]',
        '[class*="success" i]', '.modal-content', '.popup-content'
    ])];
    const markers = [
        'thành công', 'thanh cong', 'success', 'completed', 'đã nhận', 'da nhan',
        'received', 'added', 'awarded', 'approved', 'accepted', 'thất bại', 'that bai',
        'invalid', 'failed', 'error', 'sai', 'lỗi', 'loi', 'đã sử', 'da su',
        'đã dùng', 'da su dung',
        'đã hết', 'da het', 'hết hạn', 'het han', 'expired', 'not found', 'not exist',
        'not used', 'code not used', 'code_not_used', 'không hợp lệ',
        'không đúng', 'không tồn tại', 'not accepted', 'not added', 'not approved',
        'unsuccessful', 'rate limit', 'too many', '429', 'quá nhiều',
        'thử lại sau', 'thêm sau', 'mã xác thực', 'ma xac thuc',
        'nhập đúng mã trong ảnh', 'nhap dung ma trong anh', 'hoàn tất xác minh',
        'hoan tat xac minh', 'nhập mã xác nhận', 'nhap ma xac nhan',
        'kéo thanh trượt', 'keo thanh truot', 'hoàn thành ghép', 'hoan thanh ghep'
    ];
    const visible = (el) => {
        const style = getComputedStyle(el), rect = el.getBoundingClientRect();
        return style.display !== 'none' && style.visibility !== 'hidden'
            && rect.width > 0 && rect.height > 0;
    };
    for (const selector of selectors) {
        try {
            for (const el of document.querySelectorAll(selector)) {
                if (!visible(el)) continue;
                const text = normalize(el.innerText || el.textContent || '');
                if (text.length >= 3 && !before.includes(text)
                    && markers.some((marker) => text.includes(marker))) return text;
            }
        } catch (_) {}
    }
    const lines = (document.body?.innerText || '').split(/\n+/);
    return lines.find((line) => {
        const text = normalize(line);
        return text.length >= 3 && !before.includes(text)
            && markers.some((marker) => text.includes(marker));
    })?.trim() || false;
}"""


def _start_browser_request_probe(page, domain: str = ""):
    """Capture timings/status for requests started during submit, never URLs/bodies."""
    started = {}
    responses = []

    def on_request(request):
        try:
            request_domain = urlparse(request.url or "").netloc.lower().replace("www.", "")
            if request.resource_type in {"document", "fetch", "xhr"} and (not domain or request_domain == domain):
                started[id(getattr(request, "_impl_obj", request))] = time.perf_counter()
        except Exception:
            pass

    def on_response(response):
        try:
            request = response.request
            start = started.pop(id(getattr(request, "_impl_obj", request)), None)
            if start is not None:
                responses.append((
                    (time.perf_counter() - start) * 1000.0,
                    int(response.status),
                    str(request.resource_type),
                ))
        except Exception:
            pass

    try:
        page.on("request", on_request)
        page.on("response", on_response)
        return {"on_request": on_request, "on_response": on_response,
                "started": started, "responses": responses}
    except Exception:
        return None


def _stop_browser_request_probe(page, probe):
    if not probe:
        return {}
    try:
        page.remove_listener("request", probe["on_request"])
        page.remove_listener("response", probe["on_response"])
    except Exception:
        pass
    samples = probe["responses"]
    return {
        "network_request_count": len(samples),
        "network_response_max_ms": round(max((r[0] for r in samples), default=0.0), 2),
        "network_http_error_count": sum(1 for _, status, _ in samples if status >= 400),
        "network_fetch_xhr_count": sum(1 for _, _, kind in samples if kind in {"fetch", "xhr"}),
    }


async def _start_cross_domain_request_probe(page) -> bool:
    """Install/start the page-side XHR/Fetch probe for the next submit."""
    if not _CROSS_DOMAIN_PROBE_JS:
        return False
    try:
        result = await page.evaluate(
            """(probeSource) => {
                if (!window.GG88RequestProbe) (0, eval)(probeSource);
                return Boolean(window.GG88RequestProbe?.start());
            }""",
            _CROSS_DOMAIN_PROBE_JS,
        )
        return bool(result)
    except Exception as exc:
        logger.debug("⚠️ Cross-domain request probe start lỗi: %s", exc)
        return False


async def _stop_cross_domain_request_probe(page) -> dict:
    """Stop the probe and return privacy-safe aggregate metrics."""
    try:
        events = await page.evaluate(
            "() => (window.GG88RequestProbe ? window.GG88RequestProbe.stop() : [])"
        )
    except Exception as exc:
        logger.debug("⚠️ Cross-domain request probe stop lỗi: %s", exc)
        return {"cross_domain_probe_error": type(exc).__name__}

    by_host: dict[str, int] = {}
    by_type: dict[str, int] = {}
    by_status: dict[str, int] = {}
    by_failure: dict[str, int] = {}
    errors = 0
    retries = 0
    durations = []
    for event in events if isinstance(events, list) else []:
        host = str(event.get("hostname") or "unknown")[:253]
        kind = str(event.get("type") or "unknown")[:16]
        status = int(event.get("status") or 0)
        failure = str(event.get("failure_class") or "unknown")[:32]
        retry_count = max(0, int(event.get("retry_count") or 0))
        by_host[host] = by_host.get(host, 0) + 1
        by_type[kind] = by_type.get(kind, 0) + 1
        by_status[str(status)] = by_status.get(str(status), 0) + 1
        by_failure[failure] = by_failure.get(failure, 0) + 1
        retries += retry_count
        if event.get("error") or status == 0:
            errors += 1
        try:
            durations.append(float(event.get("duration_ms") or 0.0))
        except (TypeError, ValueError):
            pass
    return {
        "cross_domain_request_count": len(events) if isinstance(events, list) else 0,
        "cross_domain_hosts": by_host,
        "cross_domain_types": by_type,
        "cross_domain_statuses": by_status,
        "cross_domain_failure_classes": by_failure,
        "cross_domain_error_count": errors,
        # Status 0/error is only a suspicion: page JS cannot distinguish CORS
        # from DNS, TLS, network failure, or an aborted request conclusively.
        "cross_domain_cors_suspected_count": errors,
        "cross_domain_retry_count": retries,
        "cross_domain_max_retry_count": max(
            (int(event.get("retry_count") or 0) for event in events
             if isinstance(event, dict)),
            default=0,
        ) if isinstance(events, list) else 0,
        "cross_domain_max_ms": round(max(durations, default=0.0), 2),
    }


def _append_code_history_safe(**kwargs):
    """Deferred import wrapper — gọi append_code_history() thật của
    main_script.py mà không cần import nó ở cấp module."""
    try:
        import main_script as _ms
        _ms.append_code_history(**kwargs)
    except Exception as e:
        logger.debug(f"⚠️ [Browser] append_history lỗi: {e}")


# ============================================================
# STATE
# ============================================================
class BrowserState:
    def __init__(self):
        self.account_pages: dict = {}       # key "domain|user" -> Page
        self.context_locks: dict = {}
        self.cf_verified: dict = {}
        self.cf_blocked_until: dict[str, float] = {}
        self.cf_failure_count: dict[str, int] = {}
        self.submission_count: dict = {}
        self._input_cache: dict = {}
        self._input_cache_ttl: float = 20.0
        self._submits_since_full_reload: dict = {}
        self.is_running = True


bot_state = BrowserState()


def shutdown():
    bot_state.is_running = False


def _cf_domain_paused(domain: str) -> bool:
    """Return whether only this domain is temporarily paused for CF/captcha."""
    key = _normalize_domain(domain)
    until = float(bot_state.cf_blocked_until.get(key, 0.0) or 0.0)
    if until <= 0.0:
        return False
    if time.time() >= until:
        bot_state.cf_blocked_until.pop(key, None)
        bot_state.cf_failure_count.pop(key, None)
        logger.info("▶️ [Cloudflare|%s] hết thời gian tạm dừng, thử lại domain", key)
        return False
    return True


def _mark_cf_domain(domain: str, reason: str = "challenge") -> None:
    """Pause only one domain after repeated verification challenges."""
    key = _normalize_domain(domain)
    count = int(bot_state.cf_failure_count.get(key, 0)) + 1
    threshold = max(1, int(getattr(Config, "BROWSER_CIRCUIT_FAILURE_THRESHOLD", 3)))
    bot_state.cf_failure_count[key] = count
    if count < threshold:
        return
    cooldown = max(5.0, float(getattr(Config, "BROWSER_CIRCUIT_COOLDOWN_SECONDS", 60.0)))
    bot_state.cf_blocked_until[key] = time.time() + cooldown
    logger.warning(
        "⏸️ [Cloudflare|%s] tạm dừng riêng domain %.0fs sau %s lần challenge: %s",
        key, cooldown, count, reason,
    )


def _clear_cf_domain(domain: str) -> None:
    key = _normalize_domain(domain)
    if key in bot_state.cf_failure_count or key in bot_state.cf_blocked_until:
        logger.info("✅ [Cloudflare|%s] domain hoạt động lại", key)
    bot_state.cf_failure_count.pop(key, None)
    bot_state.cf_blocked_until.pop(key, None)


# ============================================================
# SELECTORS
# ============================================================
def _get_domain_username_selectors(domain: str) -> list:
    profile = get_site_profile(domain)
    return list(profile.username_selectors) if profile else []


def _get_domain_result_selectors(domain: str) -> list:
    profile = get_site_profile(domain)
    return list(profile.result_selectors) if profile else []


def _site_profile_value(domain: str, name: str, default):
    profile = get_site_profile(domain)
    return getattr(profile, name, default) if profile else default


CF_SELECTORS = [
    "iframe[src*='turnstile']",
    "iframe[src*='challenges.cloudflare.com']",
    ".cf-turnstile",
    "[data-sitekey]",
]

REACT_FILL_JS = """
    ([el, val]) => {
        const proto = el.tagName === 'TEXTAREA'
            ? window.HTMLTextAreaElement.prototype
            : window.HTMLInputElement.prototype;
        const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
        el.focus();
        setter.call(el, '');
        setter.call(el, val);
        el.dispatchEvent(new Event('input', {bubbles: true}));
        el.dispatchEvent(new Event('change', {bubbles: true}));
    }
"""

REACT_FILL_VERIFY_JS = """
    ([userEl, codeEl, userVal, codeVal, fillUser]) => {
        const setVal = (el, val) => {
            const proto = el.tagName === 'TEXTAREA'
                ? window.HTMLTextAreaElement.prototype
                : window.HTMLInputElement.prototype;
            const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
            el.focus();
            setter.call(el, '');
            setter.call(el, val);
            el.dispatchEvent(new Event('input', {bubbles: true}));
            el.dispatchEvent(new Event('change', {bubbles: true}));
        };
        if (userEl && fillUser) setVal(userEl, userVal);
        setVal(codeEl, codeVal);
        return {
            actualUser: userEl ? userEl.value : null,
            actualCode: codeEl.value,
        };
    }
"""

_MANUAL_VERIFY_KEYWORDS = [
    "mã xác thực", "ma xac thuc",
    "nhập đúng mã trong ảnh", "nhap dung ma trong anh",
    "hoàn tất xác minh", "hoan tat xac minh",
    "nhập mã xác nhận", "nhap ma xac nhan",
    "kéo thanh trượt", "keo thanh truot",
    "hoàn thành ghép", "hoan thanh ghep",
]


def _needs_manual_verify(text: str) -> bool:
    if not text:
        return False
    low = text.strip().lower()
    return any(k in low for k in _MANUAL_VERIFY_KEYWORDS)


# ============================================================
# EDGE CDP CONNECT / LAUNCH
# ============================================================
_pw_instance = None
_edge_browser = None
_shared_context = None
_browser_lock = None
_last_launch_time: float = 0.0
_LAUNCH_COOLDOWN = 15.0


def _get_launch_lock():
    global _browser_lock
    if _browser_lock is None:
        _browser_lock = asyncio.Lock()
    return _browser_lock


def _kill_all_msedge():
    try:
        import psutil
        killed = 0
        for proc in psutil.process_iter(["pid", "name"]):
            try:
                if (proc.info.get("name") or "").lower() != "msedge.exe":
                    continue
                proc.kill()
                killed += 1
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return killed
    except ImportError:
        return 0
    except Exception:
        return 0


def _get_edge_bot_profile_dir() -> str:
    custom_dir = getattr(Config, "EDGE_PROFILE_DIR", "") or ""
    if custom_dir.strip():
        return custom_dir.strip()
    return str(Path(__file__).resolve().parent / "edge_bot_profile")


def _launch_edge_debug(cdp_port: int) -> bool:
    exe = getattr(Config, "EDGE_EXECUTABLE_PATH", r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")

    if not Path(exe).exists():
        alt = r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"
        if Path(alt).exists():
            logger.warning(f"⚠️ [Edge-CDP] EDGE_EXECUTABLE_PATH không tồn tại ({exe}) — dùng: {alt}")
            exe = alt
        else:
            logger.critical(
                f"❌ [Edge-CDP] Không tìm thấy msedge.exe ở cả 2 vị trí:\n"
                f"   - {exe}\n   - {alt}\n👉 Kiểm tra lại EDGE_EXECUTABLE_PATH trong .env"
            )
            return False

    profile_dir = _get_edge_bot_profile_dir()
    profile_name = getattr(Config, "EDGE_PROFILE_NAME", "") or "Default"

    try:
        import subprocess
        subprocess.Popen(
            [
                exe,
                f"--remote-debugging-port={cdp_port}",
                "--remote-debugging-address=127.0.0.1",
                f"--remote-allow-origins=http://localhost:{cdp_port},http://127.0.0.1:{cdp_port}",
                "--disable-blink-features=AutomationControlled",
                f"--user-data-dir={profile_dir}",
                f"--profile-directory={profile_name}",
                "--disable-background-networking",
                "--disable-sync",
                "--disable-translate",
                "--disable-component-update",
                "--disable-domain-reliability",
                "--disable-client-side-phishing-detection",
                "--disable-default-apps",
                "--no-first-run",
                "--no-default-browser-check",
                "--mute-audio",
                "--disable-features=Translate,OptimizationHints,MediaRouter,DialMediaRouteProvider,AutofillServerCommunication",
            ],
            creationflags=getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
        return True
    except Exception as e:
        logger.error(f"❌ [Edge-CDP] Không tự mở lại được Edge ({exe}): {e}")
        return False


async def get_or_launch_browser_context(user: str = "shared", force_reconnect: bool = False):
    global _pw_instance, _edge_browser, _shared_context, _last_launch_time

    if not force_reconnect and _shared_context is not None:
        try:
            _ = _shared_context.pages
            return _shared_context
        except Exception:
            _shared_context = None

    async with _get_launch_lock():
        if not force_reconnect and _shared_context is not None:
            try:
                _ = _shared_context.pages
                return _shared_context
            except Exception:
                _shared_context = None

        if _pw_instance is None:
            _pw_instance = await async_playwright().start()

        cdp_host = getattr(Config, "EDGE_CDP_HOST", "127.0.0.1")
        cdp_port = getattr(Config, "EDGE_CDP_PORT", 9222)
        cdp_url = f"http://{cdp_host}:{cdp_port}"

        logger.info(f"[Edge-CDP] Đang kết nối vào Edge đang chạy tại {cdp_url}...")
        _edge_browser = None
        last_err = None

        now = time.time()
        just_launched = (now - _last_launch_time) < _LAUNCH_COOLDOWN
        max_attempts = 1 if just_launched else 2

        for attempt in range(1, max_attempts + 1):
            try:
                _edge_browser = await _pw_instance.chromium.connect_over_cdp(cdp_url, timeout=15000)
                break
            except Exception as e:
                last_err = e
                if just_launched:
                    wait_left = max(1.0, _LAUNCH_COOLDOWN - (time.time() - _last_launch_time))
                    logger.warning(
                        f"⚠️ [Edge-CDP] Kết nối thất bại nhưng Edge vừa được mở lại gần đây "
                        f"({wait_left:.0f}s trước) — ĐỢI THÊM thay vì kill lại: {e}"
                    )
                    await asyncio.sleep(min(wait_left + 3.0, 15.0))
                    just_launched = False
                    continue

                logger.warning(
                    f"⚠️ [Edge-CDP] Kết nối thất bại (lần {attempt}/{max_attempts}): {e} — "
                    f"không đóng các tiến trình Edge hiện có; thử mở Edge debug riêng..."
                )
                await asyncio.sleep(1)
                if _launch_edge_debug(cdp_port):
                    _last_launch_time = time.time()
                    logger.info("[Edge-CDP] Đã gửi lệnh mở lại Edge — chờ khởi động...")
                    await asyncio.sleep(10)

        if _edge_browser is None:
            logger.critical(
                f"❌ [Edge-CDP] Không kết nối được tới Edge ({cdp_url}) sau khi đã thử tự mở lại: {last_err}\n"
                f"👉 Có thể do: Edge bị chặn bởi firewall/antivirus trên cổng {getattr(Config, 'EDGE_CDP_PORT', 9222)}, "
                f"hoặc \"Tiếp tục chạy ứng dụng nền\" đang bật trong edge://settings/system."
            )
            raise last_err if last_err else RuntimeError("Edge CDP connect failed")

        if not _edge_browser.contexts:
            logger.error(
                "❌ [Edge-CDP] Kết nối được nhưng Edge không có context/tab nào đang mở — "
                "mở ít nhất 1 tab trong Edge trước khi chạy bot."
            )
            raise RuntimeError("Edge CDP: no existing browser context")

        _shared_context = _edge_browser.contexts[0]
        logger.info(f"[Edge-CDP] ✅ Đã kết nối — dùng context hiện có ({len(_shared_context.pages)} tab đang mở)")
        return _shared_context


_browser_hwnd: int = 0
_last_restore_at: float = 0.0


def _find_browser_hwnd() -> int:
    global _browser_hwnd
    if _browser_hwnd:
        if ctypes.windll.user32.IsWindow(_browser_hwnd):
            return _browser_hwnd
        _browser_hwnd = 0

    found = []
    WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)

    def _cb(hwnd, _):
        if not ctypes.windll.user32.IsWindowVisible(hwnd):
            return True
        length = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
        if length == 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        ctypes.windll.user32.GetWindowTextW(hwnd, buf, length + 1)
        title = buf.value.lower()
        if any(k in title for k in ("edge", "microsoft edge", "chrome")):
            rect = ctypes.wintypes.RECT()
            ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect))
            w = rect.right - rect.left
            h = rect.bottom - rect.top
            if w > 100 and h > 50:
                found.append((w * h, hwnd))
        return True

    ctypes.windll.user32.EnumWindows(WNDENUMPROC(_cb), 0)
    if not found:
        return 0
    found.sort(key=lambda x: x[0], reverse=True)
    _browser_hwnd = found[0][1]
    return _browser_hwnd


def edge_restore():
    global _last_restore_at
    try:
        now = time.monotonic()
        if now - _last_restore_at < 2.0:
            return
        _last_restore_at = now
        hwnd = _find_browser_hwnd()
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 9)  # SW_RESTORE
            logger.debug("🔼 Edge restored")
    except Exception as e:
        logger.debug(f"browser_restore error: {e}")


# ============================================================
# CLOUDFLARE DETECTION
# ============================================================
async def _cf_already_passed(page, domain: str = "") -> bool:
    try:
        passed = await page.evaluate(
            """
            () => {
                const text = (document.body.innerText || '').toLowerCase();
                const successMarkers = ['thành công', 'thanh cong', 'xác thực thành công', 'verified', 'success'];
                return successMarkers.some(m => text.includes(m));
            }
            """
        )
        if passed:
            return True
    except Exception:
        pass

    # Turnstile stores a successful response in a hidden host-page input.
    # Never infer success from the API script being loaded; require a token.
    try:
        turnstile_token = await page.evaluate(
            """
            () => {
                const fields = [...document.querySelectorAll(
                    'input[name="cf-turnstile-response"], textarea[name="cf-turnstile-response"]'
                )];
                return fields.some((el) => String(el.value || el.textContent || '').trim().length > 20);
            }
            """
        )
        if turnstile_token:
            return True
    except Exception:
        pass

    # Google reCAPTCHA renders inside a cross-origin iframe, but its verified
    # token is mirrored into a hidden textarea in the host document. The
    # visible "Xác thực" modal button may remain present after this token is
    # issued, so use the token as a pass signal before treating that button as
    # an active challenge.
    try:
        recaptcha_token = await page.evaluate(
            """
            () => [...document.querySelectorAll(
                'textarea[name="g-recaptcha-response"], textarea[name*="recaptcha"]'
            )].some((el) => String(el.value || el.textContent || '').trim().length > 20)
            """
        )
        if recaptcha_token:
            return True
    except Exception:
        pass

    return False


async def _wait_for_turnstile_token(page, timeout_seconds: float = 15.0) -> bool:
    """Wait for the site's own Turnstile token; do not solve or bypass it."""
    try:
        state = await page.evaluate(
            """
            () => {
                const selector = 'input[name="cf-turnstile-response"], textarea[name="cf-turnstile-response"]';
                const fields = [...document.querySelectorAll(selector)];
                const ready = fields.some((el) => String(el.value || el.textContent || '').trim().length > 20);
                const widget = !!document.querySelector(
                    'input[name="cf-turnstile-response"], textarea[name="cf-turnstile-response"], '
                    + '.cf-turnstile, [data-sitekey], iframe[src*="challenges.cloudflare.com"]'
                );
                return {widget, ready};
            }
            """
        )
        if state and state.get("ready"):
            return True
        timeout_ms = max(500, min(60000, int(float(timeout_seconds) * 1000)))
        await page.wait_for_function(
            """() => [...document.querySelectorAll(
                'input[name="cf-turnstile-response"], textarea[name="cf-turnstile-response"]'
            )].some((el) => String(el.value || el.textContent || '').trim().length > 20)""",
            timeout=timeout_ms,
            polling=200,
        )
        return True
    except Exception:
        return False


async def is_cloudflare_present(page, domain: str = "") -> bool:
    # Turnstile can leave the modal and its enabled "Xác thực" button visible
    # after the widget already reports "Thành công!". Check the success state
    # first; otherwise the visible button becomes a false-positive challenge.
    try:
        if await _cf_already_passed(page, domain=domain):
            return False
    except Exception:
        pass

    try:
        verify_button_visible = await page.evaluate(
            """
            () => {
                const buttons = [...document.querySelectorAll('button')];
                for (const btn of buttons) {
                    const txt = (btn.innerText || btn.textContent || '').trim().toLowerCase();
                    if (txt === 'xác thực' || txt === 'xac thuc') {
                        const rect = btn.getBoundingClientRect();
                        return rect.width > 0 && rect.height > 0;
                    }
                }
                return false;
            }
            """
        )
    except Exception:
        verify_button_visible = False

    if verify_button_visible:
        return True

    # A present but empty Turnstile response means the challenge is unresolved,
    # even when its iframe is cross-origin/hidden and not in CF_SELECTORS.
    try:
        turnstile_pending = await page.evaluate(
            """
            () => {
                const fields = [...document.querySelectorAll(
                    'input[name="cf-turnstile-response"], textarea[name="cf-turnstile-response"]'
                )];
                return fields.length > 0 && !fields.some(
                    (el) => String(el.value || el.textContent || '').trim().length > 20
                );
            }
            """
        )
        if turnstile_pending:
            return True
    except Exception:
        pass

    # ✅ TỐI ƯU: trước đây loop Python qua từng selector trong CF_SELECTORS,
    # mỗi selector tốn 1 round-trip query_selector() + 1 round-trip
    # is_visible() riêng (tối đa 4*2=8 lệnh CDP tuần tự). Gộp thành ĐÚNG 1
    # evaluate() — JS tự quét + kiểm tra visibility nội bộ trong ~1ms thay
    # vì Python phải chờ qua lại CDP nhiều lần.
    try:
        cf_widget_found = bool(await page.evaluate(
            """
            (selectors) => {
                const visible = (el) => {
                    if (!el) return false;
                    const s = getComputedStyle(el), r = el.getBoundingClientRect();
                    return s.display !== 'none' && s.visibility !== 'hidden'
                        && r.width > 0 && r.height > 0;
                };
                for (const sel of selectors) {
                    try {
                        if (visible(document.querySelector(sel))) return true;
                    } catch (_) {}
                }
                return false;
            }
            """,
            CF_SELECTORS,
        ))
    except Exception:
        cf_widget_found = False

    if cf_widget_found:
        return True

    try:
        load_failed = await page.evaluate(
            """
            () => {
                const markers = ['không tải được captcha', 'khong tai duoc captcha', 'error', 'thử lại', 'thu lai'];
                const text = (document.body.innerText || '').toLowerCase();
                return markers.some(m => text.includes(m)) &&
                       (text.includes('captcha') || text.includes('turnstile') || text.includes('cloudflare'));
            }
            """
        )
        if load_failed:
            return True
    except Exception:
        pass

    return False


async def safe_is_visible(element) -> bool:
    try:
        return await element.is_visible()
    except Exception:
        return False


def safe_is_closed(page) -> bool:
    try:
        if page is None:
            return True
        return page.is_closed()
    except Exception:
        return True


# ============================================================
# INPUT FIELDS
# ============================================================
def _invalidate_input_cache(key: str):
    bot_state._input_cache.pop(key, None)


async def find_input_fields(page, cache_key: str = None, domain: str = ""):
    now = time.time()

    if cache_key:
        cached = bot_state._input_cache.get(cache_key)
        if cached:
            username_input, code_input, cache_time = cached
            if now - cache_time < bot_state._input_cache_ttl:
                try:
                    if code_input:
                        visible = await code_input.is_visible()
                        if visible:
                            return username_input, code_input
                    _invalidate_input_cache(cache_key)
                except Exception:
                    _invalidate_input_cache(cache_key)

    username_input = None
    code_input = None
    domain_username_selectors = _get_domain_username_selectors(domain)

    username_selectors = domain_username_selectors + [
        "#account-code", "#username-input", "#ten_tai_khoan",
        "input#username", "input[name='username']",
        "input[placeholder*='người dùng' i]", "input[placeholder*='tên' i]",
        "input[placeholder*='tài' i]", "input[placeholder*='tài khoản' i]",
        "input[placeholder*='user' i]", "input[placeholder*='đăng nhập' i]",
        "input[name='ten_tai_khoan']", "input[id='username']", "input[type='text']",
    ]

    profile = get_site_profile(domain)
    domain_code_selectors = list(profile.code_selectors) if profile else []
    code_selectors = domain_code_selectors + [
        "#enter-code-code", "#promo-code", "#giftcode-input", "input[placeholder='Nhập mã']", "input[autocomplete='one-time-code']",
        "input#code", "input[name='code']", "input[placeholder*='mã code' i]",
        "input[placeholder*='code' i]", "input[placeholder*='mã' i]",
        "input[name='giftcode']", "input[id='code']", "input[id*='code' i]", "input[id*='promo' i]",
    ]

    try:
        selector_result = await page.evaluate(
            """
            ({usernameSelectors, codeSelectors}) => {
                const visible = (el) => {
                    if (!el || el.disabled) return false;
                    const s = getComputedStyle(el), r = el.getBoundingClientRect();
                    return s.display !== 'none' && s.visibility !== 'hidden' &&
                           r.width > 0 && r.height > 0;
                };
                const first = (selectors) => {
                    for (const sel of selectors) {
                        try {
                            const el = document.querySelector(sel);
                            if (visible(el)) return sel;
                        } catch (_) {}
                    }
                    return null;
                };
                return {username: first(usernameSelectors), code: first(codeSelectors)};
            }
            """,
            {"usernameSelectors": username_selectors, "codeSelectors": code_selectors},
        )
        if selector_result:
            if selector_result.get("username"):
                username_input = await page.query_selector(selector_result["username"])
            if selector_result.get("code"):
                code_input = await page.query_selector(selector_result["code"])

        if not username_input or not code_input:
            inputs = await page.query_selector_all(
                "input:not([type='hidden']):not([type='checkbox']):not([type='radio']):not([type='submit'])"
            )
            # ✅ TỐI ƯU: trước đây loop Python gọi await inp.is_visible()
            # cho TỪNG input handle (mỗi cái 1 round-trip CDP riêng — có
            # thể 5-10+ input trên 1 trang). Gộp thành 1 evaluate() DUY
            # NHẤT nhận cả mảng handle làm arg (Playwright tự chuyển từng
            # ElementHandle trong list thành DOM element tương ứng phía
            # JS), trả về mảng bool visibility tương ứng theo đúng thứ tự.
            visible_inputs = []
            if inputs:
                try:
                    visibility = await page.evaluate(
                        """
                        (elements) => elements.map((el) => {
                            const s = getComputedStyle(el), r = el.getBoundingClientRect();
                            return s.display !== 'none' && s.visibility !== 'hidden'
                                && r.width > 0 && r.height > 0;
                        })
                        """,
                        inputs,
                    )
                except Exception:
                    visibility = [False] * len(inputs)
                visible_inputs = [inp for inp, is_vis in zip(inputs, visibility) if is_vis]
            if len(visible_inputs) >= 2:
                if not username_input:
                    username_input = visible_inputs[0]
                if not code_input:
                    code_input = visible_inputs[1]
            elif len(visible_inputs) == 1 and not code_input:
                code_input = visible_inputs[0]

    except Exception as e:
        logger.debug(f"⚠️ Error finding input fields: {e}")

    if cache_key and code_input:
        bot_state._input_cache[cache_key] = (username_input, code_input, now)

    return username_input, code_input


async def scroll_to_input_fields(page):
    try:
        found = await page.evaluate(
            """
            () => {
                const inputs = document.querySelectorAll('input[type="text"], input:not([type="hidden"])');
                if (inputs.length > 0) {
                    const firstInput = inputs[0];
                    firstInput.scrollIntoView({behavior: 'auto', block: 'center'});
                    firstInput.focus();
                    return true;
                }
                return false;
            }
            """
        )
        return found
    except Exception as e:
        logger.debug(f"⚠️ Scroll error: {e}")
        return False


async def open_mm88_code_form(page) -> bool:
    """MM88 may land on its home shell before exposing the code form."""
    try:
        clicked = await page.evaluate(
            """
            () => {
                const nodes = [...document.querySelectorAll('a,button,[role="button"],span')];
                const target = nodes.find((el) => {
                    const text = (el.innerText || el.textContent || '').trim().toLowerCase();
                    return text === 'nhập code' || text === 'nhap code';
                });
                if (!target) return false;
                const clickable = target.closest('a,button,[role="button"]') || target;
                clickable.click();
                return true;
            }
            """
        )
        if not clicked:
            return False
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=3000)
        except Exception:
            pass
        await asyncio.sleep(0.05)
        return True
    except Exception as e:
        logger.debug(f"⚠️ MM88 Nhập Code navigation lỗi: {e}")
        return False


# ============================================================
# SUBMIT BUTTON CLICKING
# ============================================================
async def click_verification_button_if_present(page, domain: str = "") -> bool | None:
    if domain not in {"tangquaqq88.com", "hi88-freecode.pages.dev"}:
        logger.debug(f"ℹ️ [{domain or 'unknown'}] Bỏ qua nút Xác thực: domain không yêu cầu")
        return False
    try:
        site_wait = getattr(
            Config,
            "QQ88_VERIFICATION_WAIT_SECONDS" if domain == "tangquaqq88.com"
            else "HI88_VERIFICATION_WAIT_SECONDS",
            getattr(Config, "VERIFICATION_BUTTON_WAIT_SECONDS", 15.0),
        )
        wait_seconds = min(30.0, max(1.0, float(site_wait)))
        # Treat the button's own enabled state as authoritative. Cloudflare can
        # already say "Thành công!" while the site still requires this final
        # React click; is_cloudflare_present() intentionally reports that
        # challenge as passed, so it must not gate the final confirmation.
        status_js = r"""() => {
            const normalize = (value) => String(value || '').normalize('NFD')
                .replace(/[\u0300-\u036f]/g, '').replace(/đ/gi, 'd')
                .replace(/\s+/g, ' ').trim().toLowerCase();
            const visible = (el) => {
                const r = el.getBoundingClientRect(), s = getComputedStyle(el);
                return r.width > 0 && r.height > 0 && s.display !== 'none'
                    && s.visibility !== 'hidden' && s.opacity !== '0';
            };
            const candidates = [...document.querySelectorAll('button,[role="button"]')]
                .filter((el) => ['xac thuc', 'verify'].includes(
                    normalize(el.innerText || el.textContent)
                ) && visible(el));
            if (!candidates.length) return {present: false, enabled: false};
            const button = candidates[candidates.length - 1];
            return {
                present: true,
                enabled: !button.disabled && button.getAttribute('aria-disabled') !== 'true',
            };
        }"""
        try:
            state = await page.evaluate(status_js)
        except Exception:
            state = {"present": False, "enabled": False}

        if not state.get("present"):
            try:
                has_verification_context = await page.evaluate(
                    """() => {
                        const visible = (el) => {
                            if (!el) return false;
                            const r=el.getBoundingClientRect(), s=getComputedStyle(el);
                            return r.width>0 && r.height>0 && s.display!=='none' && s.visibility!=='hidden';
                        };
                        const widget = [...document.querySelectorAll(
                            '.cf-turnstile,[data-sitekey],iframe[src*="turnstile"],'
                            +'iframe[src*="challenges.cloudflare.com"],iframe[src*="recaptcha"]'
                        )].some(visible);
                        const dialogs = [...document.querySelectorAll(
                            '[role="dialog"],[role="alertdialog"],.modal,[class*="modal" i]'
                        )].filter(visible);
                        const text = dialogs.map((el)=>el.innerText || el.textContent || '').join(' ').toLowerCase();
                        return widget || /mã xác thực|ma xac thuc|captcha|turnstile|cloudflare|hoàn tất xác minh|hoan tat xac minh/.test(text);
                    }"""
                )
            except Exception:
                has_verification_context = False
            if not has_verification_context:
                return False
            try:
                await page.wait_for_function(
                    """() => {
                        const n = (v) => String(v || '').normalize('NFD')
                            .replace(/[\\u0300-\\u036f]/g, '').replace(/đ/gi, 'd')
                            .replace(/\\s+/g, ' ').trim().toLowerCase();
                        return [...document.querySelectorAll('button,[role="button"]')].some((el) => {
                            const r=el.getBoundingClientRect(), s=getComputedStyle(el);
                            return ['xac thuc','verify'].includes(n(el.innerText || el.textContent))
                                && r.width>0 && r.height>0 && s.display!=='none' && s.visibility!=='hidden';
                        });
                    }""",
                    timeout=int(wait_seconds * 1000),
                    polling="mutation",
                )
                state = await page.evaluate(status_js)
            except Exception:
                return False

        if not state.get("enabled"):
            try:
                await page.wait_for_function(
                    """() => {
                        const n = (v) => String(v || '').normalize('NFD')
                            .replace(/[\\u0300-\\u036f]/g, '').replace(/đ/gi, 'd')
                            .replace(/\\s+/g, ' ').trim().toLowerCase();
                        return [...document.querySelectorAll('button,[role="button"]')].some((el) => {
                            const r=el.getBoundingClientRect(), s=getComputedStyle(el);
                            return ['xac thuc','verify'].includes(n(el.innerText || el.textContent))
                                && r.width>0 && r.height>0 && s.display!=='none' && s.visibility!=='hidden'
                                && !el.disabled && el.getAttribute('aria-disabled')!=='true';
                        });
                    }""",
                    timeout=int(wait_seconds * 1000),
                    polling="mutation",
                )
                state = await page.evaluate(status_js)
            except Exception:
                logger.warning(
                    f"⏸️ [{domain}] Nút Xác thực vẫn disabled sau {wait_seconds:.1f}s; "
                    "giữ nguyên trang, không bypass trạng thái disabled"
                )
                return None

        if not state.get("present") or not state.get("enabled"):
            return None

        # Use a trusted browser click so React receives the user-like event.
        verify_button = page.locator("button").filter(
            has_text=_re.compile(r"^\s*(?:xác thực|xac thuc|verify)\s*$", _re.IGNORECASE)
        ).last
        if not await verify_button.count():
            logger.warning(f"⚠️ [{domain}] Enabled verification button not found by Playwright locator")
            return None
        if not await verify_button.is_visible() or not await verify_button.is_enabled():
            return None
        await verify_button.click(timeout=2500, no_wait_after=True)
        logger.info(f"✅ [Browser|{domain}] Playwright clicked enabled Xác thực button")
        return True
    except Exception as exc:
        logger.warning(f"⚠️ [{domain}] verification button check lỗi: {exc}")
        return None


async def click_submit_fast(page, domain: str = "") -> bool:
    profile = get_site_profile(domain)
    domain_sel = profile.submit_selector if profile else None
    if domain_sel:
        try:
            locator = page.locator(domain_sel).first
            await locator.wait_for(state="visible", timeout=300)
            await locator.click(timeout=700)
            logger.debug(f"✅ Playwright-clicked domain-specific button: {domain}")
            return True
        except Exception:
            pass
        try:
            clicked = await page.evaluate(
                """
                async (sel) => {
                    const deadline = Date.now() + 300;
                    while (Date.now() < deadline) {
                        const btn = document.querySelector(sel);
                        if (btn && !btn.disabled) {
                            const rect = btn.getBoundingClientRect();
                            if (rect.width > 0 && rect.height > 0) {
                                btn.click();
                                return true;
                            }
                        }
                        await new Promise(r => setTimeout(r, 50));
                    }
                    const btn = document.querySelector(sel);
                    if (btn) { btn.click(); return true; }
                    return false;
                }
                """,
                domain_sel,
            )
            if clicked:
                logger.debug(f"✅ Clicked domain-specific button: {domain}")
                return True
        except Exception:
            pass

    # Complex SPAs commonly render duplicate buttons in hidden menus, stale
    # portals, or unrelated modals.  Rank candidates by form context instead
    # of clicking the first keyword match.
    try:
        discovery = await page.evaluate(
            """
            ({domainSelector, codeSelectors}) => {
                const norm = (v) => String(v || '').normalize('NFD')
                    .replace(/[\\u0300-\\u036f]/g, '').replace(/đ/gi, 'd')
                    .replace(/\\s+/g, ' ').trim().toLowerCase();
                const visible = (el) => {
                    if (!el || !el.isConnected) return false;
                    const s = getComputedStyle(el), r = el.getBoundingClientRect();
                    return s.display !== 'none' && s.visibility !== 'hidden'
                        && s.opacity !== '0' && r.width > 0 && r.height > 0;
                };
                const enabled = (el) => !el.disabled
                    && el.getAttribute('aria-disabled') !== 'true' && !el.inert;
                const roots = [document];
                for (let i = 0; i < roots.length; i++) {
                    for (const node of roots[i].querySelectorAll('*')) {
                        if (node.shadowRoot) roots.push(node.shadowRoot);
                    }
                }
                const all = [], seen = new Set();
                const buttonSelector = 'button,[role="button"],input[type="button"],input[type="submit"],a[role="button"]';
                for (const root of roots) for (const el of root.querySelectorAll(buttonSelector)) {
                    if (seen.has(el) || !visible(el) || !enabled(el)) continue;
                    seen.add(el);
                    const text = norm(el.innerText || el.textContent || el.value);
                    const aria = norm(el.getAttribute('aria-label'));
                    const title = norm(el.getAttribute('title'));
                    const imgAlt = norm([...el.querySelectorAll('img[alt],svg[aria-label]')]
                        .map(n => n.getAttribute('alt') || n.getAttribute('aria-label')).join(' '));
                    const label = [text, aria, title, imgAlt].filter(Boolean).join(' ');
                    if (!label || /menu|nav|home|close|cancel|toggle|hamburger|back|tro ve|huy|dong|xac thuc|verify|captcha|turnstile/.test(label)) continue;
                    let score = 0, reasons = [];
                    for (const [word, points] of [['kiem tra ngay',120],['kiem tra',100],['nhan code',95],['nhan ngay',80],['ap dung',80],['doi code',75],['nap code',75],['submit',70],['apply',70],['gui',55]]) {
                        if (label.includes(word)) { score += points; reasons.push(word); break; }
                    }
                    const type = norm(el.getAttribute('type'));
                    if (type === 'submit') { score += 90; reasons.push('type=submit'); }
                    const form = el.form || el.closest('form');
                    let codeInput = null;
                    for (const selector of (codeSelectors || [])) {
                        try { codeInput = (form || document).querySelector(selector); if (codeInput) break; } catch (_) {}
                    }
                    if (!codeInput) codeInput = (form || document).querySelector('input[autocomplete="one-time-code"],input[name*="code" i],input[id*="code" i],textarea[name*="code" i]');
                    if (codeInput) {
                        score += 65; reasons.push('same-form-code-input');
                        const a = el.getBoundingClientRect(), b = codeInput.getBoundingClientRect();
                        score += Math.max(0, 35 - Math.min(35, Math.hypot(a.x-b.x, a.y-b.y) / 40));
                    }
                    if (form) { score += 20; reasons.push('form'); }
                    const cls = norm(typeof el.className === 'string' ? el.className : '');
                    if (/submit|apply|promo|gift|code|check/.test(cls)) score += 25;
                    if (['fixed','sticky'].includes(getComputedStyle(el).position)) score -= 25;
                    try { if (domainSelector && el.matches(domainSelector)) { score += 150; reasons.push('profile-selector'); } } catch (_) {}
                    all.push({el, score, reasons, text, aria, title, imgAlt, type});
                }
                all.sort((a,b) => b.score - a.score);
                const best = all[0];
                if (!best || best.score < 40) return {clicked:false, candidates:all.slice(0,5).map(({el,...x})=>x)};
                best.el.click();
                return {clicked:true, selected:{score:Math.round(best.score), reasons:best.reasons, text:best.text, aria:best.aria, title:best.title, imgAlt:best.imgAlt, type:best.type}, candidates:all.slice(0,5).map(({el,...x})=>({score:Math.round(x.score), text:x.text, aria:x.aria, reasons:x.reasons}))};
            }
            """,
            {"domainSelector": domain_sel, "codeSelectors": list(profile.code_selectors) if profile else []},
        )
        if discovery and discovery.get("clicked"):
            selected = discovery.get("selected") or {}
            logger.debug(
                "✅ Context-aware submit button: %s | score=%s | reasons=%s | candidates=%s",
                domain, selected.get("score"), ",".join(selected.get("reasons") or []),
                len(discovery.get("candidates") or []),
            )
            return True
    except Exception as exc:
        logger.debug("⚠️ [%s] Context-aware button discovery lỗi: %s", domain, exc)

    # ✅ TỐI ƯU: gộp fallback cuối (7 selector chung) thành 1 evaluate duy
    # nhất — trước đây mỗi selector tốn round-trip query_selector() +
    # is_visible() + (nếu khớp) 1 evaluate click riêng.
    generic_selectors = [
        "button[type='submit']", "input[type='submit']", ".btn-submit",
        ".apply-btn", ".submit-btn", "[class*='submit' i]", "[class*='apply' i]",
    ]
    try:
        clicked = await page.evaluate(
            """
            (selectors) => {
                const visible = (el) => {
                    if (!el) return false;
                    const s = getComputedStyle(el), r = el.getBoundingClientRect();
                    return s.display !== 'none' && s.visibility !== 'hidden'
                        && r.width > 0 && r.height > 0;
                };
                for (const sel of selectors) {
                    try {
                        const el = document.querySelector(sel);
                        if (visible(el)) { el.click(); return true; }
                    } catch (_) {}
                }
                return false;
            }
            """,
            generic_selectors,
        )
        if clicked:
            return True
    except Exception:
        pass

    try:
        await page.keyboard.press("Enter")
        return True
    except Exception:
        return False


# ============================================================
# RESULT DETECTION
# ============================================================
def _filter_nextjs_noise(text: str) -> str:
    if not text:
        return ""
    noise_markers = [
        "__next_f", "__NEXT", "self.__next", 'push([1,"', '"stylesheet"',
        '"link"', "webpack", "hydrat", '"rel":', '"href":', ':[[[\"$\"',
    ]
    t = text.strip()
    for marker in noise_markers:
        if marker in t:
            return ""
    if t.startswith(('{"', '[["', '[[["', "self.")):
        return ""
    return t


_TRANSIENT_CF_PATTERNS = [
    "captcha", "turnstile", "xác thực người dùng",
    "đang xử lý", "dang xu ly", "đang tải", "dang tai",
    "đang kiểm tra", "dang kiem tra", "checking",
    "vui lòng đợi", "vui long doi", "please wait",
    "processing", "verifying", "đang xác thực", "dang xac thuc",
]


def _is_transient_captcha_text(text: str) -> bool:
    if not text:
        return False
    low = text.strip().lower()
    if any(p in low for p in _TRANSIENT_CF_PATTERNS):
        return True
    BUTTON_ONLY_WORDS = {"hủy", "huy", "xác thực", "xac thuc", "đóng", "dong", "cancel", "verify", "ok", "close"}
    tokens = [t.strip() for t in _re.split(r"[\n/|,]+", low) if t.strip()]
    if tokens and len(low) <= 40 and all(t in BUTTON_ONLY_WORDS for t in tokens):
        return True
    return False


async def _detect_result_by_text_diff(page, before_text: str) -> str:
    try:
        after_text = await page.evaluate("() => document.body.innerText || ''")
    except Exception:
        return ""
    if not after_text:
        return ""

    before_lines = {l.strip() for l in (before_text or "").splitlines() if l.strip()}
    new_lines = []
    for line in after_text.splitlines():
        line = line.strip()
        if not line or line in before_lines:
            continue
        if len(line) < 3:
            continue
        clean = _filter_nextjs_noise(line)
        if not clean:
            continue
        if _is_transient_captcha_text(clean):
            continue
        new_lines.append(clean)

    if not new_lines:
        return ""
    return " ".join(new_lines[:6])


_DETECT_RESULT_JS = r"""
(args) => {
    const { orderedSelectors, combinedSelectors, includeGlobal } = args;
    const readSelector = (sel) => {
        try {
            const els = document.querySelectorAll(sel);
            const texts = [];
            for (const el of els) {
                const t = (el.innerText || el.textContent || '').trim();
                if (t) texts.push(t);
            }
            return texts.join(' ');
        } catch (e) {
            return '';
        }
    };
    const ordered = orderedSelectors.map(readSelector);
    const combinedParts = [];
    if (includeGlobal) {
        for (const sel of combinedSelectors) {
            const t = readSelector(sel);
            if (t) combinedParts.push(t);
        }
    }
    if (!includeGlobal) {
        return {ordered, combinedText: '', keywordText: '', bodyText: ''};
    }
    const keywords = [
        'thành công', 'thanh cong', 'thất bại', 'that bai', 'sai', 'lỗi', 'loi',
        'đã sử dụng', 'da su dung', 'success', 'failed', 'error', 'invalid', 'used',
        'không hợp lệ', 'khong hop le', 'hết hạn', 'het han', 'không đúng', 'không tồn tại',
    ];
    const noisePatterns = ['__next_f', '__NEXT', 'self.__next', 'push([', 'webpack'];
    let keywordText = '';
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, null, false);
    let node;
    while (node = walker.nextNode()) {
        const parent = node.parentElement;
        if (!parent || ['SCRIPT', 'STYLE', 'NOSCRIPT'].includes(parent.tagName)) continue;
        const txt = (node.textContent || '').trim();
        if (txt.length < 3 || noisePatterns.some(p => txt.includes(p))) continue;
        if (keywords.some(k => txt.toLowerCase().includes(k))) {
            keywordText = txt;
            break;
        }
    }
    return {
        ordered,
        combinedText: combinedParts.join(' '),
        keywordText,
        bodyText: (document.body.innerText || '').slice(0, 30000),
    };
}
"""


_WS_RE = _re.compile(r"\s+")


def _normalize_ws(text: str) -> str:
    return _WS_RE.sub(" ", (text or "")).strip().lower()


def _is_stale_static_text(candidate: str, before_text: str) -> bool:
    """True nếu 'candidate' đã xuất hiện y hệt trên trang TRƯỚC khi bấm
    submit (before_text chụp lúc đó). Một số site (vd tangquaqq88.com) có
    banner cảnh báo tĩnh (vd "QQ88 LINK CHÍNH THỨC...") luôn nằm sẵn trong
    DOM và tình cờ khớp 1 trong các selector chung (PRIORITY_SELECTORS) —
    nếu không lọc, banner này bị đọc nhầm thành kết quả submit ngay ở lần
    poll ĐẦU TIÊN (trước khi popup thật kịp hiện ra), khiến vòng lặp thoát
    sớm với nội dung sai (AMBIGUOUS/NO_RESULT giả) dù site chưa trả lời gì.
    So khớp theo substring sau khi chuẩn hoá khoảng trắng — nội dung kết
    quả thật (thành công/sai/hết hạn...) gần như không bao giờ trùng khớp
    y hệt với text tĩnh đã có sẵn trước đó."""
    if not candidate or not before_text:
        return False
    norm_candidate = _normalize_ws(candidate)
    if len(norm_candidate) < 3:
        return False
    return norm_candidate in _normalize_ws(before_text)


async def detect_result_text(
    page,
    domain: str = "",
    before_text: str = "",
    *,
    selector_only: bool = False,
) -> str:
    domain_selectors = _get_domain_result_selectors(domain)

    PRIORITY_SELECTORS = [
        ".swal2-container", ".swal2-popup", "[role='alertdialog']",
        "#toast-container", ".iziToast-wrapper", ".notyf", ".p-toast",
        ".p-toast-message-content", "[class*='snackbar' i]",
        ".swal2-html-container", ".swal2-title", ".swal2-popup",
        "div[class*='popup'] p", "div[class*='modal'] p", "div[class*='dialog'] p",
        "div[class*='alert'] p", "div[class*='notice'] p", "div[class*='message'] p",
        ".text-red-600", ".text-green-600", ".text-yellow-600",
        ".text-red-500", ".text-green-500", "p.mt-1.text-sm",
        "div[class*='rounded-2xl'] p", "div[class*='rounded-xl'] p", "div[class*='rounded-lg'] p",
        "[role='alert']", "[role='status']", "[role='dialog']",
        "div[style*='position: fixed'] p", "div[style*='position:fixed'] p",
        "[data-sonner-toast] [data-description]", "[data-sonner-toast]",
        ".Toastify__toast-body", ".ant-message-notice-content", ".ant-notification-notice-message",
        "[data-toast]", "[data-radix-toast-viewport] *", "[aria-live]", "output",
        ".van-toast", ".van-dialog", ".el-message", ".el-notification", ".ant-message",
        ".toast", ".modal", "[class*='message']", "[class*='result']",
        "[class*='success']", "[class*='error']",
    ]

    result_selectors = [
        ".swal2-container", "[role='alertdialog']", "#toast-container",
        ".iziToast-wrapper", ".notyf", ".p-toast", ".p-toast-message-content",
        "[class*='snackbar' i]",
        ".text-red-600", ".text-green-600", "p.mt-1.text-sm",
        "div[class*='rounded-2xl'] p", "div[class*='rounded-xl'] p", "div[class*='rounded-lg'] p",
        "[role='dialog']", "[role='alert']", "[role='status']",
        ".modal-body", ".modal-content", ".popup-content", ".alert",
        "[class*='success']", "[class*='error']", "[class*='toast']",
        "[class*='result']", "[class*='notify']", "[class*='modal']",
        "[class*='popup']", "[class*='notification']", "div[style*='position: fixed']",
    ]

    ordered_selectors = domain_selectors + PRIORITY_SELECTORS

    try:
        data = await page.evaluate(
            _DETECT_RESULT_JS,
            {
                "orderedSelectors": domain_selectors if selector_only else ordered_selectors,
                "combinedSelectors": result_selectors,
                "includeGlobal": not selector_only,
            },
        )
    except Exception:
        data = None

    if data:
        for txt in data.get("ordered", []):
            if txt and len(txt.strip()) >= 3:
                clean = _filter_nextjs_noise(txt.strip())
                if not clean or _is_transient_captcha_text(clean):
                    continue
                if _is_stale_static_text(clean, before_text):
                    continue
                return clean

        combined = (data.get("combinedText") or "").strip()
        if len(combined) >= 3 and not _is_transient_captcha_text(combined):
            filtered = _filter_nextjs_noise(combined)
            if filtered and not _is_stale_static_text(filtered, before_text):
                return filtered

    if data:
        page_text = (data.get("keywordText") or "").strip()
        if page_text:
            clean = _filter_nextjs_noise(page_text)
            if clean and not _is_transient_captcha_text(clean) and not _is_stale_static_text(clean, before_text):
                return clean

        after_text = data.get("bodyText") or ""
        if before_text and after_text:
            before_lines = {line.strip() for line in before_text.splitlines() if line.strip()}
            new_lines = []
            for line in after_text.splitlines():
                line = line.strip()
                if not line or line in before_lines or len(line) < 3:
                    continue
                clean = _filter_nextjs_noise(line)
                if clean and not _is_transient_captcha_text(clean):
                    new_lines.append(clean)
            if new_lines:
                return " ".join(new_lines[:6])

    return ""


# ============================================================
# PAGE PERFORMANCE / STEALTH
# ============================================================
async def _setup_page_performance(page, label: str = ""):
    STEALTH_JS = """
        () => {
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined, configurable: true });
            if (!window.chrome) { window.chrome = {}; }
            window.chrome.runtime = {};
            Object.defineProperty(navigator, 'plugins', {
                get: () => ([
                    { name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
                    { name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai', description: '' },
                ]),
                configurable: true,
            });
            Object.defineProperty(navigator, 'languages', { get: () => ['vi-VN', 'vi', 'en-US', 'en'], configurable: true });
            if (window.$cdc_asdjflasutopfhvcZLmcfl_) { delete window.$cdc_asdjflasutopfhvcZLmcfl_; }
            if (window.$wdc_) { delete window.$wdc_; }
            if (navigator.permissions && navigator.permissions.query) {
                const origQuery = navigator.permissions.query;
                navigator.permissions.query = (parameters) =>
                    parameters.name === 'notifications'
                        ? Promise.resolve({ state: Notification.permission })
                        : origQuery(parameters);
            }
            Object.defineProperty(navigator, 'headless', { get: () => false, configurable: true });
            Object.defineProperty(screen, 'width', { get: () => 1920, configurable: true });
            Object.defineProperty(screen, 'height', { get: () => 1080, configurable: true });
            Object.defineProperty(screen, 'availWidth', { get: () => 1920, configurable: true });
            Object.defineProperty(screen, 'availHeight', { get: () => 1040, configurable: true });
            try {
                const getParam = WebGLRenderingContext.prototype.getParameter;
                WebGLRenderingContext.prototype.getParameter = function(parameter) {
                    if (parameter === 37445) return 'Intel Inc.';
                    if (parameter === 37446) return 'Intel Iris OpenGL Engine';
                    return getParam.call(this, parameter);
                };
                const getParam2 = WebGL2RenderingContext.prototype.getParameter;
                WebGL2RenderingContext.prototype.getParameter = function(parameter) {
                    if (parameter === 37445) return 'Intel Inc.';
                    if (parameter === 37446) return 'Intel Iris OpenGL Engine';
                    return getParam2.call(this, parameter);
                };
            } catch(e) {}
            Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8, configurable: true });
            Object.defineProperty(navigator, 'deviceMemory', { get: () => 8, configurable: true });
            Object.defineProperty(navigator, 'connection', {
                get: () => ({ rtt: 50, downlink: 10, effectiveType: '4g', saveData: false }),
                configurable: true,
            });
        }
    """
    try:
        await page.add_init_script(STEALTH_JS)
    except Exception as e:
        logger.debug(f"⚠️ [{label}] add_init_script error: {e}")

    _BLOCK_DOMAINS = (
        "google-analytics", "googletagmanager", "doubleclick", "facebook.net",
        "fbcdn.net", "hotjar", "googlesyndication", "adsystem", "criteo",
        "taboola", "outbrain", "clarity.ms", "sentry.io", "crisp.chat", "tawk.to",
    )
    _BLOCK_TYPES = ("media", "ping", "font")

    async def _handle_route(route):
        req = route.request
        url = req.url.lower()
        rtype = req.resource_type
        if "cloudflare" in url:
            await route.continue_()
            return
        if any(d in url for d in _BLOCK_DOMAINS):
            await route.abort()
            return
        if rtype in _BLOCK_TYPES:
            await route.abort()
            return
        await route.continue_()

    try:
        await page.route("**/*", _handle_route)
    except Exception as e:
        logger.debug(f"⚠️ [{label}] Cannot setup route: {e}")


async def _close_unwanted_popups(page):
    try:
        closed = await page.evaluate(
            """
            () => {
                const hasOverlay = document.querySelector(
                    '.modal, [class*="modal" i], [class*="popup" i], [role="dialog"]'
                );
                if (!hasOverlay) return 0;
                const CLOSE_KEYWORDS = ['đóng', 'close', 'x', 'cancel', 'hủy', 'dismiss', 'got it', 'ok', 'thoát'];
                const SKIP_TEXT = ['xác thực', 'xac thuc', 'submit', 'kiểm tra', 'áp dụng', 'nhận'];
                const OVERLAY_SEL = [
                    '.modal', '[class*="modal" i]', '[class*="popup" i]',
                    '[class*="overlay" i]', '[class*="dialog" i]',
                    '[class*="notification" i]', '[class*="toast" i]',
                    '[class*="alert" i]:not(.alert-success):not(.alert-info)',
                    '[class*="banner" i]', '[class*="announcement" i]',
                ];
                const ICON_CLOSE_PATHS = ['M6 18L18 6M6 6l12 12'];
                let count = 0;
                for (const sel of OVERLAY_SEL) {
                    const els = [...document.querySelectorAll(sel)];
                    for (const el of els) {
                        const style = window.getComputedStyle(el);
                        if (style.display === 'none' || style.visibility === 'hidden') continue;
                        const rect = el.getBoundingClientRect();
                        if (rect.width === 0 || rect.height === 0) continue;
                        const btns = [...el.querySelectorAll('button, [role="button"], a, span')];
                        for (const btn of btns) {
                            const txt = (btn.innerText || btn.textContent || btn.getAttribute('aria-label') || '').trim().toLowerCase();
                            if (SKIP_TEXT.some(s => txt.includes(s))) continue;
                            if (CLOSE_KEYWORDS.some(k => txt === k || txt.startsWith(k))) {
                                btn.click();
                                count++;
                                break;
                            }
                        }
                    }
                }
                const paths = [...document.querySelectorAll('svg path')];
                for (const p of paths) {
                    const d = (p.getAttribute('d') || '').trim();
                    if (!ICON_CLOSE_PATHS.includes(d)) continue;
                    const clickable = p.closest('button, [role="button"], a');
                    if (!clickable) continue;
                    const rect = clickable.getBoundingClientRect();
                    if (rect.width === 0 || rect.height === 0) continue;
                    clickable.click();
                    count++;
                }
                return count;
            }
            """
        )
        if closed and closed > 0:
            logger.debug(f"🧹 Đóng {closed} popup không mong muốn")
    except Exception:
        pass


async def _wake_tab_for_submit(page):
    try:
        await page.bring_to_front()
        await page.evaluate(
            "Object.defineProperty(document, 'visibilityState', { get: () => 'visible', configurable: true });"
        )
        await _close_unwanted_popups(page)
    except Exception:
        pass


# ============================================================
# TAB POOL
# ============================================================
class TabPool:
    def __init__(self, max_per_domain: int = 3, per_domain_overrides: dict | None = None):
        self.max_per_domain = max(1, int(max_per_domain))
        self._per_domain_max: dict = dict(per_domain_overrides or {})
        self._domains: dict = {}
        self._rr_idx: dict = {}
        self._setup_lock = asyncio.Lock()

    @staticmethod
    def _new_entry(page):
        now = time.monotonic()
        return {"page": page, "lock": asyncio.Lock(), "created_at": now, "last_used": now}

    @staticmethod
    def _touch(entry):
        entry["last_used"] = time.monotonic()

    def _max_for(self, domain: str) -> int:
        return max(1, int(self._per_domain_max.get(domain, self.max_per_domain)))

    async def init(self, domain_url_map: dict | None = None):
        context = await get_or_launch_browser_context("shared")
        existing = list(context.pages)
        claimed_ids = set()
        domain_url_map = domain_url_map or {}

        pending_nav = []
        for domain, target_url in domain_url_map.items():
            if not domain or domain in self._domains:
                continue

            # Warm one tab per submit slot at startup. Previously only one tab
            # was preloaded, so the second concurrent task had to open/navigate
            # a cold tab during the live submit path (and could fail fast while
            # the first tab was busy). The pool cap still bounds every domain.
            warm_count = min(self._max_for(domain), self.max_per_domain)
            for slot in range(max(1, warm_count)):
                page = None
                for p in existing:
                    if id(p) in claimed_ids:
                        continue
                    try:
                        url = (p.url or "").lower()
                    except Exception:
                        continue
                    if domain in url:
                        page = p
                        claimed_ids.add(id(p))
                        break

                opened_new = False
                if page is not None:
                    await _setup_page_performance(page, f"dedicated-{domain}-{slot + 1}")
                    logger.info(f"  🗂️ [Startup] '{domain}' → dùng tab có sẵn ({slot + 1}/{warm_count})")
                else:
                    page = await context.new_page()
                    await _setup_page_performance(page, f"dedicated-{domain}-{slot + 1}")
                    opened_new = True
                    logger.info(f"  🌐 [Startup] '{domain}' → mở tab nóng ({slot + 1}/{warm_count})")

                self._domains.setdefault(domain, []).append(self._new_entry(page))
                pending_nav.append((domain, page, opened_new, target_url))

        nav_semaphore = asyncio.Semaphore(5)

        async def _goto_one(domain, page, opened_new, target_url):
            async with nav_semaphore:
                try:
                    need_goto = opened_new
                    if not need_goto:
                        try:
                            need_goto = domain not in (page.url or "").lower()
                        except Exception:
                            need_goto = True
                    if need_goto:
                        await page.goto(
                            target_url,
                            wait_until="domcontentloaded",
                            timeout=int(float(_site_profile_value(domain, "navigation_timeout_seconds", 12.0)) * 1000),
                        )
                        await scroll_to_input_fields(page)
                    await _close_unwanted_popups(page)
                    await page.bring_to_front()
                except Exception as e:
                    logger.warning(f"⚠️ [Startup] '{domain}' lỗi điều hướng: {e}")

        await asyncio.gather(*[
            _goto_one(domain, page, opened_new, target_url)
            for domain, page, opened_new, target_url in pending_nav
        ])

        logger.info(
            f"✅ TabPool: {len(self._domains)} domain đã sẵn sàng. Nếu 1 tab bị Cloudflare "
            f"chặn ngay lúc này, lượt submit tới đó sẽ tự fallback sang browser — "
            f"không cần xác minh tay."
        )

    async def _claim_existing_or_new(self, domain: str) -> dict | None:
        context = await get_or_launch_browser_context("shared")
        claimed_ids = {id(e["page"]) for entries in self._domains.values() for e in entries}
        page = None
        for p in context.pages:
            if id(p) in claimed_ids:
                continue
            try:
                url = (p.url or "").lower()
            except Exception:
                continue
            if domain in url:
                page = p
                break

        if page is not None:
            await _setup_page_performance(page, f"dedicated-{domain}")
            entry = self._new_entry(page)
            self._domains.setdefault(domain, []).append(entry)
            return entry

        if not getattr(Config, "AUTO_OPEN_MISSING_TABS", True):
            logger.info(f"🗂️ [Dedicated] '{domain}' → AUTO_OPEN_MISSING_TABS=False: không mở tab mới")
            return None

        page = await context.new_page()
        await _setup_page_performance(page, f"dedicated-{domain}")
        logger.info(f"🌐 [Dedicated] '{domain}' → chưa có tab sẵn (lazy) → mở tab mới")
        entry = self._new_entry(page)
        self._domains.setdefault(domain, []).append(entry)
        return entry

    async def _new_tab_for_domain(self, domain: str) -> dict:
        context = await get_or_launch_browser_context("shared")
        claimed_ids = {id(e["page"]) for e in self._domains.get(domain, [])}
        for p in context.pages:
            if id(p) in claimed_ids:
                continue
            try:
                url = (p.url or "").lower()
            except Exception:
                continue
            if domain in url:
                await _setup_page_performance(p, f"dedicated-{domain}")
                entry = self._new_entry(p)
                self._domains.setdefault(domain, []).append(entry)
                return entry

        if not getattr(Config, "AUTO_OPEN_MISSING_TABS", True):
            raise RuntimeError("Auto-open missing tabs disabled by config")

        page = await context.new_page()
        await _setup_page_performance(page, f"dedicated-{domain}")
        entry = self._new_entry(page)
        self._domains.setdefault(domain, []).append(entry)
        logger.info(
            f"🗂️ [Dedicated] '{domain}' cần xử lý song song → mở thêm tab phụ "
            f"({len(self._domains[domain])}/{self._max_for(domain)})"
        )
        return entry

    async def _respawn_page(self, domain: str):
        context = await get_or_launch_browser_context("shared")
        page = await context.new_page()
        await _setup_page_performance(page, f"dedicated-{domain}")
        return page

    async def acquire(self, domain: str = ""):
        """Acquire a usable tab, waiting briefly through transient contention."""
        wait_limit = max(0.0, float(getattr(Config, "TAB_ACQUIRE_WAIT_SECONDS", 2.0)))
        deadline = time.monotonic() + wait_limit
        while True:
            try:
                return await self._try_acquire(domain)
            except RuntimeError:
                if time.monotonic() >= deadline:
                    raise
                await asyncio.sleep(0.05)

    async def _try_acquire(self, domain: str = ""):
        domain = domain or "unknown"

        if _cf_domain_paused(domain):
            raise RuntimeError(
                f"Domain '{domain}' đang tạm dừng riêng vì Cloudflare/captcha; "
                "các domain khác vẫn tiếp tục"
            )
        entries = self._domains.get(domain)
        if not entries:
            async with self._setup_lock:
                entries = self._domains.get(domain)
                if not entries:
                    entry = await self._claim_existing_or_new(domain)
                    if entry is None:
                        context = await get_or_launch_browser_context("shared")
                        wait_timeout = 10.0
                        poll = 0.5
                        waited = 0.0
                        while waited < wait_timeout:
                            for p in context.pages:
                                try:
                                    if domain in (p.url or "").lower():
                                        entry = self._new_entry(p)
                                        self._domains.setdefault(domain, []).append(entry)
                                        entries = self._domains[domain]
                                        break
                                except Exception:
                                    pass
                            if entries:
                                break
                            await asyncio.sleep(poll)
                            waited += poll
                        if not entries:
                            raise RuntimeError(f"No tab available for domain '{domain}'")

        for entry in entries:
            page = entry["page"]
            if entry["lock"].locked() or safe_is_closed(page):
                continue
            try:
                cf_blocked = await is_cloudflare_present(page, domain=domain)
            except Exception:
                cf_blocked = False
            if cf_blocked:
                # Đừng để tab đang chờ người dùng xác minh nằm phía sau các
                # tab khác. Việc đưa tab lên trước chỉ ảnh hưởng UI của Edge,
                # không chiếm lock và không làm gián đoạn luồng nhận tin.
                await _wake_tab_for_submit(page)
                _mark_cf_domain(domain, "tab đang chờ xác minh")
                logger.warning(
                    "⚠️ [TabPool|%s] Tab đang chờ Cloudflare/captcha — "
                    "đã đưa lên trước để xác minh thủ công (url=%s)",
                    domain,
                    getattr(page, "url", ""),
                )
                continue
            self._touch(entry)
            return entry, page, entry["lock"]

        domain_cap = self._max_for(domain)
        entries = self._domains.get(domain, [])
        if len(entries) < domain_cap:
            try:
                entry = await self._new_tab_for_domain(domain)
                self._touch(entry)
                return entry, entry["page"], entry["lock"]
            except Exception:
                pass

        for candidate in entries:
            if candidate["lock"].locked() or safe_is_closed(candidate["page"]):
                continue
            try:
                if await is_cloudflare_present(candidate["page"], domain=domain):
                    continue
            except Exception:
                pass
            self._touch(candidate)
            return candidate, candidate["page"], candidate["lock"]

        raise RuntimeError(
            f"No available non-blocked tab for domain '{domain}' "
            f"({len(entries)} entries are busy, closed, or under verification)"
        )

    async def collect_garbage(self, *, idle_ttl: float = 900.0, min_tabs_per_domain: int = 1) -> dict:
        """Remove closed/stale spare tabs without interrupting submissions."""
        idle_ttl = max(30.0, float(idle_ttl))
        keep_min = max(1, int(min_tabs_per_domain))
        now = time.monotonic()
        removed = 0
        closed = 0

        async with self._setup_lock:
            for domain, entries in list(self._domains.items()):
                if not entries:
                    self._domains.pop(domain, None)
                    self._rr_idx.pop(domain, None)
                    continue

                survivors = []
                ordered = sorted(entries, key=lambda e: e.get("created_at", now))
                for index, entry in enumerate(ordered):
                    page = entry.get("page")
                    lock = entry.get("lock")
                    is_closed = safe_is_closed(page)
                    is_idle_spare = (
                        index >= keep_min
                        and not lock.locked()
                        and now - float(entry.get("last_used", now)) >= idle_ttl
                    )
                    if lock.locked() or (not is_closed and not is_idle_spare):
                        survivors.append(entry)
                        continue

                    if not is_closed:
                        try:
                            await page.close()
                            closed += 1
                        except Exception:
                            survivors.append(entry)
                            continue
                    removed += 1

                    for key, cached_page in list(bot_state.account_pages.items()):
                        if cached_page is page:
                            bot_state.account_pages.pop(key, None)
                            bot_state._input_cache.pop(key, None)
                            bot_state.context_locks.pop(key, None)

                if survivors:
                    self._domains[domain] = survivors
                    self._rr_idx[domain] = self._rr_idx.get(domain, 0) % len(survivors)
                else:
                    self._domains.pop(domain, None)
                    self._rr_idx.pop(domain, None)

        if removed:
            gc.collect()
            logger.info(
                "🧹 [TabPool-GC] removed=%s closed=%s remaining=%s",
                removed,
                closed,
                sum(len(items) for items in self._domains.values()),
            )
            return {
                "removed": removed,
                "closed": closed,
                "remaining": sum(len(items) for items in self._domains.values()),
            }

    async def cleanup_idle_memory(
        self, *, compact_idle_seconds: float = 300.0, reload_idle_seconds: float = 900.0
    ) -> dict:
        """Release accumulated page memory without touching active submits.

        Only unlocked, non-Cloudflare tabs are considered. A lightweight DOM
        compact runs first; a long-idle tab is reloaded to release the page's
        JS/resource graph while retaining the warm tab slot. At most one tab
        per domain is reloaded per watchdog pass to avoid a synchronized cold
        start across all sites.
        """
        now = time.monotonic()
        compacted = 0
        reloaded = 0
        skipped = 0
        for domain, entries in list(self._domains.items()):
            reloaded_this_domain = False
            for entry in entries:
                page = entry.get("page")
                lock = entry.get("lock")
                if lock is None or lock.locked() or safe_is_closed(page):
                    skipped += 1
                    continue
                idle = now - float(entry.get("last_used", now))
                if idle < max(30.0, float(compact_idle_seconds)):
                    continue
                try:
                    if await is_cloudflare_present(page, domain=domain):
                        skipped += 1
                        continue
                except Exception:
                    skipped += 1
                    continue

                if (
                    not reloaded_this_domain
                    and idle >= max(float(compact_idle_seconds), float(reload_idle_seconds))
                ):
                    try:
                        await page.reload(
                            wait_until="domcontentloaded",
                            timeout=int(getattr(Config, "PAGE_NAVIGATION_TIMEOUT", 10000)),
                        )
                        await _close_unwanted_popups(page)
                        _touch = self._touch
                        _touch(entry)
                        reloaded += 1
                        reloaded_this_domain = True
                        continue
                    except Exception as exc:
                        logger.debug("⚠️ [TabPool-Memory] reload %s lỗi: %s", domain, exc)

                try:
                    await page.evaluate(
                        """
                        () => {
                            const transient = [
                                '.toast', '[class*="toast" i]', '[class*="snackbar" i]',
                                '[role="alert"]', '[role="status"]'
                            ];
                            for (const selector of transient) {
                                for (const el of document.querySelectorAll(selector)) {
                                    if (el && !el.matches('input,textarea,form')) el.remove();
                                }
                            }
                            try { performance.clearResourceTimings(); } catch (_) {}
                            return true;
                        }
                        """
                    )
                    self._touch(entry)
                    compacted += 1
                except Exception as exc:
                    logger.debug("⚠️ [TabPool-Memory] compact %s lỗi: %s", domain, exc)
        return {"compacted": compacted, "reloaded": reloaded, "skipped": skipped}


_tab_pool: TabPool | None = None


async def _check_edge_cdp_port_reachable(port: int, timeout: float = 1.5) -> bool:
    """Check CDP reachability without blocking the browser event loop.

    ``socket.create_connection`` is synchronous and may wait up to the full
    timeout when Edge/CDP is unavailable.  The watchdog calls this periodically
    while submit tasks are active, so run the small blocking probe in a worker
    thread instead of pausing every Playwright coroutine.
    """
    import socket
    host = getattr(Config, "EDGE_CDP_HOST", "127.0.0.1")

    def _probe() -> bool:
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            return False

    return await asyncio.to_thread(_probe)


async def preload_browsers_and_accounts(account_targets: list):
    """account_targets: danh sách item {"key","domain","target_url","accounts"}
    ĐÃ ĐƯỢC LỌC SẴN chỉ gồm domain thuộc BROWSER_DOMAINS — main_script.py
    tính toán và truyền vào (dùng chung build_unique_account_targets())."""
    global _tab_pool

    if not account_targets:
        logger.info("ℹ️ [Browser] Không có kênh nào thuộc 7 domain trình duyệt — bỏ qua preload")
        return

    pool_size = max(1, min(10, int(getattr(Config, "TAB_POOL_SIZE", 2) or 2)))

    domain_channel_count: dict = {}
    for item in account_targets:
        d = item["domain"]
        domain_channel_count[d] = domain_channel_count.get(d, 0) + 1

    domain_tab_cap = max(1, int(getattr(Config, "MAX_TAB_PER_DOMAIN_CAP", 5)))
    per_domain_overrides = {}
    for d, count in domain_channel_count.items():
        # Apply the cap to every configured domain. Without this override,
        # TAB_POOL_SIZE could accidentally allow many tabs for domains that
        # have fewer configured account targets than the global pool size.
        profile_slots = int(_site_profile_value(d, "tab_slots", domain_tab_cap))
        per_domain_overrides[d] = min(
            domain_tab_cap,
            max(1, profile_slots),
            max(1, count),
        )

    _tab_pool = TabPool(max_per_domain=pool_size, per_domain_overrides=per_domain_overrides)

    domain_url_map: dict = {}
    for item in account_targets:
        d = item["domain"]
        if d not in domain_url_map:
            domain_url_map[d] = item["target_url"]

    for item in account_targets:
        key = item.get("key", item["domain"])
        bot_state.context_locks[key] = asyncio.Lock()
        bot_state.cf_verified[key] = True
        bot_state.submission_count[key] = 0

    site_count = len({item.get("domain") for item in account_targets if item.get("domain")})
    logger.info(
        "✅ [Browser] %s target domain+tài khoản (%s site trình duyệt) đăng ký xong",
        len(account_targets),
        site_count,
    )

    cdp_port = getattr(Config, "EDGE_CDP_PORT", 9222)
    cdp_ready = await _check_edge_cdp_port_reachable(cdp_port)
    if not cdp_ready:
        max_retries = 7
        for attempt in range(1, max_retries + 1):
            logger.info(f"⏳ [Edge-CDP] Cổng {cdp_port} chưa phản hồi — thử lại ({attempt}/{max_retries}, mỗi 2s)...")
            await asyncio.sleep(2.0)
            if await _check_edge_cdp_port_reachable(cdp_port):
                cdp_ready = True
                break

    if cdp_ready:
        logger.info(f"✅ [Edge-CDP] Cổng {cdp_port} đang mở — Edge sẵn sàng nhận kết nối")
        try:
            await _tab_pool.init(domain_url_map=domain_url_map)
            logger.info(f"✅ [TabPool] Đã gán tab riêng cho {len(domain_url_map)} domain")
        except Exception as e:
            logger.warning(f"⚠️ [TabPool] Không init được ngay lúc preload ({e}) — sẽ tự thử lại kiểu lazy")
    else:
        # Không để một pool chưa khởi tạo tiếp tục chạy lazy rồi thử kết nối
        # lại trong từng submit, gây chậm khoảng thời gian timeout CDP.
        _tab_pool = None
        logger.error(
            f"❌ [Edge-CDP] Cổng {cdp_port} KHÔNG phản hồi — Edge CHƯA chạy ở chế độ debug! "
            f"Các submit sẽ fail-fast cho tới khi Edge được khởi động đúng chế độ debug."
        )


# ============================================================
# PAGE CLEAN-UP AFTER SUBMIT
# ============================================================
async def _reload_page_and_refill(page, domain: str, target_url: str, key: str):
    try:
        edge_restore()
        await page.goto(
            target_url,
            wait_until="domcontentloaded",
            timeout=int(float(_site_profile_value(
                domain,
                "navigation_timeout_seconds",
                float(getattr(Config, "PAGE_NAVIGATION_TIMEOUT", 10000)) / 1000.0,
            )) * 1000),
        )
        if domain == "livemm88.net":
            await open_mm88_code_form(page)
        await scroll_to_input_fields(page)
        await _close_unwanted_popups(page)
        settle = float(_site_profile_value(
            domain,
            "form_settle_seconds",
            getattr(Config, "MM88_FORM_SETTLE_SECONDS", 0.05)
            if domain == "livemm88.net"
            else getattr(Config, "FORM_SETTLE_SECONDS", 0.03),
        ))
        await asyncio.sleep(max(0.02, settle))
        _invalidate_input_cache(key)
        return True
    except Exception as e:
        logger.warning(f"⚠️ [{domain}] Lỗi reload trang sau submit: {e}")
        return False


async def _quick_clean_page(page, key: str) -> bool:
    try:
        await _close_unwanted_popups(page)
        await page.evaluate(
            """
            () => {
                const inputs = document.querySelectorAll('input:not([type="hidden"])');
                for (const inp of inputs) {
                    try {
                        const placeholder = (inp.placeholder || '').toLowerCase();
                        const isUsername = placeholder.includes('tài khoản')
                            || placeholder.includes('tên người dùng')
                            || placeholder.includes('tai khoan')
                            || placeholder.includes('ten nguoi dung')
                            || inp.id === 'account-code'
                            || inp.name === 'username';
                        if (isUsername && (inp.value || '').trim()) continue;
                        const proto = inp.tagName === 'TEXTAREA'
                            ? window.HTMLTextAreaElement.prototype
                            : window.HTMLInputElement.prototype;
                        const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
                        setter.call(inp, '');
                        inp.dispatchEvent(new Event('input', {bubbles: true}));
                        inp.dispatchEvent(new Event('change', {bubbles: true}));
                    } catch (e) {}
                }
            }
            """
        )
        return True
    except Exception as e:
        logger.debug(f"⚠️ [{key}] Quick-clean lỗi: {e}")
        return False


async def _clean_page_after_submit(page, domain: str, target_url: str, key: str, force_full: bool = False):
    count = bot_state._submits_since_full_reload.get(key, 0) + 1
    threshold = int(_site_profile_value(
        domain,
        "full_reload_every",
        getattr(Config, "FULL_RELOAD_EVERY_N", 100),
    ))

    if not force_full and count < threshold:
        ok = await _quick_clean_page(page, key)
        if ok:
            bot_state._submits_since_full_reload[key] = count
            return True

    bot_state._submits_since_full_reload[key] = 0
    return await _reload_page_and_refill(page, domain, target_url, key)


# ============================================================
# SUBMIT (bản trình duyệt) — điểm vào chính của module này
# ============================================================
async def submit_code_browser(user: str, code: str, target_url: str, systems: dict) -> dict:
    """Serialize form mutations for one ``(domain, account)`` pair.

    A page keeps the username and code in shared DOM state. Without this
    outer lock, two fanout tasks for the same account can overwrite each
    other's inputs between fill and click, producing validation errors.
    """
    domain = _normalize_domain(target_url)
    key = f"{domain}|{user}"
    lock = bot_state.context_locks.setdefault(key, asyncio.Lock())
    async with lock:
        return await _submit_code_browser_locked(user, code, target_url, systems)


async def _submit_code_browser_locked(user: str, code: str, target_url: str, systems: dict) -> dict:
    """Trả về kết quả submit chuẩn hóa gồm {"success", "message",
    "has_points", "is_wrong_code", ...}. Khi gặp lỗi HẠ TẦNG (mất tab, mất
    CDP, không tìm thấy input, Cloudflare/captcha chặn...) trả thêm khoá
    "_infra_failure": True — main_script.py.submit_code_safe() dựa vào cờ
    này để ghi nhận lỗi sang browser ngay, không đứng chờ."""
    start_time = time.perf_counter()
    browser_perf_start = time.perf_counter()
    domain = _normalize_domain(target_url)
    key = f"{domain}|{user}"
    probe = None
    cross_probe_started = False
    clicked = False
    verified_clicked = False
    tab_acquire_ms = 0.0
    prepare_ms = 0.0
    click_path_ms = 0.0
    click_to_result_ms = 0.0

    if _tab_pool is None:
        return {"success": False, "message": "Browser chưa sẵn sàng (no TabPool)", "_infra_failure": True}

    if key not in bot_state.context_locks:
        bot_state.context_locks[key] = asyncio.Lock()
        bot_state.cf_verified[key] = True
        bot_state.submission_count.setdefault(key, 0)

    tab_acquire_started = time.perf_counter()
    try:
        tab_entry, page, tab_lock = await _tab_pool.acquire(domain=domain)
        tab_acquire_ms = (time.perf_counter() - tab_acquire_started) * 1000.0
    except Exception as e:
        logger.warning(f"⚠️ [Browser|{domain}] Không lấy được tab: {e}")
        return {"success": False, "message": f"No tab: {e}", "_infra_failure": True}

    try:
        async with tab_lock:
            prepare_started = time.perf_counter()
            bot_state.account_pages[key] = page

            if page.is_closed():
                context = await get_or_launch_browser_context("shared")
                page = await context.new_page()
                await _setup_page_performance(page, domain)
                tab_entry["page"] = page
                bot_state.account_pages[key] = page
                bot_state._input_cache.pop(key, None)

            # TabPool tái sử dụng tab nên trước đây _wake_tab_for_submit()
            # chỉ chạy trong nhánh goto. Mỗi code phải đánh thức tab ở đây,
            # trước khi tìm/fill input, để người dùng luôn thấy đúng trang
            # đang chờ nhập mã hoặc click xác minh Cloudflare.
            await _wake_tab_for_submit(page)
            logger.info(
                "🖥️ [Browser|%s|%s] Tab đang hoạt động — nhập code=%s tại %s",
                domain,
                user,
                code,
                target_url,
            )

            try:
                page_url = page.url
            except Exception:
                page_url = ""

            if _should_navigate_to_target(page_url, target_url, domain):
                logger.info(f"🌐 [{domain}] Điều hướng tới {target_url}")
                edge_restore()
                try:
                    await page.goto(
                        target_url,
                        wait_until="domcontentloaded",
                        timeout=int(float(_site_profile_value(
                            domain,
                            "navigation_timeout_seconds",
                            float(getattr(Config, "PAGE_NAVIGATION_TIMEOUT", 10000)) / 1000.0,
                        )) * 1000),
                    )
                    if domain == "livemm88.net":
                        await open_mm88_code_form(page)
                    await scroll_to_input_fields(page)
                    settle = float(_site_profile_value(
                        domain,
                        "form_settle_seconds",
                        getattr(Config, "MM88_FORM_SETTLE_SECONDS", 0.05)
                        if domain == "livemm88.net"
                        else getattr(Config, "FORM_SETTLE_SECONDS", 0.03),
                    ))
                    await asyncio.sleep(max(0.02, settle))
                    _invalidate_input_cache(key)
                except Exception as e:
                    return {"success": False, "message": f"Goto failed: {e}", "_infra_failure": True}

                await _wake_tab_for_submit(page)
                edge_restore()

                if domain != "gg88live.tv" and await is_cloudflare_present(page, domain=domain):
                    _mark_cf_domain(domain, "challenge sau điều hướng")
                    logger.warning(f"⚠️ [{domain}] Cloudflare/captcha chặn tab — lỗi hạ tầng browser")
                    return {"success": False, "message": "Cloudflare challenge", "_infra_failure": True}

            # The page/tab can be reused between queue retries. Its Turnstile
            # token may expire after navigation, so verify it again directly
            # before filling/clicking any code.
            if domain == "gg88live.tv" and not await _wait_for_turnstile_token(
                page,
                float(getattr(Config, "GG88_TURNSTILE_WAIT_SECONDS", 15.0)),
            ):
                logger.warning(
                    "⏸️ [%s] Turnstile token chưa sẵn sàng; bỏ qua submit lần này",
                    domain,
                )
                return {
                    "success": False,
                    "message": "GG88 Turnstile verification pending",
                    "_infra_failure": True,
                    "_clicked": False,
                    "_verification_pending": True,
                }

            cached_entry = bot_state._input_cache.get(key)
            cache_was_fresh = bool(cached_entry and (time.time() - cached_entry[2]) < bot_state._input_cache_ttl)
            username_input, code_input = await find_input_fields(page, cache_key=key, domain=domain)

            if not code_input:
                await asyncio.sleep(0.05)
                _invalidate_input_cache(key)
                cache_was_fresh = False
                username_input, code_input = await find_input_fields(page, cache_key=key, domain=domain)

            if not code_input:
                if domain == "livemm88.net" and await open_mm88_code_form(page):
                    _invalidate_input_cache(key)
                    username_input, code_input = await find_input_fields(page, cache_key=key, domain=domain)

            if not code_input:
                return {"success": False, "message": "Không tìm thấy ô nhập code (site có thể đã đổi UI)", "_infra_failure": True}

            if not cache_was_fresh:
                await scroll_to_input_fields(page)

            preserve_prefilled = False
            try:
                if username_input and getattr(Config, "PRESERVE_PREFILLED_USERNAME", True):
                    try:
                        current_username = (await username_input.input_value()).strip()
                        preserve_prefilled = bool(current_username) and current_username == str(user).strip()
                        if preserve_prefilled:
                            logger.debug(f"✅ [{domain}|{user}] giữ tài khoản đã điền sẵn, chỉ nhập code")
                    except Exception:
                        preserve_prefilled = False
                # Gộp fill + đọc xác minh vào một round-trip CDP thay vì
                # fill username, fill code, đọc username, đọc code riêng lẻ.
                verify = await page.evaluate(
                    REACT_FILL_VERIFY_JS,
                    [username_input, code_input, user, code, not preserve_prefilled],
                )
                actual_user = (verify.get("actualUser") or "").strip()
                actual_code = (verify.get("actualCode") or "").strip()
                if username_input and not preserve_prefilled and actual_user != str(user).strip():
                    raise RuntimeError(
                        f"Account input mismatch: expected={user!r} actual={actual_user!r}"
                    )
                if actual_code.upper() != str(code).strip().upper():
                    raise RuntimeError(
                        f"Code input mismatch: expected={code!r} actual={actual_code!r}"
                    )
            except Exception as e:
                _invalidate_input_cache(key)
                username_input, code_input = await find_input_fields(page, cache_key=key, domain=domain)
                if code_input:
                    try:
                        verify = await page.evaluate(
                            REACT_FILL_VERIFY_JS,
                            [username_input, code_input, user, code, not preserve_prefilled],
                        )
                        actual_user = (verify.get("actualUser") or "").strip()
                        actual_code = (verify.get("actualCode") or "").strip()
                        if username_input and not preserve_prefilled and actual_user != str(user).strip():
                            return {
                                "success": False,
                                "message": f"Account input mismatch after retry: {actual_user!r}",
                                "_infra_failure": True,
                            }
                        if actual_code.upper() != str(code).strip().upper():
                            return {
                                "success": False,
                                "message": f"Code input mismatch after retry: {actual_code!r}",
                                "_infra_failure": True,
                            }
                    except Exception as e2:
                        return {"success": False, "message": f"Fill error: {e2}", "_infra_failure": True}
                else:
                    return {"success": False, "message": f"Fill error: {e}", "_infra_failure": True}

            # QQ88/HI88 expose the Cloudflare verification button only after
            # the user clicks "Kiểm tra ngay". Their required order is:
            # fill code -> click check -> wait for Xác thực -> click Xác thực.
            if domain not in {"tangquaqq88.com", "hi88-freecode.pages.dev"}:
                cf_wait_deadline = time.time() + float(getattr(Config, "CF_WAIT_SECONDS", 1.0))
                while time.time() < cf_wait_deadline:
                    try:
                        cf_state = await page.evaluate(
                            """
                            () => {
                                const hasWidget = !!document.querySelector(
                                    '.cf-turnstile, [data-sitekey], iframe[src*="turnstile"], '
                                    + 'iframe[src*="challenges.cloudflare.com"]'
                                );
                                if (!hasWidget) return {hasWidget: false, passed: false};
                                const text = (document.body.innerText || '').toLowerCase();
                                const passed = ['thành công', 'thanh cong', 'verified', 'success']
                                    .some((marker) => text.includes(marker));
                                return {hasWidget: true, passed};
                            }
                            """
                        )
                    except Exception:
                        cf_state = {"hasWidget": False, "passed": False}
                    if not cf_state.get("hasWidget") or cf_state.get("passed"):
                        break
                    await asyncio.sleep(max(0.05, float(getattr(Config, "CF_POLL_INTERVAL", 0.10))))

            try:
                # HI88/QQ88 chỉ cần snapshot form/modal để loại stale result.
                # Không gửi toàn bộ main/#app (thường chứa banner/link dài) qua
                # CDP trước mỗi submit; các site khác vẫn giữ snapshot rộng để
                # tương thích với layout cũ.
                pre_click_scopes = (
                    'form, [role="dialog"], .modal, [role="alert"], [role="status"]'
                    if domain in {"tangquaqq88.com", "hi88-freecode.pages.dev"}
                    else 'form, [role="dialog"], .modal, main, #app, #root'
                )
                pre_click_text = await page.evaluate(
                    """
                    (scopeSelector) => {
                        const scopes = document.querySelectorAll(scopeSelector);
                        let text = '';
                        for (const scope of scopes) {
                            text += (scope.innerText || '') + '\n';
                            if (text.length >= 30000) break;
                        }
                        return text.slice(0, 30000);
                    }
                    """,
                    pre_click_scopes,
                )
            except Exception:
                pre_click_text = ""

            # Observe only request resource timing and HTTP status. Never
            # retain request URLs, headers, form values, or response bodies.
            probe = _start_browser_request_probe(page, domain=domain)
            cross_probe_started = await _start_cross_domain_request_probe(page)
            main_click_started = time.perf_counter()
            clicked = await click_submit_fast(page, domain=domain)
            main_click_completed = time.perf_counter()
            verification_started = main_click_completed
            verified_clicked = await click_verification_button_if_present(page, domain=domain)
            verification_completed = time.perf_counter()
            click_path_ms = (verification_completed - main_click_started) * 1000.0
            verification_wait_ms = (verification_completed - verification_started) * 1000.0
            actual_submit_started = verification_completed if verified_clicked else main_click_completed
            if verified_clicked:
                logger.info(f"✅ [Browser|{domain}] đã bấm nút Xác thực")
            elif domain in {"tangquaqq88.com", "hi88-freecode.pages.dev"}:
                # Turnstile may still be verifying after the check button was
                # clicked. Stop this attempt before result recording: that
                # callback can clean/reload the page and restart the widget.
                # Keep the current page alive so a human can finish the
                # verification in the same Edge profile.
                try:
                    challenge_pending = await is_cloudflare_present(page, domain=domain)
                except Exception:
                    challenge_pending = False
                if verified_clicked is None or challenge_pending:
                    _mark_cf_domain(domain, "nút xác thực đang chờ")
                    logger.warning(
                        f"⚠️ [{domain}] Final verification click still pending "
                        f"(challenge={challenge_pending}) — giữ nguyên trang, không reload"
                    )
                    network_metrics = _stop_browser_request_probe(page, probe)
                    if cross_probe_started:
                        network_metrics.update(await _stop_cross_domain_request_probe(page))
                        cross_probe_started = False
                    probe = None
                    return {
                        "success": False,
                        "message": "Verification button disabled or click pending",
                        "_infra_failure": True,
                        "keep_page": True,
                        "_clicked": bool(clicked or verified_clicked),
                        "_browser_timing": {
                            "stage_ms": {
                                "tab_acquire": round(tab_acquire_ms, 2),
                                "prepare_form": round((time.perf_counter() - prepare_started) * 1000.0 - click_path_ms, 2),
                                "main_click_path": round((main_click_completed - main_click_started) * 1000.0, 2),
                                "verification_wait": round(verification_wait_ms, 2),
                                "click_to_result": 0.0,
                                "browser_total": round((time.perf_counter() - browser_perf_start) * 1000.0, 2),
                            },
                            "network": network_metrics,
                        },
                    }
            click_elapsed = time.perf_counter() - start_time
            logger.info(f"🚀 [Browser|{user}] SUBMIT {code} ({click_elapsed:.2f}s)")

            result_text = ""
            timeout_by_domain = getattr(Config, "RESULT_DETECTION_TIMEOUT_BY_DOMAIN", {})
            default_timeout_ms = getattr(Config, "RESULT_DETECTION_TIMEOUT", 5000)
            profile_timeout_ms = _site_profile_value(
                domain,
                "result_timeout_ms",
                default_timeout_ms,
            )
            result_timeout_ms = timeout_by_domain.get(domain, default_timeout_ms)
            if domain not in timeout_by_domain:
                result_timeout_ms = profile_timeout_ms
            result_timeout_s = float(result_timeout_ms) / 1000.0
            result_timeout_ms = max(1, int(result_timeout_ms))
            try:
                # Wait inside the page on DOM mutations instead of issuing a
                # new CDP evaluate every 50–250 ms. Return the changed text so
                # the Python side does not need an extra round-trip on success.
                handle = await page.wait_for_function(
                    _RESULT_DOM_CHANGED_JS,
                    arg={"beforeText": pre_click_text,
                         "selectors": _get_domain_result_selectors(domain)},
                    timeout=result_timeout_ms,
                    polling="mutation",
                )
                try:
                    candidate = str(await handle.json_value() or "").strip()
                    if (candidate and len(candidate) >= 3
                            and not _is_transient_captcha_text(candidate)
                            and not _is_stale_static_text(candidate, pre_click_text)):
                        result_text = _filter_nextjs_noise(candidate) or candidate
                finally:
                    try:
                        await handle.dispose()
                    except Exception:
                        pass
            except Exception:
                # Compatibility fallback for DOMs that do not trigger the
                # mutation observer as expected. Keep the configured deadline
                # as the safety ceiling; do not restart the full timeout.
                pass

            if not result_text:
                try:
                    candidate = await detect_result_text(
                        page, domain=domain, before_text=pre_click_text,
                        selector_only=False,
                    )
                    if candidate and len(candidate.strip()) >= 3:
                        result_text = candidate
                except Exception:
                    pass

            click_to_result_ms = (time.perf_counter() - actual_submit_started) * 1000.0
            prepare_ms = max(0.0, (time.perf_counter() - prepare_started) * 1000.0 - click_path_ms - click_to_result_ms)
            elapsed = time.perf_counter() - start_time
            network_metrics = _stop_browser_request_probe(page, probe)
            if cross_probe_started:
                network_metrics.update(await _stop_cross_domain_request_probe(page))
                cross_probe_started = False
            probe = None
            browser_timing = {
                "stage_ms": {
                    "tab_acquire": round(tab_acquire_ms, 2),
                    "prepare_form": round(prepare_ms, 2),
                    "main_click_path": round((main_click_completed - main_click_started) * 1000.0, 2),
                    "verification_wait": round(verification_wait_ms, 2),
                    "click_to_result": round(click_to_result_ms, 2),
                    "browser_total": round((time.perf_counter() - browser_perf_start) * 1000.0, 2),
                },
                "network": network_metrics,
            }

            if _needs_manual_verify(result_text):
                _mark_cf_domain(domain, "site yêu cầu captcha ảnh")
                logger.warning(
                    f"⚠️ [{domain}] Site yêu cầu xác thực riêng (captcha ảnh) — "
                    f"result_text={result_text[:200]!r}"
                )
                try:
                    await _close_unwanted_popups(page)
                except Exception:
                    pass
                return {
                    "success": False,
                    "message": "Cần xác thực thủ công",
                    "_infra_failure": True,
                    "_clicked": bool(clicked or verified_clicked),
                    "_browser_timing": browser_timing,
                }

            status = classify_result(result_text)
            callbacks = {
                "clean_page": _clean_page_after_submit,
                "append_history": _append_code_history_safe,
            }
            debug_info = None
            if status.value == "NO_RESULT":
                debug_info = {
                    "code": code, "user": user, "domain": domain,
                    "clicked_submit_button": clicked,
                    "pre_click_text_len": len(pre_click_text or ""),
                    "result_timeout_s": result_timeout_s,
                    "page_url": page.url if page else None,
                }

            outcome = await record_outcome(
                status=status, page=page, user=user, code=code,
                target_url=target_url, domain=domain, key=key, elapsed=elapsed,
                result_text=result_text, systems=systems, callbacks=callbacks,
                debug_info=debug_info,
                capture_screenshot_fn=capture_result_screenshot,
            )
            outcome.result["_clicked"] = bool(clicked or verified_clicked)
            outcome.result["_browser_timing"] = browser_timing
            if outcome.postprocess:
                outcome.result["_outcome_postprocess"] = outcome.postprocess
            if outcome.result.get("success"):
                _clear_cf_domain(domain)
            return outcome.result

    except asyncio.CancelledError:
        if probe is not None:
            _stop_browser_request_probe(page, probe)
        if cross_probe_started:
            try:
                await _stop_cross_domain_request_probe(page)
            except Exception:
                pass
        raise
    except Exception as e:
        if probe is not None:
            network_metrics = _stop_browser_request_probe(page, probe)
            probe = None
        else:
            network_metrics = {}
        if cross_probe_started:
            network_metrics.update(await _stop_cross_domain_request_probe(page))
            cross_probe_started = False
        elapsed = time.perf_counter() - start_time
        err_str = str(e)
        if "Target page, context or browser has been closed" in err_str or "TargetClosedError" in type(e).__name__:
            try:
                context = await get_or_launch_browser_context("shared", force_reconnect=True)
                new_page = await context.new_page()
                bot_state.account_pages[key] = new_page
                await _setup_page_performance(new_page, domain)
                await new_page.goto(target_url, wait_until="domcontentloaded", timeout=10000)
                _invalidate_input_cache(key)
            except Exception:
                pass
        try:
            systems["performance_monitor"].record_task("submit_code", elapsed, False)
        except Exception:
            pass
        logger.error(f"❌ [Browser|{domain}] {e}")
        return {
            "success": False,
            "message": str(e),
            "_infra_failure": True,
            "_clicked": bool(clicked or verified_clicked),
            "_browser_timing": {
                "stage_ms": {
                    "tab_acquire": round(tab_acquire_ms, 2),
                    "prepare_form": round(prepare_ms, 2),
                    "main_click_path": round(click_path_ms, 2),
                    "verification_wait": round(verification_wait_ms, 2) if "verification_wait_ms" in locals() else 0.0,
                    "click_to_result": round(click_to_result_ms, 2),
                    "browser_total": round((time.perf_counter() - browser_perf_start) * 1000.0, 2),
                },
                "network": network_metrics,
            },
        }


# ============================================================
# WATCHDOGS — chỉ giữ phần KHÔNG liên quan captcha (giữ Edge/tab sống)
# ============================================================
_edge_cdp_was_down = False


async def browser_watchdog():
    """Single ordered watchdog: CDP reachability/reconnect, then stale tabs."""
    interval = max(5.0, float(getattr(Config, "CDP_PING_INTERVAL", 60.0)))
    port = getattr(Config, "EDGE_CDP_PORT", 9222)
    global _edge_cdp_was_down
    while bot_state.is_running:
        try:
            await asyncio.sleep(interval)
            reachable = await _check_edge_cdp_port_reachable(port, timeout=2.0)
            if not reachable:
                if not _edge_cdp_was_down:
                    logger.critical(f"❌ [Browser-Watchdog] Mất kết nối CDP {port}; đang reconnect")
                    _edge_cdp_was_down = True
                try:
                    await get_or_launch_browser_context("shared", force_reconnect=True)
                    _edge_cdp_was_down = False
                    logger.info(f"✅ [Browser-Watchdog] CDP {port} đã kết nối lại")
                except Exception as exc:
                    logger.warning(f"⚠️ [Browser-Watchdog] Reconnect thất bại: {exc}")
                continue
            if _edge_cdp_was_down:
                logger.info(f"✅ [Browser-Watchdog] CDP {port} phản hồi trở lại")
                _edge_cdp_was_down = False
            stale_keys = [key for key, page in list(bot_state.account_pages.items()) if safe_is_closed(page)]
            for key in stale_keys:
                domain = key.split("|", 1)[0]
                target_url = getattr(Config, "DOMAIN_TO_CHANNEL_URL", {}).get(domain)
                if not target_url:
                    target_url = next(
                        (cfg["url"] for cfg in Config.CHANNEL_CONFIG.values()
                         if _normalize_domain(cfg["url"]) == domain),
                        None,
                    )
                old_page = bot_state.account_pages.get(key)
                ctx = old_page.context if old_page else None
                if not target_url or ctx is None:
                    bot_state.account_pages.pop(key, None)
                    bot_state.context_locks.pop(key, None)
                    bot_state._input_cache.pop(key, None)
                    continue
                try:
                    new_page = await ctx.new_page()
                    await _setup_page_performance(new_page, domain)
                    await new_page.goto(target_url, wait_until="domcontentloaded", timeout=12000)
                    bot_state.account_pages[key] = new_page
                    bot_state._input_cache.pop(key, None)
                    logger.info(f"✅ [Browser-Watchdog] Đã mở lại tab {key}")
                except Exception as exc:
                    logger.warning(f"⚠️ [Browser-Watchdog] Mở lại tab {key} thất bại: {exc}")
            if _tab_pool is not None:
                try:
                    await _tab_pool.collect_garbage(
                        idle_ttl=getattr(Config, "TAB_POOL_IDLE_TTL", 900.0),
                        min_tabs_per_domain=getattr(Config, "TAB_POOL_MIN_TABS_PER_DOMAIN", 2),
                    )
                    memory_stats = await _tab_pool.cleanup_idle_memory(
                        compact_idle_seconds=getattr(Config, "TAB_POOL_MEMORY_COMPACT_IDLE_SECONDS", 300.0),
                        reload_idle_seconds=getattr(Config, "TAB_POOL_MEMORY_RELOAD_IDLE_SECONDS", 1800.0),
                    )
                    if memory_stats.get("compacted") or memory_stats.get("reloaded"):
                        logger.info("🧹 [TabPool-Memory] %s", memory_stats)
                except Exception as exc:
                    logger.debug(f"⚠️ [TabPool-GC] cleanup lỗi: {exc}")
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.debug(f"⚠️ browser_watchdog error: {exc}")


async def cleanup_browsers():
    global _shared_context, _edge_browser, _pw_instance

    _shared_context = None

    if _edge_browser is not None:
        try:
            await _edge_browser.close()
        except Exception:
            pass
        _edge_browser = None

    if _pw_instance is not None:
        try:
            await _pw_instance.stop()
        except Exception:
            pass
        _pw_instance = None
