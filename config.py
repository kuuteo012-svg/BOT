"""
⚙️ CẤU HÌNH BOT — BROWSER-ONLY
Browser là route duy nhất; không gửi code qua HTTP API.
"""

import logging
import os
from dotenv import load_dotenv

load_dotenv()


def get_int_env(key: str, default: int) -> int:
    value = os.getenv(key)
    if value is None or str(value).strip() == "":
        return default
    try:
        return int(value)
    except Exception:
        return default


def get_float_env(key: str, default: float) -> float:
    value = os.getenv(key)
    if value is None or str(value).strip() == "":
        return default
    try:
        return float(value)
    except Exception:
        return default


def get_bool_env(key: str, default: bool) -> bool:
    value = os.getenv(key)
    if value is None or str(value).strip() == "":
        return default
    return str(value).strip().lower() in ("true", "1", "yes", "y", "on")


def get_int_list_env(key: str, default: tuple[int, ...]) -> tuple[int, ...]:
    """Read comma-separated Telegram chat IDs, ignoring malformed values."""
    value = os.getenv(key)
    if value is None or not str(value).strip():
        return tuple(default)
    result = []
    for item in str(value).split(","):
        try:
            result.append(int(item.strip()))
        except (TypeError, ValueError):
            continue
    return tuple(dict.fromkeys(result)) or tuple(default)


class Config:
    # ==========================================
    # 🔐 TELEGRAM
    # ==========================================
    API_ID       = get_int_env("API_ID", 0)
    API_HASH     = os.getenv("API_HASH", "")
    SESSION_NAME = os.getenv("SESSION_NAME", "session_bot")

    # Bot API độc lập để cảnh báo khi session Telethon mất kết nối.
    # Không dùng chung với SESSION_NAME hoặc client Telethon chính.
    ALERT_BOT_TOKEN = os.getenv("ALERT_BOT_TOKEN", "").strip()
    ALERT_CHAT_ID = get_int_env(
        "ALERT_CHAT_ID",
        get_int_env("TELEGRAM_ADMIN_ID", 0),
    )
    ALERT_HTTP_TIMEOUT = get_float_env("ALERT_HTTP_TIMEOUT", 10.0)
    SUBMIT_SUCCESS_NOTIFY = get_bool_env("SUBMIT_SUCCESS_NOTIFY", True)
    SUBMIT_SUCCESS_CHAT_ID = get_int_env(
        "SUBMIT_SUCCESS_CHAT_ID",
        ALERT_CHAT_ID,
    )

    # Session recovery: backup first; deletion is opt-in and disabled by default.
    SESSION_AUTO_BACKUP = get_bool_env("SESSION_AUTO_BACKUP", True)
    SESSION_DELETE_AFTER_BACKUP = get_bool_env(
        "SESSION_DELETE_AFTER_BACKUP",
        False,
    )
    SESSION_BACKUP_DIR = os.getenv(
        "SESSION_BACKUP_DIR",
        "backups/sessions",
    ).strip()
    TELEGRAM_AUTO_RECONNECT = get_bool_env(
        "TELEGRAM_AUTO_RECONNECT",
        True,
    )
    TELEGRAM_CONNECTION_RETRIES = get_int_env(
        "TELEGRAM_CONNECTION_RETRIES",
        5,
    )
    TELEGRAM_MAX_RECONNECT_ATTEMPTS = get_int_env(
        "TELEGRAM_MAX_RECONNECT_ATTEMPTS",
        5,
    )
    # Kept as a compatibility key; Telethon does not expose a generic
    # per-request timeout through TelegramClient(timeout=...).
    TELEGRAM_REQUEST_TIMEOUT = get_float_env(
        "TELEGRAM_REQUEST_TIMEOUT",
        20.0,
    )
    TELEGRAM_CONNECT_TIMEOUT = get_float_env(
        "TELEGRAM_CONNECT_TIMEOUT",
        TELEGRAM_REQUEST_TIMEOUT,
    )
    TELEGRAM_HEARTBEAT_TIMEOUT = get_float_env(
        "TELEGRAM_HEARTBEAT_TIMEOUT",
        8.0,
    )
    TELEGRAM_CHANNEL_TIMEOUT = get_float_env(
        "TELEGRAM_CHANNEL_TIMEOUT",
        10.0,
    )
    # Không cần nạp toàn bộ dialog cá nhân/channel khi khởi động. Handler
    # đăng ký theo numeric chat_id và verify_channels chỉ resolve đúng các
    # chat đã cấu hình, giúp rút ngắn startup đáng kể với tài khoản nhiều dialog.
    TELEGRAM_SYNC_DIALOGS = get_bool_env("TELEGRAM_SYNC_DIALOGS", False)
    TELEGRAM_DIALOG_SYNC_TIMEOUT = get_float_env(
        "TELEGRAM_DIALOG_SYNC_TIMEOUT",
        120.0,
    )
    TELEGRAM_RECONNECT_BASE_DELAY = get_float_env(
        "TELEGRAM_RECONNECT_BASE_DELAY",
        2.0,
    )
    TELEGRAM_RECONNECT_MAX_DELAY = get_float_env(
        "TELEGRAM_RECONNECT_MAX_DELAY",
        60.0,
    )
    TELEGRAM_RECONNECT_JITTER = get_float_env(
        "TELEGRAM_RECONNECT_JITTER",
        1.0,
    )
    TELEGRAM_STABLE_CONNECTION_SECONDS = get_float_env(
        "TELEGRAM_STABLE_CONNECTION_SECONDS",
        30.0,
    )

    # ==========================================
    # 📝 LOG / DATABASE
    # ==========================================
    LOG_LEVEL    = os.getenv("LOG_LEVEL", "INFO")
    LOG_FILE     = os.getenv("LOG_FILE", "logs/bot_activity.log")
    MAX_LOG_SIZE = get_int_env("MAX_LOG_SIZE", 10485760)
    BACKUP_COUNT = get_int_env("BACKUP_COUNT", 5)

    DATABASE_PATH = os.getenv("DATABASE_PATH", "data/code_history.db")

    # ==========================================
    # 🔎 CODE FILTER
    # ==========================================
    CODE_MIN_LENGTH = 6
    CODE_MAX_LENGTH = 15

    SPECIAL_CODE_CHARS_30 = r"""~!@#$%^&*()_+{}|:\"<>?`=[]\\;',./»«"""

    CODE_FILTER_GROUPS = {
        "multi_site_strict": {
            "description": "Dùng chung cho XX88, RR88, GG88",
            "url_keywords": ["xx88", "rr88", "gg88"],
            "allowed_sites": ["xx88", "rr88", "gg88"],
            "allow_numeric": False,
            "allow_random_mix": False,
            "require_uppercase": True,
            "force_uppercase": True,
            "remove_spaces": True,
            "prefer_spoiler": True,
            "marker_scan_lines": 3,
            "allow_fallback": False,
            "special_chars_group": "SPECIAL_CODE_CHARS_30",
            "special_chars": SPECIAL_CODE_CHARS_30,
            "min_special_chars": 0,
            "min_entropy": 2.0,
            "uppercase_min_entropy": 2.5,
            "soft_blacklist": ["CODE", "GAME", "FREE", "VIP", "NAP", "RUT"],
            "max_clean_length": 9,
        },
        "mm88": {
            "description": "Nhóm riêng cho MM88",
            "url_keywords": ["mm88"],
            "allowed_sites": ["mm88"],
            "allow_numeric": False,
            "allow_random_mix": False,
            "require_uppercase": True,
            "force_uppercase": True,
            "remove_spaces": True,
            "prefer_spoiler": True,
            "marker_scan_lines": 3,
            "allow_fallback": False,
            "special_chars_group": "SPECIAL_CODE_CHARS_30",
            "special_chars": SPECIAL_CODE_CHARS_30,
            "min_special_chars": 0,
            "min_entropy": 2.0,
            "uppercase_min_entropy": 2.5,
            "soft_blacklist": ["CODE", "GAME", "FREE", "VIP", "NAP", "RUT"],
            "max_clean_length": 9,
        },
        "qq88": {
            "description": "Nhóm riêng cho QQ88 (tangquaqq88.com)",
            "url_keywords": ["qq88", "tangquaqq88"],
            "allowed_sites": ["qq88"],
            "allow_numeric": False,
            "allow_random_mix": True,
            "require_uppercase": False,
            "force_uppercase": False,
            "remove_spaces": False,
            "prefer_spoiler": True,
            "marker_scan_lines": 3,
            "allow_fallback": False,
            "special_chars_group": "SPECIAL_CODE_CHARS_30",
            "special_chars": "",
            "min_special_chars": 0,
            "min_entropy": 2.0,
            "uppercase_min_entropy": 2.5,
            "soft_blacklist": ["CODE", "GAME", "FREE", "VIP", "NAP", "RUT"],
        },
        "o8": {
            "description": "Nhóm riêng cho O8 (o8code.com)",
            "url_keywords": ["o8", "o8code"],
            "allowed_sites": ["o8"],
            "allow_numeric": False,
            "allow_random_mix": False,
            "require_uppercase": True,
            "force_uppercase": True,
            "remove_spaces": True,
            "prefer_spoiler": True,
            "marker_scan_lines": 3,
            "allow_fallback": False,
            "special_chars_group": "SPECIAL_CODE_CHARS_30",
            "special_chars": "",
            "min_special_chars": 0,
            "min_entropy": 2.0,
            "uppercase_min_entropy": 2.5,
            "soft_blacklist": ["CODE", "GAME", "FREE", "VIP", "NAP", "RUT"],
            "max_clean_length": 9,
        },
        "hi88": {
            "description": "Nhóm riêng cho HI88 (hi88-freecode.pages.dev)",
            "url_keywords": ["hi88", "hi88code"],
            "allowed_sites": ["hi88"],
            "allow_numeric": False,
            "allow_random_mix": True,
            "require_uppercase": False,
            "force_uppercase": False,
            "remove_spaces": False,
            "prefer_spoiler": True,
            "marker_scan_lines": 3,
            "allow_fallback": False,
            "special_chars_group": "SPECIAL_CODE_CHARS_30",
            "special_chars": "",
            "min_special_chars": 0,
            "min_entropy": 2.0,
            "uppercase_min_entropy": 2.5,
            "soft_blacklist": ["CODE", "GAME", "FREE", "VIP", "NAP", "RUT"],
        },
        "default": {
            "description": "Fallback nếu URL chưa thuộc nhóm nào",
            "url_keywords": [],
            "allowed_sites": [],
            "allow_numeric": True,
            "allow_random_mix": True,
            "require_uppercase": False,
            "force_uppercase": False,
            "remove_spaces": False,
            "prefer_spoiler": True,
            "min_entropy": 2.3,
            "uppercase_min_entropy": 2.9,
            "soft_blacklist": ["CODE", "GAME", "FREE", "VIP", "NAP", "RUT"],
        },
    }

    # ==========================================
    # 📡 TELEGRAM CHANNEL CONFIG
    # ==========================================
    KJC_SPECIAL_CHANNEL_IDS = {
        -1002528908352,  # KJC GÁI XINH
        -1003503954906,  # KJC - ĐỒNG HÀNH THỂ THAO
    }

    KJC_SPECIAL_MODE = get_bool_env("KJC_SPECIAL_MODE", True)

    # OCR_ALLOWED_CHANNEL_IDS là whitelist cơ bản. OCR_MEDIA_ONLY_CHANNEL_IDS
    # và OCR_MEDIA_FALLBACK_CHANNEL_IDS là các nhóm bổ sung theo loại media;
    # logic runtime dùng hợp nhất cả ba nhóm, trừ khi fallback toàn media được bật.
    OCR_ALLOWED_CHANNEL_IDS = set(
        get_int_list_env(
            "OCR_ALLOWED_CHANNEL_IDS",
            (-1002817093108,),  # PHÁT CODE XX88
        )
    )
    # Tắt OCR mặc định cho mọi channel ngoài whitelist bên trên. Tất cả
    # channel vẫn nhận và xử lý spoiler/caption/text; OCR chỉ là fallback media.
    OCR_MEDIA_ONLY_CHANNEL_IDS = get_int_list_env(
        "OCR_MEDIA_ONLY_CHANNEL_IDS",
        (),
    )
    # Không tự động thêm HI88/QQ88 vào OCR fallback; chỉ PHÁT CODE XX88
    # được bật qua OCR_ALLOWED_CHANNEL_IDS ở trên.
    OCR_MEDIA_FALLBACK_CHANNEL_IDS = get_int_list_env(
        "OCR_MEDIA_FALLBACK_CHANNEL_IDS",
        (),
    )
    OCR_FALLBACK_ALL_MEDIA = get_bool_env("OCR_FALLBACK_ALL_MEDIA", False)
    # Mặc định không tắt theo group: các channel cần tắt được đánh dấu riêng
    # bằng "ocr_enabled": False trong CHANNEL_CONFIG. Nhờ vậy channel mới
    # thêm sau này không tự động bị ảnh hưởng. Có thể dùng env này như một
    # override bổ sung, ví dụ: OCR_DISABLED_FILTER_GROUPS=hi88,qq88.
    # Có thể tắt riêng một channel bằng "ocr_enabled": False trong
    # Config.CHANNEL_CONFIG; cờ này có ưu tiên cao hơn whitelist/group.
    OCR_DISABLED_FILTER_GROUPS = {
        item.strip().lower()
        for item in os.getenv("OCR_DISABLED_FILTER_GROUPS", "").split(",")
        if item.strip()
    }

    # Chỉ channel này được phép OCR media/video. Mọi channel khác phải
    # ưu tiên spoiler/text và không được khởi động OCR.

    KJC_BROADCAST_DOMAINS = (
        "xx88code.com",
        "livemm88.net",
        "gg88live.tv",
        "rr88code.com",
    )

    KJC_BROADCAST_URLS = {
        "xx88code.com": "https://xx88code.com",
        "livemm88.net": "https://livemm88.net/nhap-code",
        "gg88live.tv": "https://gg88live.tv/nhap-code",
        "rr88code.com": "https://rr88code.com",
    }

    # Canonical account order per site. This override is applied after
    # channel-level config so every channel on the same site uses one list.
    DOMAIN_ACCOUNT_OVERRIDES = {
        "tangquaqq88.com": [
            {"username": "kaoboy012", "priority": 1},
            {"username": "kuuteo012", "priority": 2},
            {"username": "dad131", "priority": 3},
            {"username": "okletgo12", "priority": 4},
        ],
        "xx88code.com": [
            {"username": "dad131", "priority": 1},
            {"username": "hugolan", "priority": 2},
            {"username": "dad123", "priority": 3},
        ],
        "gg88live.tv": [
            {"username": "kaoboy012", "priority": 1},
            {"username": "conve99sau", "priority": 2},
            {"username": "hugolan012", "priority": 3},
        ],
        "livemm88.net": [
            {"username": "kaoboy012", "priority": 1},
            {"username": "dad131", "priority": 2},
            {"username": "ola12", "priority": 3},
            {"username": "dad123", "priority": 4},
        ],
        "rr88code.com": [
            {"username": "kaoboy012", "priority": 1},
            {"username": "miniichan", "priority": 2},
            {"username": "hugolan0123", "priority": 3},
        ],
        "o8code.com": [
            {"username": "kaoboy012", "priority": 1},
            {"username": "conve99sau", "priority": 2},
        ],
        "hi88-freecode.pages.dev": [
            {"username": "hugolan012", "priority": 1},
            {"username": "kuuteo012", "priority": 2},
            {"username": "minichan0123", "priority": 3},
        ],
    }

    CHANNEL_CONFIG = {
        -1002272716520: {
            "name": "QQ88 DỄ CHƠI, DỄ PHÁT TÀI",
            "url": "https://tangquaqq88.com",
            "filter_group": "qq88",
            "ocr_enabled": False,
            "priority": 1,
            "has_video": False,
            "accounts": [
                {"username": "kaoboy012", "priority": 1},
                {"username": "kuuteo012", "priority": 2},
                {"username": "dad131", "priority": 3},
                {"username": "okletgo123", "priority": 4},
            ],
        },

        -1002528908352: {
            "name": "KJC GÁI XINH",
            "url": "https://xx88code.com",
            "filter_group": "multi_site_strict",
            "ocr_enabled": False,
            "priority": 100,
            "has_video": False,
            "enabled": True,
            "accounts": [
                {"username": "dad131", "priority": 1},
                {"username": "hugolan", "priority": 2},
                {"username": "dad123", "priority": 3},
            ],
        },

        -1003503954906: {
            "name": "KJC - ĐỒNG HÀNH THỂ THAO",
            "url": "https://xx88code.com",
            "filter_group": "multi_site_strict",
            "ocr_enabled": False,
            "priority": 101,
            "has_video": False,
            "enabled": True,
            "accounts": [
                {"username": "dad131", "priority": 1},
                {"username": "hugolan", "priority": 2},
                {"username": "dad123", "priority": 3},
            ],
        },

        -1002817093108: {
            "name": "PHÁT CODE XX88",
            "url": "https://xx88code.com/",
            "filter_group": "multi_site_strict",
            "ocr_enabled": True,
            "priority": 6,
            "has_video": True,
            # Fast path: dùng một vùng bao phủ duy nhất để chỉ chạy một
            # inference OCR/frame. CodeValidator và bộ lọc banner loại phần
            # text quảng cáo còn lại; cách này nhanh hơn hai crop song song.
            "ocr_crop": (0.10, 0.99, 0.05, 0.99),
            "ocr_crops": [(0.10, 0.99, 0.05, 0.99)],
            # OCR tại giây 18 vì khung hình này chứa vùng mã rõ hơn.
            "ocr_frame_seconds": [18],
            "ocr_single_code_mode": False,
            # Không yêu cầu consensus vì chỉ chạy một crop bao phủ toàn vùng.
            "ocr_require_consensus": False,
            "max_ocr_codes_per_batch": 5,
            "accounts": [
                {"username": "dad131", "priority": 1},
                {"username": "hugolan", "priority": 2},
                {"username": "dad123", "priority": 3},
            ],
        },
        -1003734537786: {
            "name": "XX88 SĂN CODE MỖI NGÀY",
            "url": "https://xx88code.com/",
            "filter_group": "multi_site_strict",
            "ocr_enabled": False,
            "priority": 7,
            "has_video": False,
            "accounts": [
                {"username": "dad131", "priority": 1},
                {"username": "hugolan", "priority": 2},
                {"username": "dad123", "priority": 3},
            ],
        },
        -1002768264448: {
            "name": "XX88 THỂ THAO ESPORT",
            "url": "https://xx88code.com/",
            "filter_group": "multi_site_strict",
            "ocr_enabled": False,
            "priority": 8,
            "has_video": False,
            "accounts": [
                {"username": "dad131", "priority": 1},
                {"username": "hugolan", "priority": 2},
                {"username": "dad123", "priority": 3},
            ],
        },
        -1002730903277: {
            "name": "XX88 DỊCH VỤ GIAI NHÂN",
            "url": "https://xx88code.com/",
            "filter_group": "multi_site_strict",
            "ocr_enabled": False,
            "priority": 9,
            "has_video": False,
            "accounts": [
                {"username": "dad131", "priority": 1},
                {"username": "hugolan", "priority": 2},
                {"username": "dad123", "priority": 3},
            ],
        },
        -1003731231345: {
            "name": "GG88 DỊCH VỤ GIAI NHÂN",
            "url": "https://gg88live.tv/nhap-code",
            "filter_group": "multi_site_strict",
            "ocr_enabled": False,
            "priority": 10,
            "has_video": False,
            "accounts": [
                {"username": "kaoboy012", "priority": 1},
                {"username": "conve99sau", "priority": 2},
                {"username": "hugolan012", "priority": 3},
            ],
        },
        -1002421765170: {
            "name": " QQ88 - KHO GIF",
            "url": "https://tangquaqq88.com/",
            "filter_group": "qq88",
            "ocr_enabled": False,
            "priority": 11,
            "has_video": False,
            "accounts": [
                {"username": "kaoboy012", "priority": 1},
                {"username": "kuuteo012", "priority": 2},
                {"username": "dad131", "priority": 3},
                {"username": "okletgo123", "priority": 4},
            ],
        },
        -1003134541072: {
            "name": "MM88VIP Dịch Vụ Giai Nhân",
            "url": "https://livemm88.net/nhap-code",
            "filter_group": "mm88",
            "ocr_enabled": False,
            "priority": 13,
            "has_video": False,
            "accounts": [
                {"username": "kaoboy012", "priority": 1},
                {"username": "dad131", "priority": 2},
                {"username": "ola12", "priority": 3},
                {"username": "dad123", "priority": 4},
            ],
        },
        -1002278162941: {
            "name": " QQ88 - TIN HOT 24/7",
            "url": "https://tangquaqq88.com",
            "filter_group": "qq88",
            "ocr_enabled": False,
            "priority": 14,
            "has_video": False,
            "accounts": [
                {"username": "kaoboy012", "priority": 1},
                {"username": "kuuteo012", "priority": 2},
                {"username": "dad131", "priority": 3},
                {"username": "okletgo123", "priority": 4},
            ],
        },
        -1002324210129: {
            "name": "QQ88 - GIẢI TRÍ",
            "url": "https://tangquaqq88.com",
            "filter_group": "qq88",
            "ocr_enabled": False,
            "priority": 15,
            "has_video": False,
            "accounts": [
                {"username": "kaoboy012", "priority": 1},
                {"username": "kuuteo012", "priority": 2},
                {"username": "dad131", "priority": 3},
                {"username": "okletgo123", "priority": 4},
            ],
        },
        -1002377579866: {
            "name": "QQ88 - TIN TỨC MỖI NGÀY",
            "url": "https://tangquaqq88.com",
            "filter_group": "qq88",
            "ocr_enabled": False,
            "priority": 16,
            "has_video": False,
            "accounts": [
                {"username": "kaoboy012", "priority": 1},
                {"username": "kuuteo012", "priority": 2},
                {"username": "dad131", "priority": 3},
                {"username": "okletgo123", "priority": 4},
            ],
        },
        -1002325212717: {
            "name": "QQ88 - REVIEW PHIM HAY",
            "url": "https://tangquaqq88.com",
            "filter_group": "qq88",
            "ocr_enabled": False,
            "priority": 18,
            "has_video": False,
            "accounts": [
                {"username": "kaoboy012", "priority": 1},
                {"username": "kuuteo012", "priority": 2},
                {"username": "dad131", "priority": 3},
                {"username": "okletgo123", "priority": 4},
            ],
        },
        -1003802387209: {
            "name": "o8 TIN HOT 24H",
            "url": "https://o8code.com",
            "filter_group": "o8",
            "ocr_enabled": False,
            "priority": 19,
            "has_video": False,
            "accounts": [
                {"username": "kaoboy012", "priority": 1},
                {"username": "conve99sau", "priority": 2},
            ],
        },
        -1003574944644: {
            "name": "o8 - TROLL BÓNG ĐÁ",
            "url": "https://o8code.com",
            "filter_group": "o8",
            "ocr_enabled": False,
            "priority": 20,
            "has_video": False,
            "accounts": [
                {"username": "kaoboy012", "priority": 1},
                {"username": "conve99sau", "priority": 2},
            ],
        },
        -1002386905514: {
            "name": "RR88 DỊCH VỤ GIAI NHÂN",
            "url": "https://rr88code.com",
            "filter_group": "multi_site_strict",
            "ocr_enabled": False,
            "priority": 21,
            "has_video": False,
            "accounts": [
                {"username": "kaoboy012", "priority": 1},
                {"username": "miniichan", "priority": 2},
            ],
        },
        -1002446066378: {
            "name": "QQ88 - PHÁT CODE MIỄN PHÍ",
            "url": "https://tangquaqq88.com",
            "filter_group": "qq88",
            "ocr_enabled": False,
            "priority": 23,
            "has_video": False,
            "accounts": [
                {"username": "kaoboy012", "priority": 1},
                {"username": "kuuteo012", "priority": 2},
                {"username": "dad131", "priority": 3},
                {"username": "okletgo123", "priority": 4},
            ],
        },
        -1004435825431: {
            "name": "Hi88 PHÁT CODE MIỄN PHÍ NỖ HŨ-BẮN CÁ",
            "url": "https://hi88-freecode.pages.dev/",
            "filter_group": "hi88",
            "ocr_enabled": False,
            "priority": 24,
            "has_video": False,
            "ocr_crop": (0.33, 0.72, 0.05, 0.97),
            "accounts": [
                {"username": "hugolan012", "priority": 1},
                {"username": "kuuteo012", "priority": 2},
                {"username": "teoem0123", "priority": 3},
                {"username": "minichan0123", "priority": 4},
            ],
        },
        -1003933844700: {
            "name": "Hi88 - CƯỢC GIẢI TRÍ, KIẾM TIỀN TỶ",
            "url": "https://hi88-freecode.pages.dev/",
            "filter_group": "hi88",
            "ocr_enabled": False,
            "priority": 25,
            "has_video": False,
            "accounts": [
                {"username": "hugolan012", "priority": 1},
                {"username": "kuuteo012", "priority": 2},
                {"username": "teoem0123", "priority": 3},
                {"username": "minichan0123", "priority": 4},
            ],
        },
        -1002695720902: {
            "name": "Hi88 - KÊNH GIẢI TRÍ HOT",
            "url": "https://hi88-freecode.pages.dev/",
            "filter_group": "hi88",
            "ocr_enabled": False,
            "priority": 26,
            "has_video": False,
            "accounts": [
                {"username": "hugolan012", "priority": 1},
                {"username": "kuuteo012", "priority": 2},
                {"username": "teoem0123", "priority": 3},
                {"username": "minichan0123", "priority": 4},
            ],
        },
        -1002657420328: {
            "name": "Hi88 - TIN HOT MỖI NGÀY",
            "url": "https://hi88-freecode.pages.dev/",
            "filter_group": "hi88",
            "ocr_enabled": False,
            "priority": 27,
            "has_video": False,
            "accounts": [
                {"username": "hugolan012", "priority": 1},
                {"username": "kuuteo012", "priority": 2},
                {"username": "teoem0123", "priority": 3},
                {"username": "minichan0123", "priority": 4},
            ],
        },
        -1002018121888: {
            "name": "Hi88 - KHO GIF",
            "url": "https://hi88-freecode.pages.dev/",
            "filter_group": "hi88",
            "ocr_enabled": False,
            "priority": 28,
            "has_video": False,
            "ocr_crop": None,
            "accounts": [
                {"username": "hugolan012", "priority": 1},
                {"username": "kuuteo012", "priority": 2},
                {"username": "teoem0123", "priority": 3},
                {"username": "minichan0123", "priority": 4},
            ],
        },
        -1002662584621: {
            "name": "Hi88 - REVIEW PHIM HAY MỖI NGÀY",
            "url": "https://hi88-freecode.pages.dev/",
            "filter_group": "hi88",
            "ocr_enabled": False,
            "priority": 29,
            "has_video": False,
            "accounts": [
                {"username": "hugolan012", "priority": 1},
                {"username": "kuuteo012", "priority": 2},
                {"username": "teoem0123", "priority": 3},
                {"username": "minichan0123", "priority": 4},
            ],
        },
        -1002625548636: {
            "name": "Hi88 TUYỂN ĐẠI LÝ HOA HỒNG 60%",
            "url": "https://hi88-freecode.pages.dev/",
            "filter_group": "hi88",
            "ocr_enabled": False,
            "priority": 30,
            "has_video": False,
            "accounts": [
                {"username": "hugolan012", "priority": 1},
                {"username": "kuuteo012", "priority": 2},
                {"username": "teoem0123", "priority": 3},
                {"username": "minichan0123", "priority": 4},
            ],
        },
        -1003936595246: {
            "name": "GÁI 18+",
            "url": "https://livemm88.net/nhap-code",
            "filter_group": "mm88",
            "ocr_enabled": False,
            "priority": 31,
            "has_video": False,
            "accounts": [
                {"username": "kaoboy012", "priority": 1},
                {"username": "dad131", "priority": 2},
                {"username": "ola12", "priority": 3},
                {"username": "dad123", "priority": 4},
            ],
        },
        -1003939163957: {
            "name": "MM88 GIRL DANCE",
            "url": "https://livemm88.net/nhap-code",
            "filter_group": "mm88",
            "ocr_enabled": False,
            "priority": 32,
            "has_video": False,
            "accounts": [
                {"username": "kaoboy012", "priority": 1},
                {"username": "dad131", "priority": 2},
                {"username": "ola12", "priority": 3},
                {"username": "dad123", "priority": 4},
            ],
        },
        -1002519029952: {
            "name": "MM88 ĐỘNG BÀN TƠ",
            "url": "https://livemm88.net/nhap-code",
            "filter_group": "mm88",
            "ocr_enabled": False,
            "priority": 33,
            "has_video": False,
            "accounts": [
                {"username": "kaoboy012", "priority": 1},
                {"username": "dad131", "priority": 2},
                {"username": "ola12", "priority": 3},
                {"username": "dad123", "priority": 4},
            ],
        },
        -1003396129975: {
            "name": "o8 DỊCH VỤ GIAI NHÂN",
            "url": "https://o8code.com",
            "filter_group": "o8",
            "ocr_enabled": False,
            "priority": 34,
            "has_video": False,
            "accounts": [
                {"username": "kaoboy012", "priority": 1},
                {"username": "conve99sau", "priority": 2},
            ],
        },
        -1003904150684: {
            "name": "o8 SOI KÈO 24/7",
            "url": "https://o8code.com",
            "filter_group": "o8",
            "ocr_enabled": False,
            "priority": 35,
            "has_video": False,
            "accounts": [
                {"username": "kaoboy012", "priority": 1},
                {"username": "conve99sau", "priority": 2},
            ],
        },
        -1004411105242: {
            "name": "KÈO BÓNG GG88",
            "url": "https://gg88live.tv/nhap-code",
            "filter_group": "multi_site_strict",
            "ocr_enabled": False,
            "priority": 36,
            "has_video": False,
            "accounts": [
                {"username": "kaoboy012", "priority": 1},
                {"username": "conve99sau", "priority": 2},
                {"username": "hugolan012", "priority": 3},
            ],
        },
        -1003912975699: {
            "name": "SOI KÈO MM88",
            "url": "https://livemm88.net/nhap-code",
            "filter_group": "mm88",
            "ocr_enabled": False,
            "priority": 37,
            "has_video": False,
            "accounts": [
                {"username": "kaoboy012", "priority": 1},
                {"username": "dad131", "priority": 2},
                {"username": "ola12", "priority": 3},
                {"username": "dad123", "priority": 4},
            ],
        },
        -1004406362195: {
            "name": "RR88 SOI KÈO",
            "url": "https://rr88code.com",
            "filter_group": "multi_site_strict",
            "ocr_enabled": False,
            "priority": 38,
            "has_video": False,
            "accounts": [
                {"username": "kaoboy012", "priority": 1},
                {"username": "miniichan", "priority": 2},
                {"username": "hugolan0123", "priority": 3},
            ],
        },
        -1004352437280: {
            "name": "SOI KÈO CÙNG XX88",
            "url": "https://xx88code.com",
            "filter_group": "multi_site_strict",
            "ocr_enabled": False,
            "priority": 39,
            "has_video": False,
            "accounts": [
                {"username": "dad131", "priority": 1},
                {"username": "hugolan", "priority": 2},
                {"username": "dad123", "priority": 3},
            ],
        },
    }

    # Nếu không có env override, các nhóm được derive sau khi class hoàn tất
    # từ filter_group trong CHANNEL_CONFIG để không bị lệch khi thêm/bớt chat.
    QQ88_CHAT_IDS = get_int_list_env("QQ88_CHAT_IDS", ())
    HI88_CHAT_IDS = get_int_list_env("HI88_CHAT_IDS", ())

    # ==========================================
    # 🚫 BLACKLIST
    # ==========================================
    CODE_BLACKLIST = [
        "COM", "HTTP", "HTTPS", "WWW",
        "FACEBOOK", "TELEGRAM",
        "CHECK", "CLIP", "VUI", "BOT",
        "DAILY", "TRUYCAP", "BANCA", "NOHU",
        "ONLINE", "FREE", "CODE", "GIFTCODE",
        "MINIGAME", "GAME", "THETHAO",
        "O8THETHAO", "BONGDA", "TROLL",
    ]

    # ==========================================
    # ⚙️ FEATURE FLAGS
    # ==========================================
    ENABLE_MONITORING = True
    TELEGRAM_ADMIN_ID = get_int_env("TELEGRAM_ADMIN_ID", 0)

    # ==========================================
    # ⏱️ TIMEOUT / SPEED
    # ==========================================
    SITE_CODE_DEDUP_TTL = get_float_env("SITE_CODE_DEDUP_TTL", 30.0)
    PAGE_NAVIGATION_TIMEOUT = get_int_env("PAGE_NAVIGATION_TIMEOUT", 10000)
    FORM_SETTLE_SECONDS = get_float_env("FORM_SETTLE_SECONDS", 0.03)
    CF_WAIT_SECONDS = get_float_env("CF_WAIT_SECONDS", 1.0)
    CF_POLL_INTERVAL = get_float_env("CF_POLL_INTERVAL", 0.10)
    VERIFICATION_BUTTON_WAIT_SECONDS = get_float_env("VERIFICATION_BUTTON_WAIT_SECONDS", 15.0)
    GG88_TURNSTILE_WAIT_SECONDS = get_float_env("GG88_TURNSTILE_WAIT_SECONDS", 15.0)
    HI88_VERIFICATION_WAIT_SECONDS = get_float_env("HI88_VERIFICATION_WAIT_SECONDS", 15.0)
    QQ88_VERIFICATION_WAIT_SECONDS = get_float_env("QQ88_VERIFICATION_WAIT_SECONDS", 20.0)

    MAX_CONCURRENT_SUBMITS            = get_int_env("MAX_CONCURRENT_SUBMITS", 8)
    MAX_CONCURRENT_SUBMITS_PER_DOMAIN = get_int_env("MAX_CONCURRENT_SUBMITS_PER_DOMAIN", 2)
    # Mỗi code được phát cho bao nhiêu tài khoản trong cùng một đợt.
    ACCOUNTS_PER_CODE                 = get_int_env("ACCOUNTS_PER_CODE", 2)
    MAX_RETRIES_PER_ACCOUNT           = get_int_env("MAX_RETRIES_PER_ACCOUNT", 2)
    RETRY_ON_TIMEOUT                  = get_bool_env("RETRY_ON_TIMEOUT", True)

    RATE_LIMIT_BACKOFF_SECONDS = get_float_env("RATE_LIMIT_BACKOFF_SECONDS", 5.0)
    MAX_RATE_LIMIT_RETRIES = get_int_env("MAX_RATE_LIMIT_RETRIES", 5)

    REQUESTS_PER_MINUTE = get_int_env("REQUESTS_PER_MINUTE", 30)
    MAX_BURST           = get_int_env("MAX_BURST", 5)

    # ==========================================
    # ⚡ TELEGRAM REALTIME / QUEUE
    # ==========================================
    MESSAGE_QUEUE_MAXSIZE      = get_int_env("MESSAGE_QUEUE_MAXSIZE", 2000)
    MESSAGE_WORKERS            = get_int_env("MESSAGE_WORKERS", 4)
    MAX_CONCURRENT_PROCESSING  = get_int_env("MAX_CONCURRENT_PROCESSING", 24)
    # Bound download → frame extraction → OCR → submit fan-out as one media job.
    MAX_CONCURRENT_MEDIA_PIPELINES = get_int_env("MAX_CONCURRENT_MEDIA_PIPELINES", 4)
    MEDIA_PIPELINE_QUEUE_MAXSIZE = get_int_env("MEDIA_PIPELINE_QUEUE_MAXSIZE", 200)

    HEARTBEAT_INTERVAL            = get_float_env("HEARTBEAT_INTERVAL", 300.0)
    TELEGRAM_CATCH_UP             = get_bool_env("TELEGRAM_CATCH_UP", False)
    TELEGRAM_INBOX_RECOVERY_MODE  = os.getenv(
        "TELEGRAM_INBOX_RECOVERY_MODE",
        "live_only",
    ).strip().lower()
    if TELEGRAM_INBOX_RECOVERY_MODE not in {"at_least_once", "live_only"}:
        logging.getLogger(__name__).warning(
            "⚠️ TELEGRAM_INBOX_RECOVERY_MODE=%r không hợp lệ; dùng live_only",
            TELEGRAM_INBOX_RECOVERY_MODE,
        )
        TELEGRAM_INBOX_RECOVERY_MODE = "live_only"
    # Chỉ nhận event từ CHANNEL_CONFIG ngay tại Telethon/quick(); tránh đưa
    # chat ngoài danh sách vào DurableInbox và tạo tải giả cho worker.
    TELEGRAM_FILTER_AT_SOURCE     = get_bool_env("TELEGRAM_FILTER_AT_SOURCE", True)
    # Chỉ bật khi chẩn đoán peer/channel ID; mặc định không log mọi event
    # Telegram để tránh làm nghẽn event loop.
    TELEGRAM_LOG_ALL_INGRESS      = get_bool_env("TELEGRAM_LOG_ALL_INGRESS", False)

    DOMAIN_QUEUE_MAXSIZE = get_int_env("DOMAIN_QUEUE_MAXSIZE", 500)
    TELEGRAM_INBOX_DB_PATH = os.getenv("TELEGRAM_INBOX_DB_PATH", "data/telegram_inbox.db")
    TELEGRAM_INBOX_LEASE_SECONDS = get_int_env("TELEGRAM_INBOX_LEASE_SECONDS", 300)
    TELEGRAM_INBOX_DRAIN_BATCH = get_int_env("TELEGRAM_INBOX_DRAIN_BATCH", 250)
    # Poll nhanh hơn để row mới không phải chờ nửa giây trước khi vào worker.
    TELEGRAM_INBOX_DRAIN_INTERVAL = get_float_env("TELEGRAM_INBOX_DRAIN_INTERVAL", 0.1)
    TELEGRAM_INBOX_ENQUEUE_RETRIES = get_int_env("TELEGRAM_INBOX_ENQUEUE_RETRIES", 3)
    TELEGRAM_INBOX_SQLITE_DEFERRED_RETRIES = get_int_env(
        "TELEGRAM_INBOX_SQLITE_DEFERRED_RETRIES", 3
    )
    TELEGRAM_INBOX_SQLITE_DEFERRED_RETRY_DELAY = get_float_env(
        "TELEGRAM_INBOX_SQLITE_DEFERRED_RETRY_DELAY", 2.0
    )
    SHUTDOWN_MESSAGE_DRAIN_TIMEOUT = get_float_env("SHUTDOWN_MESSAGE_DRAIN_TIMEOUT", 8.0)
    MAX_CONCURRENT_INGRESS_TASKS = get_int_env("MAX_CONCURRENT_INGRESS_TASKS", 64)

    # ✅ FIX RETRY-STORM: trước đây DurableInbox.retry() không có giới hạn số
    # lần thử → 1 tin nhắn lỗi (site trả NO_RESULT liên tục, CDP mất kết nối...)
    # bị replay lại vô hạn mỗi vài giây, chiếm slot xử lý và làm trễ tin nhắn
    # mới. Giờ mọi retry đi qua DurableInbox.retry_or_fail(), bị chặn ở
    # MAX_INBOX_ATTEMPTS lần thử với backoff tăng dần (INBOX_RETRY_BASE_DELAY
    # → nhân đôi mỗi lần, trần ở INBOX_RETRY_MAX_DELAY).
    MAX_INBOX_ATTEMPTS = get_int_env("MAX_INBOX_ATTEMPTS", 5)
    INBOX_RETRY_BASE_DELAY = get_float_env("INBOX_RETRY_BASE_DELAY", 2.0)
    INBOX_RETRY_MAX_DELAY = get_float_env("INBOX_RETRY_MAX_DELAY", 120.0)

    # Quét dọn định kỳ các dòng 'pending' bị kẹt quá lâu (lỗi không xác định,
    # crash giữa chừng...) — giftcode gần như luôn hết hạn trước mốc này nên
    # an toàn khi bỏ qua, tránh backlog cũ tiếp tục dồn ứ queue.
    INBOX_MAX_PENDING_AGE_SECONDS = get_float_env("INBOX_MAX_PENDING_AGE_SECONDS", 900.0)
    INBOX_PENDING_SWEEP_INTERVAL_SECONDS = get_float_env("INBOX_PENDING_SWEEP_INTERVAL_SECONDS", 300.0)

    # ==========================================
    # ⚙️ WATCHDOG / MISC
    # ==========================================
    INPUT_CACHE_CLEANUP_INTERVAL = get_float_env("INPUT_CACHE_CLEANUP_INTERVAL", 30.0)

    # ==========================================
    # 🆕 OCR (chỉ PHÁT CODE XX88; tối đa 2 ảnh xử lý đồng thời)
    # ==========================================
    OCR_CONFIDENCE_THRESHOLD = get_float_env("OCR_CONFIDENCE_THRESHOLD", 0.70)
    # Giới hạn cạnh dài trước inference để giảm đáng kể số pixel cần xử lý.
    OCR_MAX_IMAGE_SIDE = get_int_env("OCR_MAX_IMAGE_SIDE", 1280)
    OCR_MIN_CROP_WIDTH = get_int_env("OCR_MIN_CROP_WIDTH", 300)
    # Cho phép xử lý song song có kiểm soát khi nhiều ảnh tới cùng lúc.
    # RapidOCR dùng chung engine singleton; không nên đặt quá cao trên CPU.
    MAX_CONCURRENT_OCR = get_int_env("MAX_CONCURRENT_OCR", 2)
    VIDEO_OCR_MAX_FRAMES = get_int_env("VIDEO_OCR_MAX_FRAMES", 3)
    # Tải media là bottleneck I/O chính; OCR vẫn giữ MAX_CONCURRENT_OCR=2
    # để không làm nghẽn CPU/RAM.
    MAX_CONCURRENT_MEDIA_DOWNLOADS = get_int_env("MAX_CONCURRENT_MEDIA_DOWNLOADS", 2)
    FAST_TELEGRAM_DOWNLOAD = get_bool_env("FAST_TELEGRAM_DOWNLOAD", False)
    FAST_DOWNLOAD_MIN_BYTES = get_int_env("FAST_DOWNLOAD_MIN_BYTES", 4 * 1024 * 1024)
    FAST_DOWNLOAD_WORKERS = get_int_env("FAST_DOWNLOAD_WORKERS", 6)
    FAST_DOWNLOAD_CHUNK_KB = get_int_env("FAST_DOWNLOAD_CHUNK_KB", 512)
    FAST_DOWNLOAD_MAX_CONNECTIONS = get_int_env("FAST_DOWNLOAD_MAX_CONNECTIONS", 8)
    OCR_FAST_PATH = get_bool_env("OCR_FAST_PATH", True)
    OCR_FAST_MIN_CHARS = get_int_env("OCR_FAST_MIN_CHARS", 8)
    OCR_FAST_VARIANTS = get_int_env("OCR_FAST_VARIANTS", 1)
    OCR_DEBUG_SAVE_FRAMES = get_bool_env("OCR_DEBUG_SAVE_FRAMES", False)
    QUIET_RAPIDOCR_STARTUP_LOGS = get_bool_env("QUIET_RAPIDOCR_STARTUP_LOGS", True)
    MEDIA_DOWNLOAD_RETRIES = get_int_env("MEDIA_DOWNLOAD_RETRIES", 1)
    MEDIA_DOWNLOAD_RETRY_DELAY = get_float_env("MEDIA_DOWNLOAD_RETRY_DELAY", 0.5)
    MEDIA_DOWNLOAD_MAX_SIZE_MB = get_int_env("MEDIA_DOWNLOAD_MAX_SIZE_MB", 200)
    # Dọn các thư mục media OCR bị sót sau crash theo chu kỳ 1 ngày.
    MEDIA_CLEANUP_INTERVAL_SECONDS = get_int_env(
        "MEDIA_CLEANUP_INTERVAL_SECONDS", 24 * 60 * 60
    )
    OCR_TEMP_DIR_MAX_AGE_SECONDS = get_int_env(
        "OCR_TEMP_DIR_MAX_AGE_SECONDS", 24 * 60 * 60
    )
    MAX_OCR_CODES_PER_BATCH = get_int_env("MAX_OCR_CODES_PER_BATCH", 4)
    PAUSE_OCR_ON_HIGH_CPU = get_bool_env("PAUSE_OCR_ON_HIGH_CPU", True)
    OCR_CPU_THRESHOLD = get_int_env("OCR_CPU_THRESHOLD", 85)
    OCR_RAM_PAUSE_THRESHOLD = get_int_env("OCR_RAM_PAUSE_THRESHOLD", 90)
    OCR_RAM_WARN_THRESHOLD = get_int_env("OCR_RAM_WARN_THRESHOLD", 85)
    HEALTH_CHECK_INTERVAL = get_float_env("HEALTH_CHECK_INTERVAL", 30.0)
    HEALTH_BREACH_CONFIRM_COUNT = get_int_env("HEALTH_BREACH_CONFIRM_COUNT", 2)
    QUEUE_SOFT_LIMIT_PCT = get_float_env("QUEUE_SOFT_LIMIT_PCT", 0.75)
    QUEUE_HARD_LIMIT_PCT = get_float_env("QUEUE_HARD_LIMIT_PCT", 0.95)
    QUEUE_CLEANUP_ON_SOFT_LIMIT = get_bool_env("QUEUE_CLEANUP_ON_SOFT_LIMIT", False)
    QUEUE_CLEANUP_TARGET_PCT = get_float_env("QUEUE_CLEANUP_TARGET_PCT", 0.50)
    QUEUE_CHECK_INTERVAL = get_float_env("QUEUE_CHECK_INTERVAL", 5.0)

    # ==========================================
    # 🆕 LOG ROTATION
    # ==========================================
    LOG_ROTATION_MAX_BYTES    = get_int_env("LOG_ROTATION_MAX_BYTES", 10_485_760)
    LOG_ROTATION_BACKUP_COUNT = get_int_env("LOG_ROTATION_BACKUP_COUNT", 5)

    # ==========================================
    # 🌐 BROWSER-ONLY
    # ==========================================
    BROWSER_ONLY = True
    BROWSER_CIRCUIT_FAILURE_THRESHOLD = get_int_env("BROWSER_CIRCUIT_FAILURE_THRESHOLD", 3)
    BROWSER_CIRCUIT_COOLDOWN_SECONDS = get_float_env("BROWSER_CIRCUIT_COOLDOWN_SECONDS", 60.0)


    # Cổng debug Edge — phải khớp với Edge đang chạy sẵn trên máy.
    # (--remote-debugging-port=<cổng này>).
    EDGE_CDP_HOST = os.getenv("EDGE_CDP_HOST", "127.0.0.1").strip() or "127.0.0.1"
    EDGE_CDP_PORT = get_int_env("EDGE_CDP_PORT", 9222)
    # Đường dẫn msedge.exe — chỉ dùng khi bot cần TỰ mở lại Edge (CDP mất
    # kết nối và không tự hồi phục được).
    EDGE_EXECUTABLE_PATH = os.getenv(
        "EDGE_EXECUTABLE_PATH", r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
    )
    # Thư mục profile Edge dùng khi bot tự mở lại Edge — để trống thì dùng
    # thư mục "edge_bot_profile" cạnh main_script.py.
    EDGE_PROFILE_DIR = os.getenv("EDGE_PROFILE_DIR", "")
    EDGE_PROFILE_NAME = os.getenv("EDGE_PROFILE_NAME", "Default")

    # Giữ hai tab nóng/domain, khớp MAX_CONCURRENT_SUBMITS_PER_DOMAIN mặc
    # định 2, để submit thứ hai không phải mở/điều hướng tab trong lúc live.
    TAB_POOL_SIZE = get_int_env("TAB_POOL_SIZE", 2)
    TAB_ACQUIRE_WAIT_SECONDS = get_float_env("TAB_ACQUIRE_WAIT_SECONDS", 2.0)
    MAX_TAB_PER_DOMAIN_CAP = get_int_env("MAX_TAB_PER_DOMAIN_CAP", 5)
    # GC chỉ đóng tab phụ không dùng lâu; luôn giữ tối thiểu một tab/domain.
    TAB_POOL_IDLE_TTL = get_float_env("TAB_POOL_IDLE_TTL", 900.0)
    TAB_POOL_MIN_TABS_PER_DOMAIN = get_int_env("TAB_POOL_MIN_TABS_PER_DOMAIN", 2)
    TAB_POOL_MEMORY_COMPACT_IDLE_SECONDS = get_float_env(
        "TAB_POOL_MEMORY_COMPACT_IDLE_SECONDS", 300.0
    )
    TAB_POOL_MEMORY_RELOAD_IDLE_SECONDS = get_float_env(
        "TAB_POOL_MEMORY_RELOAD_IDLE_SECONDS", 1800.0
    )
    # Nếu 1 domain hết tab rảnh và chưa chạm MAX_TAB_PER_DOMAIN_CAP, có tự
    # mở thêm tab mới không (true) hay chỉ dùng đúng số tab có sẵn (false).
    AUTO_OPEN_MISSING_TABS = get_bool_env("AUTO_OPEN_MISSING_TABS", True)

    # Nhịp browser_watchdog: kiểm tra/reconnect CDP trước, rồi mới phục hồi tab.
    CDP_PING_INTERVAL = get_float_env("CDP_PING_INTERVAL", 60.0)
    EDGE_CDP_WATCHDOG_INTERVAL = get_float_env("EDGE_CDP_WATCHDOG_INTERVAL", 5.0)

    # Thời gian tối đa (ms) chờ đọc kết quả sau khi bấm submit, và nhịp
    # poll giữa các lần đọc (giây) — RESULT_POLL_INTERVAL đã có sẵn trong
    # .env (dùng chung với phần queue trước đây).
    RESULT_DETECTION_TIMEOUT = get_float_env("RESULT_DETECTION_TIMEOUT", 5000.0)
    RESULT_SELECTOR_FAST_WINDOW_SECONDS = get_float_env("RESULT_SELECTOR_FAST_WINDOW_SECONDS", 0.80)
    RESULT_POLL_INTERVAL = get_float_env("RESULT_POLL_INTERVAL", 0.05)
    SUBMIT_STAGE_TIMING_LOG = get_bool_env("SUBMIT_STAGE_TIMING_LOG", True)
    MM88_FORM_SETTLE_SECONDS = get_float_env("MM88_FORM_SETTLE_SECONDS", 0.05)
    MM88_RESULT_DETECTION_TIMEOUT = get_float_env("MM88_RESULT_DETECTION_TIMEOUT", 3500.0)
    RESULT_DETECTION_TIMEOUT_BY_DOMAIN = {
        "tangquaqq88.com": get_float_env("QQ88_RESULT_DETECTION_TIMEOUT", 3500.0),
        "hi88-freecode.pages.dev": get_float_env("HI88_RESULT_DETECTION_TIMEOUT", 4500.0),
        "livemm88.net": get_float_env("MM88_RESULT_DETECTION_TIMEOUT", 4000.0),
        "rr88code.com": get_float_env("RR88_RESULT_DETECTION_TIMEOUT", 3000.0),
        "xx88code.com": get_float_env("XX88_RESULT_DETECTION_TIMEOUT", 3500.0),
        "gg88live.tv": get_float_env("GG88_RESULT_DETECTION_TIMEOUT", 3500.0),
        "o8code.com": get_float_env("O8_RESULT_DETECTION_TIMEOUT", 3000.0),
    }
    PRESERVE_PREFILLED_USERNAME = get_bool_env("PRESERVE_PREFILLED_USERNAME", True)

    # Sau bao nhiêu lần submit thì reload lại trang 1 lần cho "sạch" (thay
    # vì chỉ xoá input) — tránh trang bị phình state sau nhiều lượt submit.
    FULL_RELOAD_EVERY_N = get_int_env("FULL_RELOAD_EVERY_N", 100)

    # true = chụp ảnh + lưu HTML khi kết quả submit không rõ ràng (giúp
    # debug khi giao diện 1 site đã đổi so với lúc code chạy trước đó).
    SCREENSHOT_ON_UNKNOWN = get_bool_env("SCREENSHOT_ON_UNKNOWN", True)


# Derive các nhóm chat và URL theo domain sau khi class đã được tạo. Cách đặt
# sau class tránh giới hạn scope của comprehension trong thân class.
if not os.getenv("QQ88_CHAT_IDS", "").strip():
    Config.QQ88_CHAT_IDS = tuple(
        int(cid) for cid, cfg in Config.CHANNEL_CONFIG.items()
        if cfg.get("filter_group") == "qq88"
    )
if not os.getenv("HI88_CHAT_IDS", "").strip():
    Config.HI88_CHAT_IDS = tuple(
        int(cid) for cid, cfg in Config.CHANNEL_CONFIG.items()
        if cfg.get("filter_group") == "hi88"
    )
# Không để một env override OCR cũ làm mất channel HI88/QQ88 mới thêm vào
# CHANNEL_CONFIG. Các channel thuộc hai nhóm này thường phát code bằng ảnh;
# luôn hợp nhất ID cấu hình với whitelist media hiện tại, loại trùng nhưng vẫn
# giữ thứ tự để log/debug dễ đối chiếu.
_configured_media_channels = tuple(
    int(cid)
    for cid, cfg in Config.CHANNEL_CONFIG.items()
    if cfg.get("ocr_enabled") is True
)
Config.OCR_MEDIA_FALLBACK_CHANNEL_IDS = tuple(
    dict.fromkeys(
        int(cid)
        for cid in (
            *getattr(Config, "OCR_MEDIA_FALLBACK_CHANNEL_IDS", ()),
            *_configured_media_channels,
        )
    )
)

Config.DOMAIN_TO_CHANNEL_URL: dict[str, str] = {}
for _cfg in Config.CHANNEL_CONFIG.values():
    _raw_url = str(_cfg.get("url", ""))
    _domain = _raw_url.split("//", 1)[-1].split("/", 1)[0].lower()
    _domain = _domain.removeprefix("www.")
    if _domain:
        Config.DOMAIN_TO_CHANNEL_URL.setdefault(_domain, _raw_url)

# Chuẩn hóa account ở cấp channel theo danh sách canonical của domain.
# Như vậy mọi đường xử lý (ingress, queue, browser worker) đều dùng cùng
# một danh sách, không còn tình trạng channel giữ account cũ khác domain map.
_config_logger = logging.getLogger(__name__)
for _chat_id, _site in Config.CHANNEL_CONFIG.items():
    _domain = (_site.get("url", "").split("//", 1)[-1].split("/", 1)[0]).lower()
    _canonical = Config.DOMAIN_ACCOUNT_OVERRIDES.get(_domain)
    if _canonical:
        _old_accounts = _site.get("accounts", [])
        _new_accounts = [dict(_account) for _account in _canonical]
        if _old_accounts != _new_accounts:
            _config_logger.debug(
                "Domain account override applied: chat=%s domain=%s old=%s new=%s",
                _chat_id,
                _domain,
                [a.get("username") for a in _old_accounts],
                [a.get("username") for a in _new_accounts],
            )
        _site["accounts"] = _new_accounts

# Chốt chặn kiểm tra lỗi cấu hình lúc khởi động
for chat_id, site in Config.CHANNEL_CONFIG.items():
    group = site.get("filter_group")
    if group and group not in Config.CODE_FILTER_GROUPS:
        raise ValueError(
            f"LỖI CẤU HÌNH: filter_group '{group}' (site '{site.get('name', 'Unknown')}') "
            f"chưa được định nghĩa trong CODE_FILTER_GROUPS!"
        )
