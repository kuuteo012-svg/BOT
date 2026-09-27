"""Bounded and deduplicated Telegram media downloads."""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Union

from logger_setup import logger


@dataclass
class DownloadMetrics:
    path: Optional[str]
    size_bytes: Optional[int]
    elapsed_ms: float
    bytes_per_sec: Optional[float]
    attempts: int
    dedup_hit: bool


class MediaDownloadManager:
    """Limit concurrent downloads and coalesce duplicate in-flight requests.

    ✅ QUAN TRỌNG: dedup chỉ áp dụng cho ĐÚNG 1 TIN NHẮN CỤ THỂ (khoá =
    chat_id + message_id + media_id, xem key_for()) — KHÔNG dedup rộng theo
    chỉ mỗi media_id. Nếu 2 kênh khác nhau đăng trùng cùng 1 ảnh/video gần
    như đồng thời (rất hay gặp với các kênh QQ88/Hi88 hay đăng lại banner
    giống nhau), mỗi kênh vẫn phải được tải/OCR riêng — dedup theo nội dung
    file sẽ khiến kênh thứ 2 bị âm thầm BỎ QUA OCR dù là tin nhắn khác hẳn.
    """

    def __init__(
        self,
        max_concurrent: int = 2,
        retries: int = 1,
        retry_delay: float = 0.5,
        max_size_bytes: Optional[int] = None,
        fast_download: bool = False,
        fast_min_bytes: int = 4 * 1024 * 1024,
        fast_workers: int = 4,
        fast_chunk_kb: int = 512,
        fast_max_connections: int = 8,
    ):
        self.semaphore = asyncio.Semaphore(max(1, int(max_concurrent)))
        self.retries = max(0, int(retries))
        self.retry_delay = max(0.0, float(retry_delay))
        # ✅ Giới hạn dung lượng tải tối đa — chặn tải về những file bất
        # thường lớn (video dài quá mức, ảnh độ phân giải khủng do đăng
        # nhầm) vốn không phải giftcode thật. None = không giới hạn.
        self.max_size_bytes = int(max_size_bytes) if max_size_bytes else None
        self.fast_download = bool(fast_download)
        self.fast_min_bytes = max(1024 * 1024, int(fast_min_bytes))
        self.fast_workers = max(2, min(8, int(fast_workers)))
        self.fast_chunk_size = max(128, min(2048, int(fast_chunk_kb))) * 1024
        self.fast_connections = asyncio.Semaphore(max(1, min(16, int(fast_max_connections))))
        self._inflight: dict[tuple[Any, Any, Any], asyncio.Task] = {}
        self._lock = asyncio.Lock()

    @staticmethod
    def key_for(event: Any) -> tuple[Any, Any, Any]:
        """Khoá dedup theo (chat_id, message_id, media_id) — xem docstring
        của class ở trên về lý do KHÔNG được rút gọn khoá này."""
        message = getattr(event, "message", None)
        media = getattr(message, "media", None)
        media_id = None
        try:
            if getattr(media, "document", None) is not None:
                media_id = getattr(media.document, "id", None)
            elif getattr(media, "photo", None) is not None:
                media_id = getattr(media.photo, "id", None)
        except Exception:
            media_id = None
        return (
            getattr(event, "chat_id", None),
            getattr(message, "id", None),
            media_id,
        )

    async def download(self, event: Any, output_dir: str | Path) -> DownloadMetrics:
        """Tải media từ event. Nếu đã có 1 lượt tải ĐANG CHẠY cho đúng tin
        nhắn này (dedup theo key_for), dùng chung kết quả thay vì tải lại."""
        key = self.key_for(event)
        async with self._lock:
            current = self._inflight.get(key)
            if current is None or current.done():
                task = asyncio.create_task(self._download_once(event, output_dir))
                self._inflight[key] = task
                task.add_done_callback(
                    lambda completed, _key=key, _task=task: asyncio.create_task(
                        self._remove_inflight_when_done(_key, _task)
                    )
                )
                dedup_hit = False
            else:
                task = current
                dedup_hit = True

        try:
            try:
                # shield: nếu caller bị cancel (vd timeout xử lý tin nhắn),
                # lượt tải thật vẫn tiếp tục chạy tới cùng cho các caller
                # dedup khác đang chờ chung, không bị huỷ giữa chừng.
                result = await asyncio.wait_for(asyncio.shield(task), timeout=300.0)
            except asyncio.TimeoutError:
                logger.error(f"❌ [Download] Timeout 300s chờ tải xong (key={key})")
                result = DownloadMetrics(
                    path=None, size_bytes=None, elapsed_ms=300_000.0,
                    bytes_per_sec=None, attempts=0, dedup_hit=dedup_hit,
                )

            if dedup_hit:
                # ✅ FIX: KHÔNG mutate object 'result' dùng chung giữa nhiều
                # coroutine (race condition) — luôn tạo bản sao riêng cho
                # lượt dedup-hit này.
                result = DownloadMetrics(
                    path=result.path,
                    size_bytes=result.size_bytes,
                    elapsed_ms=result.elapsed_ms,
                    bytes_per_sec=result.bytes_per_sec,
                    attempts=result.attempts,
                    dedup_hit=True,
                )
            return result
        finally:
            if not dedup_hit and task.done():
                async with self._lock:
                    if self._inflight.get(key) is task:
                        self._inflight.pop(key, None)

    async def _remove_inflight_when_done(self, key, task) -> None:
        async with self._lock:
            if self._inflight.get(key) is task:
                self._inflight.pop(key, None)

    async def _download_once(self, event: Any, output_dir: str | Path) -> DownloadMetrics:
        started = time.perf_counter()
        attempts = 0
        path = None

        async with self.semaphore:
            for attempts in range(1, self.retries + 2):
                try:
                    path = await self._download_with_size_check(event, output_dir)
                    if path:
                        break
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    if attempts > self.retries:
                        logger.error(f"❌ [Download] Thất bại sau {attempts} lần thử: {e}")
                    else:
                        wait_s = self.retry_delay * attempts
                        logger.warning(
                            f"⚠️ [Download] Lần {attempts} lỗi ({e}) → retry sau {wait_s:.1f}s"
                        )
                        await asyncio.sleep(wait_s)

        elapsed = max(time.perf_counter() - started, 1e-9)
        size_bytes = None
        if path:
            try:
                size_bytes = Path(path).stat().st_size
            except OSError:
                pass

        bytes_per_sec = round(size_bytes / elapsed, 2) if size_bytes is not None else None

        if path and size_bytes is not None:
            logger.debug(
                f"✅ [Download] {Path(path).name} — {size_bytes / 1024:.1f}KB, "
                f"{elapsed * 1000:.0f}ms"
                + (f", {bytes_per_sec / 1_000_000:.2f}MB/s" if bytes_per_sec else "")
                + f" (lần {attempts})"
            )

        return DownloadMetrics(
            path=str(path) if path else None,
            size_bytes=size_bytes,
            elapsed_ms=round(elapsed * 1000, 2),
            bytes_per_sec=bytes_per_sec,
            attempts=attempts,
            dedup_hit=False,
        )

    async def _download_with_size_check(self, event: Any, output_dir: str | Path) -> Optional[str]:
        """Tải 1 lượt — kiểm tra dung lượng TRƯỚC khi tải (nếu Telegram báo
        trước size qua document.size) để không tốn băng thông cho file rõ
        ràng quá khổ, và kiểm tra LẠI sau khi tải xong (phòng khi size khai
        báo trước không chính xác) — xoá ngay file tạm nếu vượt giới hạn.

        Dùng event/message downloader nếu có; durable replay có thể bọc
        Telethon Message trong SimpleNamespace nên fallback qua TelegramClient.
        """
        message = getattr(event, "message", None)
        media = getattr(message, "media", None) if message else None

        declared_size = None
        try:
            if getattr(media, "document", None) is not None:
                declared_size = getattr(media.document, "size", None)
        except Exception:
            declared_size = None

        if self.max_size_bytes and declared_size and declared_size > self.max_size_bytes:
            raise ValueError(
                f"File quá khổ (khai báo trước): {declared_size / 1_000_000:.1f}MB > "
                f"{self.max_size_bytes / 1_000_000:.1f}MB giới hạn"
            )

        Path(output_dir).mkdir(parents=True, exist_ok=True)
        telegram_client = None
        if self.fast_download and declared_size and declared_size >= self.fast_min_bytes:
            try:
                # Deferred import avoids a module cycle during main_script startup.
                import main_script as _ms
                telegram_client = getattr(_ms, "client", None)
                connected = True
                is_connected = getattr(telegram_client, "is_connected", None)
                if callable(is_connected):
                    connected = bool(is_connected())
                if (
                    telegram_client is not None
                    and connected
                    and getattr(media, "document", None) is not None
                ):
                    fast_path = await self._multipart_download(
                        telegram_client, message, output_dir, declared_size
                    )
                    if fast_path:
                        return fast_path
                    logger.debug("⚠️ [Download] Multipart không trả file, fallback Telethon")
                elif telegram_client is not None and not connected:
                    logger.debug("⏭️ [Download] Telegram đang reconnect — dùng downloader chuẩn")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug("⚠️ [Download] Multipart lỗi, fallback Telethon: %s", exc)
        downloader = getattr(event, "download_media", None)
        if downloader is None and message is not None:
            downloader = getattr(message, "download_media", None)
        if downloader is not None:
            path = await downloader(file=str(output_dir))
        else:
            # Deferred import avoids a module cycle during main_script startup.
            import main_script as _ms
            telegram_client = telegram_client or getattr(_ms, "client", None)
            if telegram_client is None or message is None:
                return None
            path = await telegram_client.download_media(message, file=str(output_dir))

        if not path:
            return None

        if self.max_size_bytes:
            try:
                actual_size = Path(path).stat().st_size
            except OSError:
                actual_size = None
            if actual_size is not None and actual_size > self.max_size_bytes:
                try:
                    Path(path).unlink(missing_ok=True)
                except Exception:
                    pass
                raise ValueError(
                    f"File tải về quá khổ: {actual_size / 1_000_000:.1f}MB > "
                    f"{self.max_size_bytes / 1_000_000:.1f}MB giới hạn"
                )

        return path

    async def _multipart_download(
        self, client: Any, message: Any, output_dir: str | Path, file_size: int
    ) -> Optional[str]:
        """Download a large Telegram document through interleaved chunks.

        This follows Telethon's public ``iter_download`` API. Each worker owns
        every Nth chunk, so network requests overlap while writes are placed at
        their original offsets in a temporary file. The method is deliberately
        opt-in and returns None on any integrity/compatibility failure so the
        caller can use Telethon's standard downloader.
        """
        file_info = getattr(message, "file", None)
        name = getattr(file_info, "name", None) or ""
        if not name:
            ext = getattr(file_info, "ext", None) or ""
            name = f"telegram_{getattr(message, 'id', 'media')}{ext}"
        final_path = Path(output_dir) / Path(name).name
        temp_path = final_path.with_name(final_path.name + ".multipart.part")
        # Telegram's public iter_download API clamps request_size to its
        # supported range; 512 KiB is the useful upper bound here. More
        # workers help only when the file has enough chunks, so avoid creating
        # excess connections for small files.
        chunk = min(self.fast_chunk_size, 512 * 1024)
        chunks = max(1, (file_size + chunk - 1) // chunk)
        if file_size < 8 * 1024 * 1024:
            size_workers = 2
        elif file_size < 16 * 1024 * 1024:
            size_workers = 4
        else:
            size_workers = 8
        workers = min(self.fast_workers, size_workers, chunks)
        with temp_path.open("wb") as handle:
            handle.truncate(file_size)

        async def fetch_lane(lane: int) -> None:
            offset = lane * chunk
            stride = workers * chunk
            # Giữ một descriptor cho mỗi lane thay vì mở/đóng file ở từng
            # chunk; điều này giảm overhead local I/O với video lớn.
            with temp_path.open("r+b") as handle:
                while offset < file_size:
                    remaining = min(chunk, file_size - offset)
                    received = 0
                    async with self.fast_connections:
                        async for data in client.iter_download(
                            message,
                            offset=offset,
                            limit=1,
                            chunk_size=chunk,
                            request_size=chunk,
                            file_size=file_size,
                        ):
                            block = bytes(data)
                            if not block:
                                break
                            block = block[:remaining]
                            # Mỗi lane sở hữu offset riêng; đoạn ghi không có
                            # await nên không cần global write lock.
                            handle.seek(offset)
                            handle.write(block)
                            received += len(block)
                            break
                    if received != remaining:
                        raise IOError(
                            f"multipart chunk mismatch at {offset}: {received}/{remaining} bytes"
                        )
                    offset += stride

        try:
            await asyncio.gather(*(fetch_lane(lane) for lane in range(workers)))
            if temp_path.stat().st_size != file_size:
                raise IOError("multipart file size mismatch")
            temp_path.replace(final_path)
            return str(final_path)
        except BaseException:
            try:
                temp_path.unlink(missing_ok=True)
            except Exception:
                pass
            raise


def cleanup_stale_files(directory: Union[str, Path], max_age_seconds: float = 3600.0) -> int:
    """Xoá các FILE (không đụng thư mục con) cũ hơn max_age_seconds trong
    'directory'. Trả về số file đã xoá.

    An toàn:
      - Chỉ xoá FILE, không đụng thư mục con.
      - Bỏ qua lỗi từng file riêng lẻ (đang bị khoá, quyền truy cập...).
      - Không xoá/tạo gì nếu 'directory' không tồn tại (trả về 0).

    ✅ FIX: luôn ép 'directory' về Path ngay đầu hàm — nếu caller lỡ truyền
    str (vd cleanup_stale_files("logs/tmp")), gọi .exists() thẳng lên str
    sẽ ném AttributeError vì str không có method này.
    """
    directory = Path(directory)
    if not directory.exists():
        return 0

    now = time.time()
    removed = 0
    try:
        entries = list(directory.iterdir())
    except OSError:
        return 0

    for entry in entries:
        try:
            if not entry.is_file():
                continue
            age = now - entry.stat().st_mtime
            if age > max_age_seconds:
                entry.unlink()
                removed += 1
        except Exception:
            continue

    return removed
