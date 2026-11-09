from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from ict_trader.realtime.metrics import Metrics
from ict_trader.realtime.storage.batch_writer import AnalyticsBatchWriter
from ict_trader.realtime.storage.wal import Wal


def test_analytics_writer_batches_rows(tmp_path):
    calls = []

    def insert(table, rows, columns):
        calls.append((table, rows, columns))

    wal = Wal(tmp_path / "wal.sqlite3")
    writer = AnalyticsBatchWriter(insert_fn=insert, wal=wal, metrics=Metrics(),
                                  batch_size=100, flush_seconds=0.05, retry_seconds=1)

    async def run():
        await writer.start()
        await writer.enqueue("signals", ["a", "b"], [(1, 2), (3, 4)])
        await writer.enqueue("signals", ["a", "b"], [(5, 6)])
        await asyncio.sleep(0.2)
        await writer.stop()

    asyncio.run(run())
    assert len(calls) == 1
    assert calls[0][0] == "signals"
    assert calls[0][1] == [(1, 2), (3, 4), (5, 6)]
    assert wal.stats()["analytics_pending"] == 0
    wal.close()


def test_analytics_writer_failure_goes_to_wal_and_replays(tmp_path):
    state = {"fail": True, "rows": []}

    def insert(table, rows, columns):
        if state["fail"]:
            raise RuntimeError("clickhouse down")
        state["rows"].extend(rows)

    wal = Wal(tmp_path / "wal.sqlite3")
    writer = AnalyticsBatchWriter(insert_fn=insert, wal=wal, metrics=Metrics(),
                                  batch_size=100, flush_seconds=0.05, retry_seconds=1)

    async def run():
        await writer.start()
        now = datetime.now(timezone.utc)
        await writer.enqueue("signals", ["a", "at"], [(1, now)])
        await asyncio.sleep(0.25)
        assert wal.stats()["analytics_pending"] == 1
        state["fail"] = False
        await asyncio.sleep(2.2)
        await writer.stop()

    asyncio.run(run())
    assert state["rows"]
    assert isinstance(state["rows"][0][1], datetime)
    assert wal.stats()["analytics_pending"] == 0
    wal.close()
