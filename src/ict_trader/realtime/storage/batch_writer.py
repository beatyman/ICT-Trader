"""Generic buffered writer for low-frequency analytics rows.

K-lines keep their specialised high-throughput writer.  Signals, setups, AI
decisions, TP/SL events, errors and engine samples go through this second
buffer so request handlers never issue single-row INSERTs.

Rows are grouped by ``(table, columns)`` and flushed by size or time.  A failed
group is appended to the SQLite ``analytics_queue`` WAL and retried with
exponential backoff.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Callable, Iterable

from ..metrics import Metrics
from .wal import Wal

log = logging.getLogger(__name__)


@dataclass(slots=True)
class WriteBatch:
    table: str
    columns: list[str]
    rows: list[tuple]


class AnalyticsBatchWriter:
    def __init__(
        self,
        *,
        insert_fn: Callable[[str, list[tuple], list[str]], None],
        wal: Wal,
        metrics: Metrics,
        batch_size: int = 500,
        flush_seconds: float = 2.0,
        retry_seconds: float = 5.0,
    ) -> None:
        self.insert_fn = insert_fn
        self.wal = wal
        self.metrics = metrics
        self.batch_size = max(1, int(batch_size))
        self.flush_seconds = max(0.1, float(flush_seconds))
        self.retry_seconds = max(1.0, float(retry_seconds))
        self.queue: asyncio.Queue[WriteBatch] = asyncio.Queue()
        self._writer_task: asyncio.Task | None = None
        self._retry_task: asyncio.Task | None = None
        self._stopping = False
        self.last_error = ""

    @property
    def queue_size(self) -> int:
        return self.queue.qsize()

    async def start(self) -> None:
        self._stopping = False
        self._writer_task = asyncio.create_task(self._writer_loop(), name="analytics-writer")
        self._retry_task = asyncio.create_task(self._retry_loop(), name="analytics-wal-retry")

    async def stop(self) -> None:
        self._stopping = True
        if self._writer_task is not None:
            try:
                await asyncio.wait_for(self.queue.join(), timeout=10.0)
            except asyncio.TimeoutError:
                log.warning("analytics writer did not drain within 10s")
            self._writer_task.cancel()
            self._writer_task = None
        if self._retry_task is not None:
            self._retry_task.cancel()
            self._retry_task = None

    async def enqueue(self, table: str, columns: list[str],
                      rows: Iterable[tuple]) -> int:
        items = list(rows)
        if not items:
            return 0
        await self.queue.put(WriteBatch(table=table, columns=list(columns), rows=items))
        return len(items)

    async def _writer_loop(self) -> None:
        while not self._stopping:
            try:
                first = await self.queue.get()
            except asyncio.CancelledError:
                return
            pending = [first]
            rows = len(first.rows)
            deadline = time.monotonic() + self.flush_seconds
            while rows < self.batch_size:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    item = await asyncio.wait_for(self.queue.get(), timeout=remaining)
                except asyncio.TimeoutError:
                    break
                pending.append(item)
                rows += len(item.rows)

            groups: dict[tuple[str, tuple[str, ...]], list[WriteBatch]] = defaultdict(list)
            for item in pending:
                groups[(item.table, tuple(item.columns))].append(item)
            for (table, columns), items in groups.items():
                flat = [row for item in items for row in item.rows]
                await self._insert(table, list(columns), flat, wal_on_failure=True)
            for _ in pending:
                self.queue.task_done()

    async def _insert(self, table: str, columns: list[str],
                      rows: list[tuple], *, wal_on_failure: bool) -> bool:
        started = time.perf_counter()
        try:
            await asyncio.to_thread(self.insert_fn, table, rows, columns)
            self.metrics.observe("analytics_batch_ms", (time.perf_counter() - started) * 1000.0)
            self.metrics.inc("ch_analytics_row", len(rows))
            self.metrics.inc("ch_analytics_batch")
            self.last_error = ""
            return True
        except Exception as exc:
            self.last_error = str(exc)
            if wal_on_failure:
                await asyncio.to_thread(self.wal.add_analytics, table, columns, rows)
            self.metrics.inc("ch_analytics_failed_rows", len(rows))
            suffix = "queued in WAL" if wal_on_failure else "retry deferred"
            self.metrics.event(
                "ERROR", "clickhouse",
                f"analytics insert failed: {table} {len(rows)} rows {suffix}",
                error=str(exc)[:300],
            )
            log.error("analytics insert failed table=%s rows=%d: %s", table, len(rows), exc)
            return False

    async def _retry_loop(self) -> None:
        while not self._stopping:
            try:
                pending = await asyncio.to_thread(self.wal.pending_analytics, 5000)
                if pending:
                    groups: dict[tuple[str, tuple[str, ...]], list[tuple[int, tuple]]] = defaultdict(list)
                    for row_id, table, columns, row, _attempts in pending:
                        groups[(table, tuple(columns))].append((row_id, row))
                    for (table, columns), items in groups.items():
                        ok = await self._insert(
                            table, list(columns), [row for _, row in items],
                            wal_on_failure=False,
                        )
                        ids = [row_id for row_id, _ in items]
                        if ok:
                            await asyncio.to_thread(self.wal.delete_analytics, ids)
                            self.metrics.inc("wal_analytics_replayed", len(ids))
                            self.metrics.event("INFO", "wal", f"replayed {len(ids)} analytics rows")
                        else:
                            await asyncio.to_thread(
                                self.wal.mark_analytics_retry, ids,
                                base_seconds=self.retry_seconds,
                            )
                await asyncio.sleep(self.retry_seconds)
            except asyncio.CancelledError:
                return
            except Exception as exc:
                self.last_error = str(exc)
                log.warning("analytics WAL replay failed: %s", exc)
                await asyncio.sleep(self.retry_seconds)
