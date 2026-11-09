from __future__ import annotations

from ict_trader.realtime.models import Kline
from ict_trader.realtime.storage.wal import Wal


def bar(i: int) -> Kline:
    return Kline(
        symbol="ETHUSDT", interval="15m", open_time=1_700_000_000_000 + i * 900_000,
        close_time=1_700_000_000_000 + (i + 1) * 900_000 - 1,
        open=2000, high=2010, low=1990, close=2005, volume=12, is_closed=True,
    )


def test_wal_pending_delete_and_retry(tmp_path):
    wal = Wal(tmp_path / "wal.sqlite3")
    assert wal.stats()["pending"] == 0
    assert wal.add_klines([bar(0), bar(1)]) == 2
    pending = wal.pending()
    assert len(pending) == 2
    assert pending[0][1].symbol == "ETHUSDT"
    wal.delete([pending[0][0]])
    assert wal.stats()["pending"] == 1
    wal.mark_retry([pending[1][0]], base_seconds=0.01)
    assert wal.pending() == []
    wal.dead_letter("backfill", {"symbol": "ETHUSDT"}, "boom")
    assert wal.stats()["dead_letters"] == 1
    wal.close()


def test_analytics_wal_roundtrips_datetime(tmp_path):
    from datetime import datetime, timezone
    wal = Wal(tmp_path / "wal.sqlite3")
    now = datetime.now(timezone.utc)
    assert wal.add_analytics("signals", ["a", "at"], [(1, now)]) == 1
    pending = wal.pending_analytics()
    assert len(pending) == 1
    _, table, columns, row, _ = pending[0]
    assert table == "signals"
    assert columns == ["a", "at"]
    assert isinstance(row[1], datetime)
    wal.delete_analytics([pending[0][0]])
    assert wal.stats()["analytics_pending"] == 0
    wal.close()
