"""SQLite-backed local write-ahead log for closed bars.

No Kafka / Flink.  If a ClickHouse batch insert fails, the exact closed bars are
append-only in SQLite and retried with backoff.  A small dead-letter table
records unrecoverable failures for inspection.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

import orjson

from ..models import Kline


def _encode_row(value: Any) -> Any:
    if isinstance(value, datetime):
        return {"__dt__": value.isoformat()}
    if isinstance(value, (list, tuple)):
        return [_encode_row(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _encode_row(v) for k, v in value.items()}
    return value


def _decode_row(value: Any) -> Any:
    if isinstance(value, dict) and set(value) == {"__dt__"}:
        return datetime.fromisoformat(value["__dt__"])
    if isinstance(value, list):
        return [_decode_row(v) for v in value]
    if isinstance(value, dict):
        return {k: _decode_row(v) for k, v in value.items()}
    return value

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA busy_timeout=5000;

CREATE TABLE IF NOT EXISTS kline_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    payload TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt REAL NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS analytics_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    table_name TEXT NOT NULL,
    columns_json TEXT NOT NULL,
    row_json TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt REAL NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS dead_letters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    error TEXT NOT NULL,
    created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_kline_queue_ready
    ON kline_queue(next_attempt, id);

CREATE INDEX IF NOT EXISTS idx_analytics_queue_ready
    ON analytics_queue(next_attempt, id);
"""


class Wal:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------
    def add_klines(self, bars: Iterable[Kline]) -> int:
        rows = [(orjson.dumps(b.to_json()).decode(), time.time()) for b in bars]
        if not rows:
            return 0
        with self._lock:
            self._conn.executemany(
                "INSERT INTO kline_queue(payload, created_at) VALUES (?, ?)", rows
            )
            self._conn.commit()
        return len(rows)

    def pending(self, limit: int = 1000) -> list[tuple[int, Kline, int]]:
        now = time.time()
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, payload, attempts FROM kline_queue "
                "WHERE next_attempt <= ? ORDER BY id LIMIT ?",
                (now, limit),
            ).fetchall()
        out: list[tuple[int, Kline, int]] = []
        for row_id, payload, attempts in rows:
            try:
                out.append((int(row_id), Kline(**orjson.loads(payload)), int(attempts)))
            except Exception:
                # Corrupt row: move it out of the retry path.
                with self._lock:
                    self._conn.execute("DELETE FROM kline_queue WHERE id = ?", (row_id,))
                    self._conn.commit()
        return out

    def delete(self, ids: Iterable[int]) -> None:
        values = [(int(i),) for i in ids]
        if not values:
            return
        with self._lock:
            self._conn.executemany("DELETE FROM kline_queue WHERE id = ?", values)
            self._conn.commit()

    def mark_retry(self, ids: Iterable[int], *, base_seconds: float = 5.0,
                   cap_seconds: float = 300.0) -> None:
        now = time.time()
        with self._lock:
            for row_id in ids:
                row = self._conn.execute(
                    "SELECT attempts FROM kline_queue WHERE id = ?", (int(row_id),)
                ).fetchone()
                attempts = (int(row[0]) if row else 0) + 1
                delay = min(cap_seconds, base_seconds * (2 ** min(attempts, 8)))
                self._conn.execute(
                    "UPDATE kline_queue SET attempts = ?, next_attempt = ? WHERE id = ?",
                    (attempts, now + delay, int(row_id)),
                )
            self._conn.commit()

    def add_analytics(self, table: str, columns: list[str],
                      rows: list[tuple]) -> int:
        payloads = [
            (table, orjson.dumps(columns).decode(),
             orjson.dumps(_encode_row(row)).decode(), time.time())
            for row in rows
        ]
        if not payloads:
            return 0
        with self._lock:
            self._conn.executemany(
                "INSERT INTO analytics_queue(table_name, columns_json, row_json, created_at) "
                "VALUES (?, ?, ?, ?)",
                payloads,
            )
            self._conn.commit()
        return len(payloads)

    def pending_analytics(self, limit: int = 2000) -> list[tuple[int, str, list[str], tuple, int]]:
        now = time.time()
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, table_name, columns_json, row_json, attempts "
                "FROM analytics_queue WHERE next_attempt <= ? ORDER BY id LIMIT ?",
                (now, limit),
            ).fetchall()
        out: list[tuple[int, str, list[str], tuple, int]] = []
        for row_id, table, columns_json, row_json, attempts in rows:
            try:
                out.append((
                    int(row_id), str(table), list(orjson.loads(columns_json)),
                    tuple(_decode_row(orjson.loads(row_json))), int(attempts),
                ))
            except Exception:
                with self._lock:
                    self._conn.execute("DELETE FROM analytics_queue WHERE id = ?", (row_id,))
                    self._conn.commit()
        return out

    def delete_analytics(self, ids: Iterable[int]) -> None:
        values = [(int(i),) for i in ids]
        if not values:
            return
        with self._lock:
            self._conn.executemany("DELETE FROM analytics_queue WHERE id = ?", values)
            self._conn.commit()

    def mark_analytics_retry(self, ids: Iterable[int], *, base_seconds: float = 5.0,
                             cap_seconds: float = 300.0) -> None:
        now = time.time()
        with self._lock:
            for row_id in ids:
                row = self._conn.execute(
                    "SELECT attempts FROM analytics_queue WHERE id = ?", (int(row_id),)
                ).fetchone()
                attempts = (int(row[0]) if row else 0) + 1
                delay = min(cap_seconds, base_seconds * (2 ** min(attempts, 8)))
                self._conn.execute(
                    "UPDATE analytics_queue SET attempts = ?, next_attempt = ? WHERE id = ?",
                    (attempts, now + delay, int(row_id)),
                )
            self._conn.commit()

    def dead_letter(self, kind: str, payload: Any, error: str) -> None:
        if isinstance(payload, (dict, list)):
            encoded = orjson.dumps(payload).decode()
        elif isinstance(payload, bytes):
            encoded = payload.decode(errors="replace")
        else:
            encoded = str(payload)
        with self._lock:
            self._conn.execute(
                "INSERT INTO dead_letters(kind, payload, error, created_at) VALUES (?, ?, ?, ?)",
                (kind, encoded[:20000], error[:4000], time.time()),
            )
            self._conn.commit()

    def stats(self) -> dict[str, int]:
        with self._lock:
            pending = self._conn.execute("SELECT COUNT(*) FROM kline_queue").fetchone()[0]
            analytics = self._conn.execute("SELECT COUNT(*) FROM analytics_queue").fetchone()[0]
            dead = self._conn.execute("SELECT COUNT(*) FROM dead_letters").fetchone()[0]
        return {
            "pending": int(pending),
            "analytics_pending": int(analytics),
            "dead_letters": int(dead),
        }
