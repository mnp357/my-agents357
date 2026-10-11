from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path


class QuotaExceeded(RuntimeError):
    pass


class QuotaStore:
    def __init__(self, path: Path, jobs_per_hour: int, bytes_per_day: int):
        self.path = path
        self.jobs_per_hour = jobs_per_hour
        self.bytes_per_day = bytes_per_day
        self._lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS usage (user_key TEXT, created REAL, bytes INTEGER)")
            db.execute("CREATE INDEX IF NOT EXISTS usage_lookup ON usage(user_key, created)")

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=10)

    def reserve(self, key: str) -> int:
        now = time.time()
        with self._lock, self._connect() as db:
            db.execute("DELETE FROM usage WHERE created < ?", (now - 86_400 * 8,))
            jobs = db.execute(
                "SELECT COUNT(*) FROM usage WHERE user_key=? AND created>=?", (key, now - 3_600)
            ).fetchone()[0]
            used = db.execute(
                "SELECT COALESCE(SUM(bytes), 0) FROM usage WHERE user_key=? AND created>=?",
                (key, now - 86_400),
            ).fetchone()[0]
            if jobs >= self.jobs_per_hour:
                raise QuotaExceeded("Превышен лимит заданий за час")
            if used >= self.bytes_per_day:
                raise QuotaExceeded("Превышен суточный лимит объёма")
            cursor = db.execute("INSERT INTO usage(user_key, created, bytes) VALUES (?, ?, 0)", (key, now))
            return int(cursor.lastrowid)

    def ensure_capacity(self, reservation: int, size: int) -> None:
        now = time.time()
        with self._lock, self._connect() as db:
            row = db.execute("SELECT user_key FROM usage WHERE rowid=?", (reservation,)).fetchone()
            if not row:
                raise QuotaExceeded("Резерв квоты не найден")
            used = db.execute(
                "SELECT COALESCE(SUM(bytes), 0) FROM usage WHERE user_key=? AND created>=? AND rowid<>?",
                (row[0], now - 86_400, reservation),
            ).fetchone()[0]
            if used + size > self.bytes_per_day:
                raise QuotaExceeded("Ролик превышает остаток суточной квоты")

    def complete(self, reservation: int, size: int) -> None:
        self.ensure_capacity(reservation, size)
        with self._lock, self._connect() as db:
            db.execute("UPDATE usage SET bytes=? WHERE rowid=?", (size, reservation))
