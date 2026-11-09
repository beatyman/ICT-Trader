"""ClickHouse closed-bar store + batched writer + SQLite WAL retry loop.

The synchronous clickhouse-connect client is never called from the event loop:
all driver calls go through ``asyncio.to_thread``.  Rows are buffered and
inserted in batches; a failed batch is appended to the local SQLite WAL.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Any

import clickhouse_connect
from clickhouse_connect.driver.exceptions import ClickHouseError

from ..metrics import Metrics
from ..models import Kline
from .wal import Wal

log = logging.getLogger(__name__)

CH_COLUMNS = [
    "symbol", "interval", "open_time", "close_time", "open", "high", "low",
    "close", "volume", "quote_volume", "trades", "taker_buy_base",
    "taker_buy_quote", "ingest_time",
]


class ClickHouseStore:
    def __init__(
        self,
        *,
        host: str,
        port: int,
        username: str,
        password: str,
        database: str,
        secure: bool,
        batch_size: int,
        flush_seconds: float,
        retry_seconds: float,
        retention_days: int,
        wal: Wal,
        metrics: Metrics,
    ) -> None:
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.database = database
        self.secure = secure
        self.batch_size = max(1, batch_size)
        self.flush_seconds = max(0.1, flush_seconds)
        self.retry_seconds = max(1.0, retry_seconds)
        self.retention_days = max(1, retention_days)
        self.wal = wal
        self.metrics = metrics
        self._client = None
        # clickhouse-connect sessions are not safe for concurrent queries.
        # Every driver call (writer, health probe, chart query) shares this
        # lock and runs in a worker thread.
        self._db_lock = threading.RLock()
        self._queue: asyncio.Queue[Kline] = asyncio.Queue()
        self._writer_task: asyncio.Task | None = None
        self._retry_task: asyncio.Task | None = None
        self._stopping = False
        self.last_error: str = ""

    # ------------------------------------------------------------------
    @property
    def client(self):
        if self._client is None:
            raise RuntimeError("ClickHouseStore.start() was not called")
        return self._client

    def _connect(self):
        return clickhouse_connect.get_client(
            host=self.host,
            port=self.port,
            username=self.username,
            password=self.password,
            database="default",
            secure=self.secure,
            connect_timeout=5,
            send_receive_timeout=30,
            query_limit=0,
        )

    async def start(self, *, attempts: int = 30) -> None:
        last: Exception | None = None
        for i in range(attempts):
            try:
                self._client = await asyncio.to_thread(self._connect)
                await asyncio.to_thread(self._ensure_schema_sync)
                break
            except Exception as exc:  # container may still be booting
                last = exc
                self.last_error = str(exc)
                if self._client is not None:
                    try:
                        await asyncio.to_thread(self._client.close)
                    except Exception:
                        pass
                    self._client = None
                if i == attempts - 1:
                    raise RuntimeError(f"ClickHouse unavailable after {attempts} attempts: {last}") from last
                await asyncio.sleep(2.0)
        self.last_error = ""
        self._stopping = False
        self._writer_task = asyncio.create_task(self._writer_loop(), name="ch-writer")
        self._retry_task = asyncio.create_task(self._retry_loop(), name="ch-wal-retry")
        log.info("ClickHouse connected: %s:%s/%s", self.host, self.port, self.database)

    def _command_sync(self, command: str, **kwargs: Any):
        with self._db_lock:
            return self.client.command(command, **kwargs)

    def _query_sync(self, query: str, **kwargs: Any):
        with self._db_lock:
            return self.client.query(query, **kwargs)

    def _insert_sync(self, table: str, rows: list[tuple], columns: list[str]) -> None:
        with self._db_lock:
            self.client.insert(table, rows, column_names=columns)

    def _ensure_schema_sync(self) -> None:
        db = self.database
        if not db.replace("_", "").isalnum():
            raise ValueError(f"unsafe ClickHouse database name: {db!r}")
        self._command_sync(f"CREATE DATABASE IF NOT EXISTS {db}")
        self._command_sync(
            f"""
            CREATE TABLE IF NOT EXISTS {db}.klines
            (
                symbol LowCardinality(String),
                interval LowCardinality(String),
                open_time DateTime64(3, 'UTC'),
                close_time DateTime64(3, 'UTC'),
                open Float64,
                high Float64,
                low Float64,
                close Float64,
                volume Float64,
                quote_volume Float64,
                trades UInt32,
                taker_buy_base Float64,
                taker_buy_quote Float64,
                ingest_time DateTime64(3, 'UTC')
            )
            ENGINE = ReplacingMergeTree(ingest_time)
            PARTITION BY toYYYYMM(open_time)
            ORDER BY (symbol, interval, open_time)
            TTL toDateTime(open_time) + INTERVAL {self.retention_days} DAY DELETE
            SETTINGS index_granularity = 8192
            """
        )
        self._command_sync(
            f"""
            CREATE TABLE IF NOT EXISTS {db}.paper_trades
            (
                trade_id String,
                signal_id String,
                symbol LowCardinality(String),
                interval LowCardinality(String),
                direction LowCardinality(String),
                entry_time DateTime64(3, 'UTC'),
                exit_time DateTime64(3, 'UTC'),
                entry Float64,
                exit Float64,
                stop Float64,
                target Float64,
                quantity Float64,
                notional Float64,
                pnl Float64,
                fees Float64,
                r_multiple Float64,
                exit_reason LowCardinality(String),
                created_at DateTime64(3, 'UTC')
            )
            ENGINE = ReplacingMergeTree(created_at)
            PARTITION BY toYYYYMM(exit_time)
            ORDER BY (symbol, trade_id)
            TTL toDateTime(exit_time) + INTERVAL {self.retention_days} DAY DELETE
            """
        )
        self._command_sync(
            f"""
            CREATE TABLE IF NOT EXISTS {db}.signals
            (
                signal_id String,
                symbol LowCardinality(String),
                interval LowCardinality(String),
                bar_time DateTime64(3, 'UTC'),
                created_at DateTime64(3, 'UTC'),
                direction LowCardinality(String),
                entry Float64,
                stop Float64,
                target Float64,
                rr Float64,
                grade UInt8,
                quality Float64,
                status LowCardinality(String),
                actionable UInt8,
                executed UInt8,
                reason String
            )
            ENGINE = ReplacingMergeTree(created_at)
            PARTITION BY toYYYYMM(bar_time)
            ORDER BY (symbol, signal_id)
            """
        )

    async def stop(self) -> None:
        self._stopping = True
        if self._writer_task is not None:
            try:
                await asyncio.wait_for(self._queue.join(), timeout=10.0)
            except asyncio.TimeoutError:
                log.warning("ClickHouse writer did not drain within 10s")
            self._writer_task.cancel()
            self._writer_task = None
        if self._retry_task is not None:
            self._retry_task.cancel()
            self._retry_task = None
        if self._client is not None:
            await asyncio.to_thread(self._client.close)
            self._client = None
        log.info("ClickHouse stopped")

    # ------------------------------------------------------------------
    async def enqueue(self, bar: Kline) -> None:
        await self._queue.put(bar)

    @property
    def queue_size(self) -> int:
        return self._queue.qsize()

    async def _writer_loop(self) -> None:
        while not self._stopping:
            batch: list[Kline] = []
            try:
                first = await self._queue.get()
                batch.append(first)
            except asyncio.CancelledError:
                return
            deadline = time.monotonic() + self.flush_seconds
            while len(batch) < self.batch_size:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    batch.append(await asyncio.wait_for(self._queue.get(), timeout=remaining))
                except asyncio.TimeoutError:
                    break
            try:
                await self._insert_batch(batch)
            finally:
                for _ in batch:
                    self._queue.task_done()

    async def _insert_batch(self, batch: list[Kline], *, wal_on_failure: bool = True) -> bool:
        if not batch:
            return True
        start = time.perf_counter()
        rows = [b.to_ch_row() for b in batch]
        try:
            await asyncio.to_thread(
                self._insert_sync, f"{self.database}.klines", rows, CH_COLUMNS,
            )
            elapsed = (time.perf_counter() - start) * 1000.0
            self.metrics.observe("clickhouse_batch_ms", elapsed)
            self.metrics.inc("ch_row", len(batch))
            self.metrics.inc("ch_batch")
            self.last_error = ""
            return True
        except Exception as exc:
            self.last_error = str(exc)
            if wal_on_failure:
                await asyncio.to_thread(self.wal.add_klines, batch)
            self.metrics.inc("ch_failed_rows", len(batch))
            suffix = "queued in WAL" if wal_on_failure else "retry deferred"
            self.metrics.event(
                "ERROR", "clickhouse",
                f"batch insert failed; {len(batch)} rows {suffix}",
                error=str(exc)[:300],
            )
            log.error("ClickHouse batch insert failed (%d rows): %s", len(batch), exc)
            return False

    async def _retry_loop(self) -> None:
        # Recover WAL rows left by the previous process first.
        while not self._stopping:
            try:
                pending = await asyncio.to_thread(self.wal.pending, 5000)
                if pending:
                    batch = [bar for _, bar, _ in pending]
                    inserted = await self._insert_batch(batch, wal_on_failure=False)
                    ids = [row_id for row_id, _, _ in pending]
                    if inserted:
                        await asyncio.to_thread(self.wal.delete, ids)
                        self.metrics.inc("wal_replayed", len(pending))
                        self.metrics.event("INFO", "wal", f"replayed {len(pending)} WAL bars")
                    else:
                        await asyncio.to_thread(
                            self.wal.mark_retry, ids, base_seconds=self.retry_seconds,
                        )
                await asyncio.sleep(self.retry_seconds)
            except asyncio.CancelledError:
                return
            except Exception as exc:
                self.last_error = str(exc)
                log.warning("WAL replay failed: %s", exc)
                try:
                    pending = await asyncio.to_thread(self.wal.pending, 5000)
                    await asyncio.to_thread(
                        self.wal.mark_retry, [row_id for row_id, _, _ in pending],
                        base_seconds=self.retry_seconds,
                    )
                except Exception:
                    pass
                await asyncio.sleep(self.retry_seconds)

    # ------------------------------------------------------------------
    async def load_recent(self, symbol: str, interval: str, limit: int = 200) -> list[Kline]:
        query = f"""
            SELECT symbol, interval, open_time, close_time, open, high, low, close,
                   volume, quote_volume, trades, taker_buy_base, taker_buy_quote, ingest_time
            FROM {self.database}.klines FINAL
            WHERE symbol = {{symbol:String}} AND interval = {{interval:String}}
            ORDER BY open_time DESC
            LIMIT {{limit:UInt32}}
        """
        rows = await asyncio.to_thread(
            self._query_sync, query,
            parameters={"symbol": symbol.upper(), "interval": interval, "limit": int(limit)},
        )
        bars = [Kline.from_ch_row(tuple(r)) for r in rows.result_rows]
        bars.reverse()
        return bars

    async def insert_trade(self, trade: dict[str, Any]) -> None:
        columns = [
            "trade_id", "signal_id", "symbol", "interval", "direction", "entry_time",
            "exit_time", "entry", "exit", "stop", "target", "quantity", "notional",
            "pnl", "fees", "r_multiple", "exit_reason", "created_at",
        ]
        values = [(
            trade["trade_id"], trade["signal_id"], trade["symbol"], trade["interval"],
            trade["direction"], trade["entry_time_dt"], trade["exit_time_dt"],
            float(trade["entry"]), float(trade["exit"]), float(trade["stop"]),
            float(trade["target"]), float(trade["quantity"]), float(trade["notional"]),
            float(trade["pnl"]), float(trade["fees"]), float(trade["r_multiple"]),
            trade["exit_reason"], trade["created_at_dt"],
        )]
        try:
            await asyncio.to_thread(
                self._insert_sync, f"{self.database}.paper_trades", values, columns,
            )
        except Exception as exc:
            log.warning("paper trade persistence failed: %s", exc)

    async def insert_signal(self, signal: dict[str, Any]) -> None:
        columns = [
            "signal_id", "symbol", "interval", "bar_time", "created_at", "direction",
            "entry", "stop", "target", "rr", "grade", "quality", "status",
            "actionable", "executed", "reason",
        ]
        values = [(
            signal["id"], signal["symbol"], signal["interval"], signal["bar_time_dt"],
            signal["created_at_dt"], signal["direction"], float(signal["entry"]),
            float(signal["stop"]), float(signal["target"]), float(signal["rr"]),
            int(signal["grade"]), float(signal["quality"]), signal["status"],
            int(bool(signal["actionable"])), int(bool(signal["executed"])),
            signal.get("reason", ""),
        )]
        try:
            await asyncio.to_thread(
                self._insert_sync, f"{self.database}.signals", values, columns,
            )
        except Exception as exc:
            log.warning("signal persistence failed: %s", exc)

    async def ping(self) -> tuple[bool, float]:
        start = time.perf_counter()
        try:
            await asyncio.to_thread(self._command_sync, "SELECT 1")
            return True, (time.perf_counter() - start) * 1000.0
        except Exception:
            return False, (time.perf_counter() - start) * 1000.0

    async def count(self) -> int | None:
        try:
            result = await asyncio.to_thread(
                self._query_sync, f"SELECT count() FROM {self.database}.klines"
            )
            return int(result.result_rows[0][0])
        except Exception:
            return None
