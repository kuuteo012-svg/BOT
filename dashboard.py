"""
📊 DASHBOARD HIỂN THỊ KẾT QUẢ SĂN CODE (RICH LIVE TABLE)
Kiểu "Poller-CODE": bảng live cập nhật mỗi khi có kết quả submit mới.

Bố cục:
  Dòng 1: ⚡ RTT Telegram | 🚀 Bắn N nick đồng thời | 📡 Browser RTT TB
  Dòng 2: 📊 Thống kê Thành công / Thất bại (Tổng lượt)
  Bảng:   #, Trang, Tài Khoản, Giftcode, Trạng Thái, Browser ms, Phản Hồi RAW

⚠️ QUAN TRỌNG — TRÁNH VỠ MÀN HÌNH:
rich.Live() chiếm và tự vẽ lại MỘT VÙNG CỐ ĐỊNH của terminal nhiều lần/giây.
Bot hiện tại (logger_setup.py) đang có 1 console_handler ghi log THẲNG ra
stdout song song — nếu cả 2 cùng ghi ra terminal một lúc, bảng sẽ nhấp
nháy/vỡ dòng liên tục (đã xác nhận đây là xung đột kỹ thuật thật, không
phải lý thuyết). Gọi disable_console_logging() TRƯỚC start_dashboard() để
tắt phần in log ra MÀN HÌNH — log vẫn được ghi ĐẦY ĐỦ vào
logs/bot_activity.log như bình thường, không mất dữ liệu gì cả.
"""

import logging
import threading
from collections import deque
from typing import Optional

from rich.console import Console, Group
from rich.live import Live
from rich.table import Table
from rich.panel import Panel
from rich.text import Text

# Không giới hạn lịch sử trong phiên chạy: dashboard giữ toàn bộ mã đã submit.
# Rich vẫn chỉ render theo nhịp refresh; log file là nơi lưu trữ lâu dài.
MAX_ROWS = None

# Throttle vẽ lại bảng: _refresh() chỉ đánh dấu "dirty" (rẻ, không I/O),
# 1 thread nền riêng (_flush_loop) mới thực sự gọi _live.update() theo
# đúng nhịp REFRESH_INTERVAL — dồn nhiều lượt cập nhật dồn dập thành 1 lần vẽ.
REFRESH_INTERVAL = 1.0 / 8  # khớp refresh_per_second=8 khi start Live
_dirty = False
_flush_thread: Optional[threading.Thread] = None
_flush_stop = threading.Event()

_lock = threading.Lock()
_rows: deque = deque(maxlen=MAX_ROWS)
_stats = {
    "success": 0,
    "failed": 0,
    "total": 0,
    "last_telegram_rtt_ms": None,
    "last_concurrent": 0,
    "last_concurrent_elapsed_ms": None,
    "server_rtts_ms": deque(maxlen=200),  # browser end-to-end duration (moving average)
    "timing_records": 0,
    "download_active": 0,
    "download_completed": 0,
    "download_dedup": 0,
    "last_download_ms": None,
    "last_download_mb_s": None,
    "last_download_size_mb": None,
    "last_download_attempts": None,
}
_live: Optional[Live] = None
_console: Optional[Console] = None
_row_id = 0


def disable_console_logging():
    """Tắt log ra STDOUT — vẫn ghi đầy đủ vào file logs/bot_activity.log.
    BẮT BUỘC gọi hàm này trước start_dashboard(), nếu không log của bot sẽ
    ghi đè/xen kẽ vào bảng rich đang vẽ, làm vỡ hình hiển thị.
    ✅ Log giờ chạy qua QueueListener nền (logger_setup.py) — handler
    console thật không còn nằm trong logger.handlers nữa, nên phải gỡ qua
    logger_setup.remove_console_handler()."""
    from logger_setup import remove_console_handler
    remove_console_handler()


def _fmt_ms(ms) -> str:
    if ms is None:
        return "-"
    try:
        return f"{float(ms):.0f}ms"
    except Exception:
        return "-"


def _status_style(status: str):
    upper = (status or "").upper()
    if "THÀNH CÔNG" in upper or "SUCCESS" in upper:
        return "bold green", f"✓ {status}"
    if any(k in upper for k in ("THẤT BẠI", "FAILED", "SAI", "HẾT HẠN", "LỖI")):
        return "bold red", f"✗ {status}"
    return "yellow", f"? {status}"


def _build_renderable():
    with _lock:
        stats = dict(_stats)
        stats["server_rtts_ms"] = list(_stats["server_rtts_ms"])
        rows_snapshot = list(_rows)

    server_rtts = stats["server_rtts_ms"]
    avg_server_rtt = (sum(server_rtts) / len(server_rtts)) if server_rtts else None

    header1 = Text()
    header1.append("⚡ RTT Telegram: ", style="bold")
    header1.append(_fmt_ms(stats["last_telegram_rtt_ms"]), style="cyan")
    header1.append("   |   ", style="dim")
    header1.append("🚀 Bắn ", style="bold")
    header1.append(f"{stats['last_concurrent']}", style="bold yellow")
    header1.append(" nick đồng thời: ", style="bold")
    header1.append(_fmt_ms(stats["last_concurrent_elapsed_ms"]), style="cyan")
    header1.append("   |   ", style="dim")
    header1.append("📡 Browser RTT TB: ", style="bold")
    header1.append(_fmt_ms(avg_server_rtt), style="magenta")

    header2 = Text()
    header2.append("📊 Thống kê: ", style="bold")
    header2.append(f"{stats['success']} Thành công", style="bold green")
    header2.append(" / ", style="dim")
    header2.append(f"{stats['failed']} Thất bại", style="bold red")
    header2.append(f"   (Tổng {stats['total']} lượt submit)", style="dim")

    header3 = Text()
    header3.append("⬇ Download: ", style="bold")
    header3.append(f"{stats['download_completed']} xong", style="green")
    header3.append(f" / {stats['download_dedup']} dedup", style="yellow")
    if stats["last_download_mb_s"] is not None:
        header3.append(
            f" | gần nhất {stats['last_download_mb_s']:.2f} MB/s, "
            f"{stats['last_download_size_mb']:.2f} MB, {stats['last_download_ms']:.0f}ms",
            style="cyan",
        )
    header3.append(f" | đang tải={stats['download_active']}", style="bold yellow")
    header3.append(f" | timing={stats['timing_records']}", style="dim")

    table = Table(show_header=True, header_style="bold white on blue", expand=True)
    table.add_column("#", justify="right", style="dim", width=4)
    table.add_column("Trang", style="cyan", width=10)
    table.add_column("Tài Khoản", style="cyan")
    table.add_column("Giftcode", style="magenta")
    table.add_column("Trạng Thái", justify="center")
    table.add_column("Browser ms", justify="right", width=10)
    table.add_column("Phản Hồi RAW", style="white", overflow="fold", ratio=3)

    for row in rows_snapshot:
        style, display = _status_style(row["status"])
        table.add_row(
            str(row["id"]),
            row["domain"],
            row["account"],
            row["code"],
            f"[{style}]{display}[/{style}]",
            row["rtt"],
            row["raw"],
        )

    header_panel = Panel.fit(
        Group(header1, header2, header3), border_style="dim", title="🎯 OCR Hunter"
    )
    return Group(header_panel, table)


def _flush_loop():
    """Thread nền DUY NHẤT thực sự gọi _live.update() — chạy đúng nhịp
    REFRESH_INTERVAL, chỉ vẽ lại khi có gì mới (_dirty=True) kể từ lần vẽ
    trước. Đây là nơi GOM nhiều lượt update_dashboard()/report_*() gọi dồn
    dập (vd nhiều domain submit gần như đồng thời) thành đúng 1 lần build +
    vẽ renderable mỗi REFRESH_INTERVAL giây, thay vì build lại mỗi lần gọi."""
    global _dirty
    while not _flush_stop.wait(REFRESH_INTERVAL):
        if _live is None:
            continue
        with _lock:
            should_flush = _dirty
            _dirty = False
        if not should_flush:
            continue
        try:
            _live.update(_build_renderable(), refresh=True)
        except Exception:
            pass


def start_dashboard():
    """Khởi động bảng live. Gọi 1 lần lúc bot start (sau khi đã gọi
    disable_console_logging())."""
    global _live, _console, _flush_thread
    if _live is not None:
        return
    _console = Console()
    _live = Live(
        _build_renderable(), console=_console, auto_refresh=False, screen=False
    )
    _live.start(refresh=True)

    _flush_stop.clear()
    _flush_thread = threading.Thread(target=_flush_loop, name="dashboard-flush", daemon=True)
    _flush_thread.start()


def stop_dashboard():
    """Dừng bảng live — gọi khi bot tắt (trong khối finally của main())."""
    global _live, _flush_thread
    _flush_stop.set()
    if _flush_thread is not None:
        try:
            _flush_thread.join(timeout=1.0)
        except Exception:
            pass
        _flush_thread = None
    if _live:
        try:
            # Vẽ lần cuối để không mất kết quả vừa ghi nhận ngay trước khi tắt
            _live.update(_build_renderable(), refresh=True)
            _live.stop()
        except Exception:
            pass
        _live = None


def _refresh():
    """Chỉ đánh dấu 'có dữ liệu mới' — KHÔNG tự vẽ ngay. _flush_loop() (thread
    nền, xem trên) sẽ vẽ gộp theo nhịp REFRESH_INTERVAL. An toàn gọi từ
    nhiều luồng vì chỉ set 1 cờ bool dưới _lock."""
    global _dirty
    if _live is None:
        return
    with _lock:
        _dirty = True


def update_dashboard(
    domain: str,
    account: str,
    code: str,
    status: str,
    rtt_ms: Optional[float] = None,
    raw_response: str = "",
    telegram_rtt_ms: Optional[float] = None,
):
    """Ghi 1 dòng kết quả submit mới. Gọi ngay sau khi submit_code_safe() có
    kết quả (SUCCESS / FAILED / UNKNOWN)."""
    global _row_id
    if _live is None:
        return

    raw = (raw_response or "").strip()
    if len(raw) > 120:
        raw = raw[:120] + "..."

    def _bucket(value: str) -> str:
        upper = (value or "").upper()
        if "THÀNH CÔNG" in upper or "SUCCESS" in upper:
            return "success"
        if "THẤT BẠI" in upper or "FAILED" in upper:
            return "failed"
        return "other"

    with _lock:
        existing = next(
            (
                row for row in _rows
                if row["domain"] == domain
                and row["account"] == account
                and row["code"] == code
            ),
            None,
        )
        if existing is None:
            _row_id += 1
            _rows.append(
                {
                    "id": _row_id,
                    "domain": domain,
                    "account": account,
                    "code": code,
                    "status": status,
                    "rtt": _fmt_ms(rtt_ms),
                    "raw": raw or "-",
                }
            )
            _stats["total"] += 1
            old_bucket = "other"
        else:
            old_bucket = _bucket(existing.get("status", ""))
            existing.update(
                status=status,
                rtt=_fmt_ms(rtt_ms),
                raw=raw or "-",
            )

        new_bucket = _bucket(status)
        if old_bucket in ("success", "failed"):
            _stats[old_bucket] = max(0, _stats[old_bucket] - 1)
        if new_bucket in ("success", "failed"):
            _stats[new_bucket] += 1

        if rtt_ms is not None:
            _stats["server_rtts_ms"].append(rtt_ms)
        if telegram_rtt_ms is not None:
            _stats["last_telegram_rtt_ms"] = telegram_rtt_ms

    _refresh()


def report_download_started():
    with _lock:
        _stats["download_active"] += 1
    _refresh()


def report_download_finished():
    with _lock:
        _stats["download_active"] = max(0, _stats["download_active"] - 1)
    _refresh()


def report_timing_record(record: dict):
    """Update realtime timing/download counters from a RequestTimer record."""
    with _lock:
        _stats["timing_records"] += 1
        if record.get("media"):
            _stats["download_completed"] += 1
            if record.get("download_dedup_hit"):
                _stats["download_dedup"] += 1
            elapsed = record.get("download_elapsed_ms")
            size = record.get("file_size_bytes")
            speed = record.get("download_bytes_per_sec")
            _stats["last_download_ms"] = float(elapsed) if elapsed is not None else None
            _stats["last_download_mb_s"] = float(speed) / 1_000_000 if speed is not None else None
            _stats["last_download_size_mb"] = float(size) / 1_000_000 if size is not None else None
            _stats["last_download_attempts"] = record.get("download_attempts")
    _refresh()


def get_dashboard_snapshot() -> dict:
    """✅ MỚI: trả về bản sao gọn các số liệu hiện tại — dùng cho lệnh admin
    bất đồng bộ (vd /status trong features.py) mà không cần đụng vào biến
    private _stats/_rows trực tiếp từ module khác."""
    with _lock:
        return {
            "success": _stats["success"],
            "failed": _stats["failed"],
            "total": _stats["total"],
            "download_active": _stats["download_active"],
            "download_completed": _stats["download_completed"],
            "rows_tracked": len(_rows),
        }


def report_batch_submit(concurrent_count: int, elapsed_ms: float):
    """Gọi khi có N tài khoản được bắn SONG SONG cùng lúc (vd MM88 parallel,
    hoặc nhiều code OCR submit đồng thời) — cập nhật dòng '🚀 Bắn N nick'."""
    if _live is None:
        return
    with _lock:
        _stats["last_concurrent"] = concurrent_count
        _stats["last_concurrent_elapsed_ms"] = elapsed_ms
    _refresh()
