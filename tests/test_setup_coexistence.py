from __future__ import annotations

import asyncio

from ict_trader.realtime.analysis import EngineSignal, PriceZone
from ict_trader.realtime.config import Settings
from ict_trader.realtime.setups import SetupManager


def signal(engine: str, offset: int = 0) -> EngineSignal:
    return EngineSignal(
        engine=engine, symbol="BTCUSDT", interval="15m", direction="long",
        signal_type=f"{engine}_signal", trigger_price_zone=PriceZone(99, 101),
        invalidation_price=95, confidence=0.7,
        source_kline_open_time=1_700_000_000_000 // 900_000 * 900_000 + offset,
        engine_version="test", evidence={"target": 110},
    )


def test_second_setup_waits_then_opens_after_conflict():
    entries = []
    opened = False

    async def persist_setup(row):
        pass

    async def persist_event(row):
        pass

    async def on_entry(setup):
        nonlocal opened
        entries.append(setup)
        if opened:
            return None
        opened = True
        return True

    async def run():
        manager = SetupManager(
            persist_setup, persist_event, on_entry=on_entry,
            settings=Settings(ict_setup_enabled=True,
                              ict_setup_shadow=False,
                              ict_entry_policy="strict"),
        )
        first = await manager.on_signal(signal("ict", 0))
        second = await manager.on_signal(signal("ict", 900_000))
        assert first and second
        await manager.on_price("BTCUSDT", 100, interval="15m",
                               high=100.5, low=99, now=1_700_000_200_000)
        assert first.status == "OPEN"
        assert second.status == "DETECTED"
        assert second.entry_pending_reason == "position_conflict"
        assert [e["type"] for e in manager.events].count("ENTRY_WAIT") >= 1

    asyncio.run(run())
