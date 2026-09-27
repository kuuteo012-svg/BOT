"""Durable Telegram inbox for at-least-once browser submission.

The inbox stores only serializable Telegram metadata. Workers keep a lightweight
row_id in RAM and re-fetch the Telegram message when they claim the row.
"""
from __future__ import annotations

import hashlib
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from logger_setup import logger


class DurableInbox:
    def __init__(
        self,
        db_path: str = "data/telegram_inbox.db",
        lease_seconds: int = 300,
        max_attempts: int = 5,
        retry_base_delay: float = 2.0,
        retry_max_delay: float = 120.0,
    ):
        self.db_path = str(db_path)
        self.lease_seconds = max(30, int(lease_seconds))
        # ✅ FIX: chặn retry vô hạn — trước đây retry() không có giới hạn số
        # lần thử, nên 1 tin nhắn bị site trả NO_RESULT/lỗi liên tục sẽ bị
        # replay lại (fetch lại message, extract lại code, submit lại) MÃI
        # MÃI mỗi vài giây, chiếm slot xử lý và làm trễ tin nhắn mới thật sự
        # (message_queue/domain_queue bị dồn ứ bởi backlog cũ). Giờ mỗi
        # dòng bị giới hạn tối đa max_attempts lần claim; vượt quá sẽ tự
        # động mark_failed thay vì tiếp tục replay — xem retry_or_fail().
        self.max_attempts = max(1, int(max_attempts))
        self.retry_base_delay = max(0.1, float(retry_base_delay))
        self.retry_max_delay = max(self.retry_base_delay, float(retry_max_delay))
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.db_path, timeout=10.0, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._configure()
        self._init_schema()

    def _configure(self) -> None:
        for pragma in (
            "PRAGMA journal_mode=WAL",
            "PRAGMA synchronous=NORMAL",
            "PRAGMA busy_timeout=10000",
            "PRAGMA temp_store=MEMORY",
            "PRAGMA cache_size=-16000",
        ):
            try:
                self._conn.execute(pragma)
            except sqlite3.DatabaseError:
                pass
        self._conn.commit()

    def _init_schema(self) -> None:
        with self._lock:
            # ✅ FIX (dedup): khoá UNIQUE trước đây là
            # (chat_id, message_id, edited, content_hash) — cột 'edited'
            # nằm TRONG khoá khiến Telegram gửi event "edited" cho ĐÚNG
            # tin nhắn cũ (vd chỉ view-count cập nhật, nội dung không đổi)
            # tạo ra một khoá KHÁC (edited 0→1) → INSERT OR IGNORE không
            # ignore được nữa mà chèn thêm 1 DÒNG MỚI cho cùng 1 tin nhắn.
            # Dòng mới này bị worker lấy ra xử lý lại từ đầu → OCR lại,
            # submit lại y hệt các mã cũ 10-15 phút sau (đã xác nhận qua
            # log: cùng message_id xuất hiện 2 lần, cách nhau ~15 phút).
            # Khoá đúng chỉ nên dựa vào NỘI DUNG thực sự của tin nhắn:
            # (chat_id, message_id, content_hash) — 'edited' không còn là
            # 1 phần của khoá, chỉ lưu để tham khảo/log.
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS telegram_inbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL,
                    message_date TEXT,
                    edited INTEGER NOT NULL DEFAULT 0,
                    content_hash TEXT NOT NULL,
                    text TEXT NOT NULL DEFAULT '',
                    has_media INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    remaining_items INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    locked_at TEXT,
                    claim_token TEXT,
                    next_attempt_at TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    completed_at TEXT,
                    UNIQUE(chat_id, message_id, content_hash)
                );
                CREATE INDEX IF NOT EXISTS idx_inbox_ready
                    ON telegram_inbox(status, created_at);
                CREATE INDEX IF NOT EXISTS idx_inbox_pending_due
                    ON telegram_inbox(status, next_attempt_at, id);
                CREATE INDEX IF NOT EXISTS idx_inbox_message
                    ON telegram_inbox(chat_id, message_id);
                """
            )
            columns = {
                row[1]
                for row in self._conn.execute("PRAGMA table_info(telegram_inbox)").fetchall()
            }
            if "next_attempt_at" not in columns:
                self._conn.execute(
                    "ALTER TABLE telegram_inbox ADD COLUMN next_attempt_at TEXT"
                )
            if "claim_token" not in columns:
                self._conn.execute(
                    "ALTER TABLE telegram_inbox ADD COLUMN claim_token TEXT"
                )
            self._conn.commit()
            # DB có sẵn từ trước khi vá lỗi này vẫn còn schema cũ (UNIQUE
            # bao gồm 'edited') — CREATE TABLE IF NOT EXISTS ở trên không
            # đổi được bảng đã tồn tại, nên phải migrate riêng.
            self._migrate_drop_edited_from_unique()
            self._conn.commit()

    def _migrate_drop_edited_from_unique(self) -> None:
        """Nâng cấp DB cũ: bỏ cột 'edited' khỏi khoá UNIQUE để chặn dòng
        trùng khi Telegram gửi event edited cho tin nhắn không đổi nội
        dung (xem giải thích chi tiết ở _init_schema). An toàn để gọi mỗi
        lần khởi động — chỉ thực sự chạy khi phát hiện đúng schema cũ."""
        row = self._conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='telegram_inbox'"
        ).fetchone()
        if not row or not row[0]:
            return
        table_sql_compact = row[0].lower().replace(" ", "").replace("\n", "")
        if "unique(chat_id,message_id,edited,content_hash)" not in table_sql_compact:
            return  # Đã ở schema mới (hoặc bảng vừa mới tạo) — không cần migrate.

        logger.warning(
            "🔧 [Inbox] Phát hiện schema cũ (UNIQUE có 'edited') — đang migrate "
            "để chặn dòng trùng do Telegram gửi event edited (vd view-count "
            "cập nhật) cho tin nhắn không đổi nội dung..."
        )
        try:
            self._conn.execute("ALTER TABLE telegram_inbox RENAME TO telegram_inbox_old")
            self._conn.execute(
                """
                CREATE TABLE telegram_inbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL,
                    message_date TEXT,
                    edited INTEGER NOT NULL DEFAULT 0,
                    content_hash TEXT NOT NULL,
                    text TEXT NOT NULL DEFAULT '',
                    has_media INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    remaining_items INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    locked_at TEXT,
                    claim_token TEXT,
                    next_attempt_at TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    completed_at TEXT,
                    UNIQUE(chat_id, message_id, content_hash)
                )
                """
            )

            old_cols = [
                r[1] for r in self._conn.execute("PRAGMA table_info(telegram_inbox_old)").fetchall()
            ]
            rows = self._conn.execute(f"SELECT {','.join(old_cols)} FROM telegram_inbox_old").fetchall()

            # Nhiều dòng cũ có thể trùng (chat_id, message_id, content_hash)
            # do chính bug này — gộp lại, ưu tiên giữ dòng phản ánh kết quả
            # "chín" nhất (completed > processing > failed > pending >
            # ignored), rồi tới dòng cập nhật gần nhất.
            status_rank = {"completed": 0, "processing": 1, "failed": 2, "pending": 3, "ignored": 4}
            best: dict[tuple, dict] = {}
            for r in rows:
                d = dict(zip(old_cols, r))
                key = (d["chat_id"], d["message_id"], d["content_hash"])
                cur = best.get(key)
                if cur is None:
                    best[key] = d
                    continue
                rank_new = status_rank.get(d.get("status"), 5)
                rank_old = status_rank.get(cur.get("status"), 5)
                if rank_new < rank_old or (
                    rank_new == rank_old
                    and str(d.get("updated_at") or "") > str(cur.get("updated_at") or "")
                ):
                    best[key] = d

            insert_cols = [c for c in old_cols if c != "id"]
            placeholders = ",".join("?" for _ in insert_cols)
            for d in best.values():
                self._conn.execute(
                    f"INSERT INTO telegram_inbox ({','.join(insert_cols)}) VALUES ({placeholders})",
                    [d[c] for c in insert_cols],
                )

            self._conn.execute("DROP TABLE telegram_inbox_old")
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_inbox_ready ON telegram_inbox(status, created_at)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_inbox_pending_due ON telegram_inbox(status, next_attempt_at, id)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_inbox_message ON telegram_inbox(chat_id, message_id)"
            )
            self._conn.commit()

            kept, total = len(best), len(rows)
            logger.warning(
                "✅ [Inbox] Migration xong: giữ %s/%s dòng (đã gộp %s dòng trùng "
                "chat_id+message_id+content_hash phát sinh từ bug 'edited' cũ)",
                kept, total, total - kept,
            )
        except Exception:
            self._conn.rollback()
            logger.exception(
                "❌ [Inbox] Migration schema thất bại — DB có thể vẫn ở schema cũ. "
                "Kiểm tra thủ công file %s trước khi chạy lại bot.",
                self.db_path,
            )
            raise

    @staticmethod
    def content_hash(text: str = "", has_media: bool = False) -> str:
        payload = f"{text or ''}\x1f{int(bool(has_media))}".encode("utf-8", "ignore")
        return hashlib.sha256(payload).hexdigest()[:32]

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    def enqueue_detailed(
        self,
        chat_id: int,
        message_id: int,
        message_date: Any,
        edited: bool,
        text: str,
        has_media: bool,
        content_hash: str | None = None,
    ) -> dict[str, Any]:
        """Insert once and report the exact durable-ingress outcome.

        Returns ``inserted`` with a new id, ``duplicate`` without an id, or
        ``sqlite_error`` with the database error. Returning an existing row id
        is unsafe because the ingress path would enqueue a completed (or
        already-processing) row again, replaying the same message after an
        edited/view-count update or a transport redelivery.
        """
        h = content_hash or self.content_hash(text, has_media)
        date_text = message_date.isoformat() if hasattr(message_date, "isoformat") else str(message_date or "")
        now = self._now()
        with self._lock:
            try:
                cur = self._conn.execute(
                    """
                    INSERT OR IGNORE INTO telegram_inbox
                    (chat_id, message_id, message_date, edited, content_hash, text, has_media,
                     status, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                    """,
                    (int(chat_id), int(message_id), date_text, int(bool(edited)), h, text or "", int(bool(has_media)), now, now),
                )
                self._conn.commit()
                # INSERT OR IGNORE may expose sqlite3's previous successful
                # lastrowid when this insert was ignored. Use rowcount as the
                # reliable inserted-vs-duplicate signal under concurrent
                # delivery. A duplicate must not return the old row id: the
                # caller would enqueue that old row and replay the message.
                if cur.rowcount == 1 and cur.lastrowid:
                    return {"status": "inserted", "id": int(cur.lastrowid)}
                return {"status": "duplicate", "id": None}
            except Exception as exc:
                self._conn.rollback()
                logger.error("❌ [Inbox] enqueue lỗi: %s", exc)
                return {"status": "sqlite_error", "id": None, "error": str(exc)}

    def enqueue(
        self,
        chat_id: int,
        message_id: int,
        message_date: Any,
        edited: bool,
        text: str,
        has_media: bool,
        content_hash: str | None = None,
    ) -> int | None:
        """Backward-compatible wrapper returning only a new row id."""
        result = self.enqueue_detailed(
            chat_id, message_id, message_date, edited, text, has_media, content_hash
        )
        return result.get("id")

    def reclaim_stale(self) -> int:
        with self._lock:
            cur = self._conn.execute(
                """
                UPDATE telegram_inbox
                SET status='pending', locked_at=NULL, updated_at=?
                WHERE status='processing'
                  AND locked_at < datetime('now', ?)
                """,
                (self._now(), f"-{self.lease_seconds} seconds"),
            )
            self._conn.commit()
            return int(cur.rowcount or 0)

    def ignore_pending_before(self, cutoff: datetime, reason: str = "startup_old_message") -> int:
        """Mark pending inbox rows older than ``cutoff`` as ignored.

        New-message-only mode must not replay rows left by a previous run.
        Dates are parsed in Python because Telegram timestamps may contain
        different ISO-8601 offsets.
        """
        if cutoff.tzinfo is None:
            cutoff = cutoff.replace(tzinfo=timezone.utc)
        cutoff = cutoff.astimezone(timezone.utc)
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, message_date FROM telegram_inbox WHERE status='pending'"
            ).fetchall()
            old_ids = []
            for row in rows:
                raw = str(row[1] or "").strip()
                try:
                    message_date = datetime.fromisoformat(raw)
                    if message_date.tzinfo is None:
                        message_date = message_date.replace(tzinfo=timezone.utc)
                    if message_date.astimezone(timezone.utc) < cutoff:
                        old_ids.append(int(row[0]))
                except (TypeError, ValueError, OverflowError):
                    # Invalid timestamps are unsafe to replay in strict mode.
                    old_ids.append(int(row[0]))
            if old_ids:
                now = self._now()
                self._conn.executemany(
                    """
                    UPDATE telegram_inbox
                    SET status='ignored', last_error=?, locked_at=NULL,
                        updated_at=?, completed_at=?
                    WHERE id=? AND status='pending'
                    """,
                    [(reason[:500], now, now, row_id) for row_id in old_ids],
                )
                self._conn.commit()
            return len(old_ids)

    def discard_unfinished(self, reason: str = "startup_discard_unfinished") -> int:
        """Discard all unfinished local rows before a fresh live-only run.

        This never deletes Telegram messages. It only prevents pending or
        abandoned processing rows from being replayed after a restart.
        """
        with self._lock:
            now = self._now()
            cur = self._conn.execute(
                """
                UPDATE telegram_inbox
                SET status='ignored', last_error=?, locked_at=NULL,
                    updated_at=?, completed_at=?
                WHERE status IN ('pending', 'processing')
                """,
                (reason[:500], now, now),
            )
            self._conn.commit()
            return int(cur.rowcount or 0)

    def pending_ids(self, limit: int = 500) -> list[int]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT id FROM telegram_inbox
                WHERE status='pending'
                  AND (next_attempt_at IS NULL OR next_attempt_at <= datetime('now'))
                ORDER BY id LIMIT ?
                """,
                (max(1, int(limit)),),
            ).fetchall()
            return [int(r[0]) for r in rows]

    def claim(self, row_id: int) -> dict[str, Any] | None:
        """Atomically claim a due row and return a fencing token."""
        now = self._now()
        claim_token = uuid.uuid4().hex
        with self._lock:
            try:
                # ✅ FIX perf: gộp UPDATE + SELECT lại thành 1 lượt gọi
                # SQLite bằng RETURNING * (SQLite ≥3.35, có sẵn từ bản
                # sqlite3 đi kèm Python 3.10+/3.12). claim() nằm trên
                # đường dẫn nóng nhất — chạy ít nhất 1 lần cho MỖI dòng
                # inbox được xử lý — nên trước đây phải làm 2 lượt round-
                # trip (UPDATE rồi SELECT lại nguyên dòng) cho mỗi lần
                # claim; giờ chỉ còn 1.
                cur = self._conn.execute(
                    """
                    UPDATE telegram_inbox
                    SET status='processing', attempts=attempts+1, locked_at=?, claim_token=?, updated_at=?
                    WHERE id=? AND status='pending'
                      AND (next_attempt_at IS NULL OR next_attempt_at <= datetime('now'))
                    RETURNING *
                    """,
                    (now, claim_token, now, int(row_id)),
                )
                row = cur.fetchone()
                self._conn.commit()
                return dict(row) if row else None
            except Exception:
                self._conn.rollback()
                raise

    def set_remaining(self, row_id: int, count: int, claim_token: str | None = None) -> None:
        with self._lock:
            now = self._now()
            status = "completed" if int(count) <= 0 else "processing"
            self._conn.execute(
                "UPDATE telegram_inbox SET remaining_items=?, status=?, next_attempt_at=NULL, updated_at=?, completed_at=? WHERE id=? AND status='processing'" + (" AND claim_token=?" if claim_token else ""),
                (max(0, int(count)), status, now, now if status == "completed" else None, int(row_id), *(([claim_token] if claim_token else []))),
            )
            self._conn.commit()

    def complete_item(self, row_id: int, claim_token: str | None = None) -> bool:
        """Decrement work count; true when the durable row is fully completed."""
        with self._lock:
            now = self._now()
            # ✅ FIX perf: gộp 2 UPDATE trước đây (giảm remaining_items, rồi
            # xét flip status→completed) thành 1 UPDATE dùng CASE + RETURNING
            # — remaining_items-1 chỉ được TÍNH LẠI trong CASE (không đọc
            # giá trị đã ghi ở nhánh khác cùng câu lệnh), nên tương đương
            # hệt logic cũ. complete_item() chạy 1 lần/fanout item nên đây
            # cũng là đường dẫn nóng — trước đây tốn 3 lượt gọi SQLite
            # (UPDATE, UPDATE, SELECT) cho MỖI lần gọi, giờ còn 1 ở nhánh
            # thường gặp (dòng đang ở status='processing').
            sql = """
                UPDATE telegram_inbox
                SET
                    remaining_items = MAX(remaining_items-1, 0),
                    status = CASE WHEN MAX(remaining_items-1, 0) = 0 THEN 'completed' ELSE status END,
                    completed_at = CASE WHEN MAX(remaining_items-1, 0) = 0 THEN ? ELSE completed_at END,
                    locked_at = CASE WHEN MAX(remaining_items-1, 0) = 0 THEN NULL ELSE locked_at END,
                    updated_at = ?
                WHERE id=? AND status='processing'
            """
            params = [now, now, int(row_id)]
            if claim_token:
                sql += " AND claim_token=?"
                params.append(claim_token)
            sql += " RETURNING status"
            row = self._conn.execute(sql, params).fetchone()
            if row is None:
                # WHERE không khớp (claim_token lệch, hoặc dòng không còn ở
                # 'processing' — vd đã completed từ 1 lần gọi complete_item
                # khác trước đó cho cùng row). Giữ ĐÚNG hành vi cũ: vẫn trả
                # True nếu dòng NÀY hiện đang là 'completed', bất kể có
                # phải chính lệnh gọi này làm nó completed hay không
                # (idempotent) — chỉ tốn thêm 1 round-trip ở nhánh hiếm
                # gặp này, thay vì luôn tốn ở MỌI lần gọi như code cũ.
                row = self._conn.execute(
                    "SELECT status FROM telegram_inbox WHERE id=?", (int(row_id),)
                ).fetchone()
            self._conn.commit()
            return bool(row and row[0] == "completed")

    def mark_ignored(self, row_id: int, reason: str = "no_code", claim_token: str | None = None) -> bool:
        """Finalize a processing row as 'ignored'.

        ✅ FIX (safety net): guarded with ``remaining_items=0`` — same
        condition as mark_ignored_if_empty(). A row whose code was fanned
        out to N accounts (set_remaining(N)) must never be finalized by one
        branch while sibling fanout items are still pending; that would
        silently drop the other accounts' submissions. Every current
        call-site in main_script.py already calls mark_ignored() only
        before set_remaining() ever ran on the row (remaining_items is 0
        by schema default at that point), so this does not change today's
        behavior — it only prevents a future call-site from reintroducing
        the fanout-loss bug. Returns True if the row was actually
        finalized, False if it still has pending fanout items or was not
        in 'processing' state (caller can ignore the return value; existing
        call-sites do not depend on side effects beyond this update)."""
        with self._lock:
            now = self._now()
            sql = (
                "UPDATE telegram_inbox SET status='ignored', last_error=?, "
                "locked_at=NULL, updated_at=?, completed_at=? "
                "WHERE id=? AND status='processing' AND remaining_items=0"
            )
            params = [reason[:500], now, now, int(row_id)]
            if claim_token:
                sql += " AND claim_token=?"
                params.append(str(claim_token))
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return bool(cur.rowcount)

    def mark_ignored_if_empty(
        self,
        row_id: int,
        reason: str = "no_code_or_not_routed",
        claim_token: str | None = None,
    ) -> bool:
        """Atomically ignore a processing row only when no fanout remains."""
        with self._lock:
            now = self._now()
            sql = (
                "UPDATE telegram_inbox SET status='ignored', last_error=?, "
                "locked_at=NULL, updated_at=?, completed_at=? "
                "WHERE id=? AND status='processing' AND remaining_items=0"
            )
            params = [reason[:500], now, now, int(row_id)]
            if claim_token:
                sql += " AND claim_token=?"
                params.append(claim_token)
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return bool(cur.rowcount)

    def mark_failed(self, row_id: int, error: str, claim_token: str | None = None) -> None:
        with self._lock:
            now = self._now()
            self._conn.execute(
                "UPDATE telegram_inbox SET status='failed', last_error=?, locked_at=NULL, updated_at=? WHERE id=? AND status='processing'" + (" AND claim_token=?" if claim_token else ""),
                (error[:500], now, int(row_id), *(([claim_token] if claim_token else []))),
            )
            self._conn.commit()

    def requeue_claimed_for_shutdown(
        self, row_id: int, reason: str = "worker cancelled"
    ) -> bool:
        """Requeue a 'processing' row back to 'pending' during graceful
        shutdown — distinct from retry()/retry_or_fail() on purpose.

        This is an operational requeue (the worker task was cancelled while
        the row was still in flight), not a failure signal, so it does not
        consume any retry budget of its own: no next_attempt_at backoff is
        set (the row becomes immediately claimable again on the next run/
        drain cycle) and the caller-visible ``attempts`` counter is only
        touched by claim() itself, exactly as it would be after any other
        requeue. Using retry()/retry_or_fail() here would either apply an
        unbounded delay-only reset (retry(), no max_attempts check) or
        could push the row toward 'failed' under repeated fast
        restarts (retry_or_fail(), which does count attempts) — neither is
        correct for a plain "we were still working on this when the
        process stopped" requeue. Returns True if a row was actually
        moved back to pending."""
        with self._lock:
            now = self._now()
            cur = self._conn.execute(
                """
                UPDATE telegram_inbox
                SET status='pending', locked_at=NULL, claim_token=NULL,
                    next_attempt_at=NULL, updated_at=?, last_error=?
                WHERE id=? AND status='processing'
                """,
                (now, reason[:500], int(row_id)),
            )
            self._conn.commit()
            return bool(cur.rowcount)

    def retry(self, row_id: int, error: str, delay_seconds: int = 2, claim_token: str | None = None) -> None:
        with self._lock:
            now = self._now()
            delay = max(0, int(delay_seconds))
            self._conn.execute(
                "UPDATE telegram_inbox "
                "SET status='pending', remaining_items=0, last_error=?, "
                "locked_at=NULL, next_attempt_at=datetime('now', ?), updated_at=? "
                "WHERE id=? AND status='processing'"
                + (" AND claim_token=?" if claim_token else ""),
                (error[:500], f"+{delay} seconds", now, int(row_id), *(([claim_token] if claim_token else []))),
            )
            self._conn.commit()

    def retry_or_fail(
        self,
        row_id: int,
        error: str,
        base_delay: float | None = None,
        claim_token: str | None = None,
    ) -> str:
        """Entry point BẮT BUỘC cho mọi retry từ bên ngoài (thay cho gọi
        thẳng retry()) — tự đọc số lần 'attempts' hiện tại của dòng, và:
          - Nếu đã đạt/vượt max_attempts → mark_failed() NGAY, KHÔNG replay
            thêm nữa (chặn retry-storm vô hạn — xem __init__ để biết lý do).
          - Nếu còn hạn mức → retry() với delay tăng dần theo cấp số nhân
            (exponential backoff), giới hạn ở retry_max_delay, tránh dội
            liên tục vào 1 site đang lỗi tạm thời.

        Trả về "retried" hoặc "failed" để caller log/theo dõi nếu cần.
        """
        delay = self.retry_base_delay if base_delay is None else max(0.1, float(base_delay))
        row_id = int(row_id)
        with self._lock:
            row = self._conn.execute(
                "SELECT attempts, status, claim_token FROM telegram_inbox WHERE id=?",
                (row_id,),
            ).fetchone()
            if not row:
                return "missing"

            attempts = int(row[0] or 0)
            status = str(row[1] or "")
            current_token = str(row[2] or "")
            if claim_token and current_token != str(claim_token):
                return "stale_claim"
            # A late failure callback must not move an already completed or
            # intentionally ignored row back into the processing pipeline.
            if status in {"completed", "ignored", "failed"}:
                return status

            now = self._now()
            if attempts >= self.max_attempts:
                sql = """
                    UPDATE telegram_inbox
                    SET status='failed', last_error=?, locked_at=NULL,
                        updated_at=?
                    WHERE id=? AND status IN ('pending', 'processing')
                """
                params = [f"{error} (đã vượt max_attempts={self.max_attempts}, dừng retry)"[:500], now, row_id]
                if claim_token:
                    sql += " AND claim_token=?"
                    params.append(str(claim_token))
                self._conn.execute(sql, params)
                self._conn.commit()
                logger.warning(
                    "🛑 [Inbox] row=%s vượt quá %s lần thử — đánh dấu 'failed', "
                    "KHÔNG replay lại tin nhắn nữa. Lỗi gần nhất: %s",
                    row_id, self.max_attempts, error,
                )
                return "failed"

            backoff = min(self.retry_max_delay, delay * (2 ** max(0, attempts - 1)))
            sql = """
                UPDATE telegram_inbox
                SET status='pending', remaining_items=0, last_error=?,
                    locked_at=NULL, next_attempt_at=datetime('now', ?), updated_at=?
                WHERE id=? AND status IN ('pending', 'processing')
            """
            params = [error[:500], f"+{int(round(backoff))} seconds", now, row_id]
            if claim_token:
                sql += " AND claim_token=?"
                params.append(str(claim_token))
            self._conn.execute(sql, params)
            self._conn.commit()
            return "retried"

    def get(self, row_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM telegram_inbox WHERE id=?", (int(row_id),)).fetchone()
            return dict(row) if row else None

    def purge_completed(self, keep_days: int = 7) -> int:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM telegram_inbox WHERE status IN ('completed','ignored') AND completed_at < datetime('now', ?)",
                (f"-{max(1, int(keep_days))} days",),
            )
            self._conn.commit()
            return int(cur.rowcount or 0)

    def close(self) -> None:
        with self._lock:
            self._conn.close()


__all__ = ["DurableInbox"]
