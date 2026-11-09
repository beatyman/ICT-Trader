from __future__ import annotations

import asyncio

from ict_trader.realtime.analysis import EngineSignal, PriceZone
from ict_trader.realtime.setups import SetupManager


def signal() -> EngineSignal:
    return EngineSignal(
        engine="wyckoff", symbol="BTCUSDT", interval="15m", direction="long",
        signal_type="wyckoff_spring", trigger_price_zone=PriceZone(99.0, 101.0),
        invalidation_price=95.0, confidence=0.7,
        source_kline_open_time=1_700_000_000_000 // 900_000 * 900_000,
        engine_version="wyckoff-test",
        evidence={"target": 110.0},
    )


def test_setup_lifecycle_persists_transitions():
    rows = []
    events = []

    async def persist_setup(row):
        rows.append(row)

    async def persist_event(row):
        events.append(row)

    async def run():
        manager = SetupManager(persist_setup, persist_event, max_bars=3)
        setup = await manager.on_signal(signal(), now=1_700_000_100_000)
        assert setup is not None
        assert setup.status == "DETECTED"
        await manager.on_price("BTCUSDT", 100.0, high=100.5, low=99.5,
                               now=1_700_000_200_000)
        assert setup.status == "OPEN"
        await manager.on_price("BTCUSDT", 110.0, high=110.5, low=109.5,
                               now=1_700_000_300_000)
        assert setup.status == "CLOSED"
        assert setup.exit_price == 110.0

    asyncio.run(run())
    # DETECTED + OPEN + CLOSED setup snapshots
    assert len(rows) == 3
    assert [r[4] for r in rows] == ["DETECTED", "OPEN", "CLOSED"]
    assert [r[4] for r in events] == ["ENTRY", "TP"]


def test_setup_expires_without_trigger():
    rows = []

    async def persist_setup(row):
        rows.append(row)

    async def persist_event(row):
        rows.append(row)

    async def run():
        manager = SetupManager(persist_setup, persist_event, max_bars=4)
        setup = await manager.on_signal(signal(), now=1_700_000_100_000)
        for i in range(5):
            await manager.on_bar("BTCUSDT", high=102.0, low=101.5, close=101.8,
                                 now=1_700_000_200_000 + i * 900_000)
        assert setup.status == "EXPIRED"
        assert setup.bars_alive == 5

    asyncio.run(run())
    assert rows
