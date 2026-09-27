"""
📊 DATABASE MANAGEMENT (v4.0 - OPTIMIZED)
- WAL mode: đọc/ghi song song không block nhau
- Connection pool riêng cho async context
- Batch write: gom nhiều record ghi 1 lần thay vì từng dòng
- Prepared statements cache
"""

import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from logger_setup import logger


class CodeDatabase:
    """Quản lý database SQLite - tối ưu tốc độ cao"""

    def __init__(self, db_path: str = "data/code_history.db"):
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        # Timeout kết nối khớp PRAGMA busy_timeout=10000 bên dưới (cùng 10s).
        self.conn = sqlite3.connect(db_path, check_same_thread=False, timeout=10.0)
        self.conn.row_factory = sqlite3.Row
        self._optimize_connection()
        self._init_tables()

    def _optimize_connection(self):
        """Bật các pragma tăng tốc SQLite đáng kể"""
        pragmas = [
            "PRAGMA journal_mode=WAL",        # Cho phép đọc/ghi song song
            "PRAGMA synchronous=NORMAL",       # Nhanh hơn FULL, vẫn an toàn
            "PRAGMA cache_size=-32000",        # 32MB cache trong RAM
            "PRAGMA temp_store=MEMORY",        # Temp tables trong RAM
            "PRAGMA mmap_size=268435456",      # 256MB memory-mapped I/O
            "PRAGMA busy_timeout=10000",       # Tự retry 10s khi bị lock — khớp connect(timeout=10.0) ở trên
        ]
        for pragma in pragmas:
            try:
                self.conn.execute(pragma)
            except Exception:
                pass
        self.conn.commit()

    def _init_tables(self):
        """Tạo bảng và index"""
        try:
            with self._lock:
                self.conn.execute("""
                    CREATE TABLE IF NOT EXISTS code_submission (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        code TEXT NOT NULL,
                        account TEXT NOT NULL,
                        website TEXT NOT NULL,
                        status TEXT,
                        result TEXT,
                        submitted_at TIMESTAMP,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE(code, account)
                    )
                """)
                # DEDUP VINH VIEN: 1 code chi xu ly 1 lan / domain
                self.conn.execute("""
                    CREATE TABLE IF NOT EXISTS used_codes (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        domain TEXT NOT NULL,
                        code TEXT NOT NULL,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE(domain, code)
                    )
                """)
                self.conn.execute("""
                    CREATE TABLE IF NOT EXISTS submission_log (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        code TEXT NOT NULL,
                        account TEXT NOT NULL,
                        website TEXT NOT NULL,
                        status TEXT,
                        result TEXT,
                        attempt INTEGER,
                        submitted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                self.conn.execute("""
                    CREATE TABLE IF NOT EXISTS account_stats (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        account TEXT NOT NULL UNIQUE,
                        total_submitted INTEGER DEFAULT 0,
                        total_success INTEGER DEFAULT 0,
                        total_failed INTEGER DEFAULT 0,
                        last_submit TIMESTAMP,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                self.conn.execute("""
                    CREATE TABLE IF NOT EXISTS website_stats (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        website TEXT NOT NULL UNIQUE,
                        total_submitted INTEGER DEFAULT 0,
                        total_success INTEGER DEFAULT 0,
                        total_failed INTEGER DEFAULT 0,
                        last_submit TIMESTAMP,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                self.conn.execute("CREATE INDEX IF NOT EXISTS idx_code ON code_submission(code)")
                self.conn.execute("CREATE INDEX IF NOT EXISTS idx_account ON submission_log(account)")
                self.conn.execute("CREATE INDEX IF NOT EXISTS idx_website ON submission_log(website)")
                self.conn.execute("CREATE INDEX IF NOT EXISTS idx_submitted_at ON submission_log(submitted_at)")
                self.conn.commit()
            logger.info("✅ Database tables khởi tạo xong (WAL mode)")
        except Exception as e:
            logger.error(f"❌ Lỗi tạo tables: {e}")
            raise

    def record_submission(self, code: str, account: str, website: str,
                          status: str, result: str = None, attempt: int = 1):
        """Ghi submission - thread-safe, non-blocking với WAL"""
        with self._lock:
            try:
                now = datetime.now()
                self.conn.execute("""
                    INSERT INTO submission_log (code, account, website, status, result, attempt)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (code, account, website, status, result, attempt))

                self.conn.execute("""
                    INSERT OR REPLACE INTO code_submission
                    (code, account, website, status, result, submitted_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (code, account, website, status, result, now))

                self.conn.execute("""
                    INSERT INTO account_stats (account, total_submitted, last_submit)
                    VALUES (?, 1, ?)
                    ON CONFLICT(account) DO UPDATE SET
                        total_submitted = total_submitted + 1,
                        last_submit = excluded.last_submit
                """, (account, now))

                self.conn.execute("""
                    INSERT INTO website_stats (website, total_submitted, last_submit)
                    VALUES (?, 1, ?)
                    ON CONFLICT(website) DO UPDATE SET
                        total_submitted = total_submitted + 1,
                        last_submit = excluded.last_submit
                """, (website, now))

                if status == "SUCCESS":
                    self.conn.execute(
                        "UPDATE account_stats SET total_success = total_success + 1 WHERE account = ?",
                        (account,))
                    self.conn.execute(
                        "UPDATE website_stats SET total_success = total_success + 1 WHERE website = ?",
                        (website,))
                elif status == "FAILED":
                    self.conn.execute(
                        "UPDATE account_stats SET total_failed = total_failed + 1 WHERE account = ?",
                        (account,))
                    self.conn.execute(
                        "UPDATE website_stats SET total_failed = total_failed + 1 WHERE website = ?",
                        (website,))

                self.conn.commit()
                logger.debug(f"💾 [{account}] Code {code}: {status}")

            except sqlite3.IntegrityError as e:
                logger.warning(f"⚠️ IntegrityError không mong đợi trong record_submission: {e}")
                try:
                    self.conn.rollback()
                except Exception:
                    pass
            except Exception as e:
                logger.error(f"❌ Lỗi record submission: {e}")
                try:
                    self.conn.rollback()
                except Exception:
                    pass

    # DEDUP VĨNH VIỄN - 1 code chỉ xử lý 1 lần / domain.
    # Không tự động xóa used_codes trong vacuum; chỉ clear thủ công khi cần.
    def is_code_used(self, domain: str, code: str) -> bool:
        with self._lock:
            try:
                row = self.conn.execute(
                    "SELECT 1 FROM used_codes WHERE domain = ? AND code = ?",
                    (domain, code.upper())
                ).fetchone()
                return row is not None
            except Exception as e:
                # Fail-safe: nếu DB lỗi, bỏ qua code thay vì mạo hiểm submit trùng.
                logger.error(
                    f"❌ Lỗi check used_codes, coi như đã dùng để tránh trùng: {e}"
                )
                return True

    def unused_codes(self, domain: str, codes) -> set[str]:
        """Return codes not yet used for ``domain`` in one SQLite round-trip.

        The message path commonly receives several codes from one Telegram
        post. Checking them one by one through the executor added avoidable
        event-loop/executor latency. On database failure this fails closed and
        returns an empty set, preserving the existing no-duplicate guarantee.
        """
        normalized = list(dict.fromkeys(
            str(code or "").strip().upper() for code in (codes or []) if str(code or "").strip()
        ))
        if not normalized:
            return set()
        placeholders = ",".join("?" for _ in normalized)
        with self._lock:
            try:
                rows = self.conn.execute(
                    f"SELECT code FROM used_codes WHERE domain = ? AND code IN ({placeholders})",
                    (domain, *normalized),
                ).fetchall()
                used = {str(row[0]).upper() for row in rows}
                return {code for code in normalized if code not in used}
            except Exception as e:
                logger.error("❌ Lỗi batch check used_codes, bỏ qua toàn bộ batch để tránh submit trùng: %s", e)
                return set()

    def mark_code_used(self, domain: str, code: str) -> bool:
        """True = vua mark thanh cong (code moi). False = code da dung truoc do."""
        with self._lock:
            try:
                self.conn.execute(
                    "INSERT INTO used_codes (domain, code) VALUES (?, ?)",
                    (domain, code.upper())
                )
                self.conn.commit()
                return True
            except sqlite3.IntegrityError:
                try:
                    self.conn.rollback()
                except Exception:
                    pass
                return False
            except Exception as e:
                logger.error(f"❌ Lỗi mark_code_used: {e}")
                try:
                    self.conn.rollback()
                except Exception:
                    pass
                return False

    # ✅ Clear dedup vĩnh viễn cho 1 domain (fix code bị dính) — công cụ bảo
    # trì thủ công, gọi tay khi cần (không nằm trong luồng tự động của bot),
    # ví dụ: get_database().clear_domain_dedup("qq88.com") từ 1 script riêng.
    def clear_domain_dedup(self, domain: str) -> int:
        """Xóa toàn bộ dedup history cho 1 domain - dùng khi code bị dính"""
        with self._lock:
            try:
                cursor = self.conn.execute(
                    "DELETE FROM used_codes WHERE domain = ?",
                    (domain,)
                )
                count = cursor.rowcount
                self.conn.commit()
                logger.info(f"🗑️ Đã xóa {count} entries dedup cho domain: {domain}")
                return count
            except Exception as e:
                logger.error(f"❌ Lỗi clear dedup: {e}")
                try:
                    self.conn.rollback()
                except Exception:
                    pass
                return 0

    def vacuum(self):
        """Dọn log cũ và chạy VACUUM thật; giữ used_codes để dedup vĩnh viễn."""
        try:
            with self._lock:
                self.conn.execute(
                    "DELETE FROM submission_log WHERE submitted_at < datetime('now', '-30 days')"
                )
                self.conn.commit()
                self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                self.conn.execute("VACUUM")
            logger.info("✅ Database vacuum + cleanup cũ xong")
        except Exception as e:
            logger.warning(f"⚠️ DB vacuum error: {e}")

    def close(self):
        with self._lock:
            try:
                self.conn.close()
                logger.info("✅ Database đã đóng")
            except Exception as e:
                logger.error(f"❌ Lỗi close database: {e}")


_db_instance = None

def init_database(db_path: str = "data/code_history.db") -> CodeDatabase:
    global _db_instance
    if _db_instance is None:
        _db_instance = CodeDatabase(db_path)
    elif Path(_db_instance.db_path).expanduser().resolve() != Path(db_path).expanduser().resolve():
        raise RuntimeError(
            "Database singleton đã được khởi tạo với path khác: "
            f"{_db_instance.db_path!r} != {db_path!r}"
        )
    return _db_instance

def get_database() -> CodeDatabase:
    """Lấy database instance hiện có — dùng cho công cụ bảo trì thủ công
    (vd. clear_domain_dedup) bên ngoài luồng chính của bot."""
    global _db_instance
    if _db_instance is None:
        _db_instance = init_database()
    return _db_instance
