from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional

from dashboard import update_dashboard
from logger_setup import logger
# take_result_screenshot dùng bản dùng chung trong media_helpers.py (module
# trung lập, không phụ thuộc main_script.py — tránh import vòng).
from media_helpers import persist_result_screenshot

SUCCESS_KW = [
    "THÀNH CÔNG", "THANH CONG", "SUCCESS", "COMPLETED",
    "ĐÃ NHẬN", "DA NHAN", "RECEIVED", "ADDED", "AWARDED",
    "CONGRATULATIONS", "APPROVED", "ACCEPTED",
]

FAILED_KW = [
    "SAI", "LỖI", "LOI",
    "ĐÃ SỬ", "DA SU", "ĐÃ DÙNG",
    "FAILED", "ERROR", "INVALID",
    "KHÔNG ĐÚNG", "KHÔNG TỒN TẠI", "KHÔNG HỢP LỆ",
    "HẾT HẠN", "ĐÃ HẾT", "EXPIRED",
    "CODE ĐÃ SỬ DỤNG HẾT", "CODE DA SU DUNG HET",
    "CODE ĐÃ HẾT HẠN", "CODE DA HET HAN",
    "MÃ ĐÃ ĐƯỢC SỬ DỤNG", "MA DA DUOC SU DUNG",
    "MÃ ĐÃ HẾT HẠN", "MA DA HET HAN",
    "CODE USED UP", "CODE HAS EXPIRED",
    "NOT FOUND", "NOT EXIST", "KHÔNG TÌM THẤY",
    "CODE NOT USED", "CODE_NOT_USED",
    "THAT BAI", "THẤT BẠI",
    "REJECTED", "DECLINED",
]

TOO_MANY_KW = [
    "TOO MANY",
    "RATE LIMIT",
    "QUÁ NHIỀU",
    "429",
    "THÊM SAU",
    "THỬ LẠI SAU",
]

NEGATIVE_SUCCESS_KW = (
    "NOT ACCEPTED", "NOT ADDED", "NOT APPROVED", "UNSUCCESSFUL",
    "KHÔNG THÀNH CÔNG", "KHONG THANH CONG",
)

POINT_KW = ["ĐIỂM", "XU", "COIN", "POINT"]

_SHORT_KW = {"SAI", "LOI", "LỖI", "XU", "ĐIỂM", "DIEM"}
_SHORT_KW_PATTERNS = {
    kw: re.compile(rf"(?:^|[^\w]){re.escape(kw)}(?:[^\w]|$)")
    for kw in _SHORT_KW
}


def _kw_matches(text_upper: str, keyword: str) -> bool:
    pattern = _SHORT_KW_PATTERNS.get(keyword)
    if pattern is not None:
        return bool(pattern.search(text_upper))
    return keyword in text_upper


class ResultStatus(str, Enum):
    SUCCESS_POINTS = "SUCCESS_POINTS"
    SUCCESS_NO_POINTS = "SUCCESS_NO_POINTS"
    FAILED = "FAILED"
    AMBIGUOUS = "AMBIGUOUS"
    NO_RESULT = "NO_RESULT"
    RATE_LIMITED = "RATE_LIMITED"


@dataclass(frozen=True)
class Outcome:
    status: ResultStatus
    result: dict
    postprocess: dict | None = None


def classify_result(raw_text: str) -> ResultStatus:
    text = raw_text or ""
    stripped = text.strip()
    upper = text.upper()

    is_rate_limited = any(kw in upper for kw in TOO_MANY_KW)
    if is_rate_limited:
        return ResultStatus.RATE_LIMITED

    if any(marker in upper for marker in NEGATIVE_SUCCESS_KW):
        return ResultStatus.FAILED

    is_success = any(_kw_matches(upper, kw) for kw in SUCCESS_KW)
    is_failed = any(_kw_matches(upper, kw) for kw in FAILED_KW)

    if is_success and not is_failed:
        has_points = any(_kw_matches(upper, kw) for kw in POINT_KW)
        return ResultStatus.SUCCESS_POINTS if has_points else ResultStatus.SUCCESS_NO_POINTS

    if len(stripped) < 3:
        return ResultStatus.NO_RESULT

    if is_failed:
        return ResultStatus.FAILED

    return ResultStatus.AMBIGUOUS


async def _build_no_result_debug_info(
    page, code: str, user: str, domain: str, clicked: bool,
    pre_click_text: str, result_timeout_s: float, is_hi88: bool,
) -> dict:
    """Debug info CHI TIẾT (snippet trang + capture MutationObserver HI88)
    — chỉ dùng khi caller KHÔNG tự truyền sẵn debug_info vào record_outcome
    (xem nhánh NO_RESULT bên dưới). main_script.py hiện tự build 1 bản
    debug_info đơn giản hơn và truyền thẳng vào — nếu có, dùng bản đó,
    không gọi lại hàm này (tránh tính 2 lần, tốn round-trip page.evaluate)."""
    debug_info: dict[str, Any] = {
        "code": code,
        "user": user,
        "domain": domain,
        "clicked_submit_button": clicked,
        "pre_click_text_len": len(pre_click_text or ""),
        "result_timeout_s": result_timeout_s,
        "page_url": None,
        "post_click_text_snippet": "",
    }
    try:
        debug_info["page_url"] = page.url
    except Exception:
        pass
    try:
        post_text = await page.evaluate("() => document.body.innerText || ''")
        debug_info["post_click_text_snippet"] = (post_text or "")[:800]
    except Exception:
        pass
    if is_hi88:
        try:
            debug_info["hi88_watcher_captures"] = await page.evaluate(
                "() => window.__hi88Captures || []"
            )
        except Exception:
            debug_info["hi88_watcher_captures"] = []
    return debug_info


def _safe_append_history(callbacks: Optional[dict], **kwargs) -> None:
    """Gọi callbacks['append_history'] nếu có — đây là hàm SYNC
    (append_code_history trong main_script.py không phải coroutine, chỉ
    put_nowait vào queue hoặc ghi file), nên KHÔNG await ở đây. An toàn bỏ
    qua nếu không có callback hoặc callback tự ném lỗi — ghi lịch sử không
    được phép làm sập luồng submit chính."""
    if not callbacks:
        return
    fn = callbacks.get("append_history")
    if not fn:
        return
    try:
        fn(**kwargs)
    except Exception as e:
        logger.debug(f"⚠️ append_history callback error: {e}")


async def record_outcome(
    *,
    systems: dict,
    page,
    user: str,
    code: str,
    target_url: str,
    domain: str,
    elapsed: float,
    raw_text: str = "",
    result_text: str = "",  # alias của raw_text — main_script.py gọi bằng tên này
    status: "ResultStatus | str | None" = None,  # nếu có sẵn thì dùng luôn, không tính lại
    key: str | None = None,  # main_script.py truyền "domain|user"
    # callbacks: dict các hàm (append_history, clean_page, reset_page...) —
    # dùng callback thay vì import ngược main_script.py để tránh import vòng.
    callbacks: Optional[dict] = None,
    debug_info: Optional[dict] = None,  # nếu main_script.py build sẵn thì dùng luôn
    clicked: bool = True,
    pre_click_text: str = "",
    result_timeout_s: float = 0.0,
    is_hi88: bool = False,
    capture_screenshot_fn=None,
    **_ignored_kwargs: Any,  # nuốt tham số lạ phát sinh sau này thay vì crash
) -> Outcome:
    final_raw_text = raw_text or result_text or ""
    key = key or f"{domain}|{user}"

    if status is None:
        status = classify_result(final_raw_text)
    elif not isinstance(status, ResultStatus):
        try:
            status = ResultStatus(status)
        except ValueError:
            # Chuỗi lạ không khớp enum nào (vd lỗi gõ tay) → tự phân loại
            # lại từ text cho an toàn, không để crash vì ValueError.
            logger.debug(f"⚠️ [{key}] status lạ '{status}' — tự phân loại lại từ raw_text")
            status = classify_result(final_raw_text)

    result_text = final_raw_text
    capture = None
    if status in (ResultStatus.NO_RESULT, ResultStatus.FAILED, ResultStatus.AMBIGUOUS):
        info = debug_info
        if status == ResultStatus.NO_RESULT and info is None:
            info = await _build_no_result_debug_info(
                page, code, user, domain, clicked, pre_click_text, result_timeout_s, is_hi88,
            )
            debug_info = info
        if capture_screenshot_fn is not None:
            try:
                capture = await capture_screenshot_fn(page)
            except Exception as e:
                logger.debug(f"⚠️ [{key}] screenshot capture lỗi (bỏ qua): {e}")

    if status == ResultStatus.RATE_LIMITED:
        result = {"success": False, "message": f"RateLimit:{result_text[:60]}", "rate_limited": True}
    elif status in (ResultStatus.SUCCESS_POINTS, ResultStatus.SUCCESS_NO_POINTS):
        result = {
            "success": True,
            "has_points": status == ResultStatus.SUCCESS_POINTS,
            "message": result_text[:100],
        }
    elif status == ResultStatus.NO_RESULT:
        result = {"success": False, "message": "No popup"}
    elif status == ResultStatus.FAILED:
        result = {"success": False, "message": result_text[:100], "is_wrong_code": True}
    else:
        status = ResultStatus.AMBIGUOUS
        result = {"success": False, "message": result_text[:100], "is_wrong_code": False}

    postprocess = {
        "status": status,
        "result_text": result_text,
        "elapsed": elapsed,
        "user": user,
        "code": code,
        "target_url": target_url,
        "domain": domain,
        "key": key,
        "screenshot_capture": capture,
        "debug_info": debug_info,
        "callbacks": callbacks or {},
    }

    # Page cleanup must stay serialized with the tab/account locks: unlike DB,
    # history, and file persistence, it mutates the shared live page.
    clean_page_fn = (callbacks or {}).get("clean_page")
    if clean_page_fn is not None:
        try:
            await clean_page_fn(page, domain, target_url, key)
        except Exception as e:
            logger.debug(f"⚠️ [{key}] clean_page callback lỗi (bỏ qua, không ảnh hưởng kết quả): {e}")

    return Outcome(status=status, result=result, postprocess=postprocess)


async def finalize_outcome(postprocess: dict, systems: dict) -> str | None:
    """Persist a captured result after browser/account/submit locks are released."""
    status = postprocess["status"]
    result_text = postprocess.get("result_text") or ""
    elapsed = float(postprocess.get("elapsed") or 0.0)
    user = postprocess["user"]
    code = postprocess["code"]
    target_url = postprocess["target_url"]
    domain = postprocess["domain"]
    callbacks = postprocess.get("callbacks") or {}
    debug_info = postprocess.get("debug_info")
    key = postprocess.get("key") or f"{domain}|{user}"
    db = systems["db"]
    perf_mon = systems["performance_monitor"]
    # ✅ FIX: dùng đúng pool _DB_EXECUTOR (main_script.py) cho các lệnh ghi
    # DB bên dưới, thay vì default executor (None) — tránh tranh chấp pool
    # mặc định của event loop với các run_in_executor(None, ...) khác.
    # Fallback về None nếu vì lý do gì đó systems không có sẵn (vd gọi từ
    # test) để không crash.
    db_executor = systems.get("db_executor")
    screenshot = None

    if postprocess.get("screenshot_capture") is not None:
        screenshot = await asyncio.to_thread(
            persist_result_screenshot,
            postprocess["screenshot_capture"], user, code, target_url,
            "UNKNOWN" if status in (ResultStatus.NO_RESULT, ResultStatus.AMBIGUOUS) else status.value,
            debug_info,
        )

    if status == ResultStatus.RATE_LIMITED:
        logger.warning(f"🚫 [{user}|{domain}] Too Many Requests — deferred to retry/queue policy")
        return

    if status in (ResultStatus.SUCCESS_POINTS, ResultStatus.SUCCESS_NO_POINTS):
        logger.info(f"✅ [{user}] SUCCESS ({elapsed:.2f}s) — {result_text[:60]}")
        update_dashboard(
            domain=domain, account=user, code=code, status="THÀNH CÔNG",
            rtt_ms=elapsed * 1000, raw_response=result_text,
        )
        _safe_append_history(
            callbacks, event_type="RESULT", code=code, target_url=target_url,
            account=user, status="SUCCESS", submit_elapsed=elapsed, message=result_text[:100],
        )
        await asyncio.get_running_loop().run_in_executor(
            db_executor, db.record_submission, code, user, target_url, "SUCCESS", result_text[:100],
        )
        perf_mon.record_task("submit_code", elapsed, True)
        return

    if status == ResultStatus.NO_RESULT:
        logger.warning(f"⚠️ [{user}] NO RESULT after {elapsed:.2f}s")
        dashboard_status, history_status, db_status, message = "UNKNOWN", "UNKNOWN", "UNKNOWN", "No popup"
    elif status == ResultStatus.FAILED:
        logger.warning(f"❌ [{user}] FAILED ({elapsed:.2f}s) — {result_text[:60]}")
        dashboard_status, history_status, db_status, message = "THẤT BẠI", "FAILED", "FAILED", result_text[:100]
    else:
        logger.warning(
            f"❓ [{user}] Kết quả MƠ HỒ ({elapsed:.2f}s), không rõ đúng/sai — "
            f"KHÔNG huỷ code, để retry: {result_text[:80]}"
        )
        dashboard_status, history_status, db_status, message = "UNKNOWN", "AMBIGUOUS", "UNKNOWN", result_text[:100]

    update_dashboard(
        domain=domain, account=user, code=code, status=dashboard_status,
        rtt_ms=elapsed * 1000, raw_response="No popup" if status == ResultStatus.NO_RESULT else result_text,
    )
    history_kwargs = {
        "event_type": "RESULT", "code": code, "target_url": target_url,
        "account": user, "status": history_status, "submit_elapsed": elapsed,
        "message": message,
    }
    if screenshot is not None:
        history_kwargs["screenshot"] = screenshot
    _safe_append_history(callbacks, **history_kwargs)
    await asyncio.get_running_loop().run_in_executor(
        db_executor, db.record_submission, code, user, target_url, db_status, message,
    )
    perf_mon.record_task("submit_code", elapsed, False)
