from __future__ import annotations

import asyncio

import pytest

from ict_trader.realtime.analysis import EngineSignal, PriceZone
from ict_trader.realtime.config import Settings, validate_ict_settings
from ict_trader.realtime.setups import SetupManager


def signal():
    return EngineSignal(
        engine="ict", role="primary", symbol="BTCUSDT", interval="15m", direction="long",
        signal_type="chan_bsp", trigger_price_zone=PriceZone(99.0, 101.0),
        invalidation_price=95.0, confidence=0.7,
        source_kline_open_time=1_700_000_000_000 // 900_000 * 900_000,
        engine_version="test", evidence={"target": 110.0},
    )


def test_shadow_never_opens():
    rows, events, entered = [], [], []

    async def persist_setup(row):
        rows.append(row)

    async def persist_event(row):
        events.append(row)

    async def on_entry(setup):
        entered.append(setup)
        raise AssertionError("shadow must not call on_entry")

    async def run():
        manager = SetupManager(
            persist_setup, persist_event, on_entry=on_entry,
            settings=Settings(ict_setup_shadow=True, ict_setup_enabled=True),
        )
        setup = await manager.on_signal(signal(), now=1_700_000_100_000)
        await manager.on_price("BTCUSDT", 100.0, interval="15m",
                               high=100.5, low=98.8, now=1_700_000_200_000)
        assert setup.status == "DETECTED"
        assert setup.shadow is True
        assert setup.execution_mode == "shadow"
        assert setup.fill_price is None
        assert entered == []
        assert [e["type"] for e in manager.events] == ["SETUP_DETECTED", "SHADOW_ENTRY"]

    asyncio.run(run())


def test_settings_validate_rejects_live_legacy():
    with pytest.raises(RuntimeError):
        validate_ict_settings(Settings(
            ict_setup_enabled=True, ict_setup_shadow=False,
            ict_entry_policy="legacy",
        ))


def test_setup_row_length_unchanged():
    manager = SetupManager(
        lambda r: asyncio.sleep(0), lambda r: asyncio.sleep(0),
        settings=Settings(ict_setup_enabled=True, ict_setup_shadow=True),
    )
    setup = asyncio.run(manager.on_signal(signal(), now=1_700_000_100_000))
    assert len(setup.setup_row(__import__("datetime").datetime.now(
        __import__("datetime").timezone.utc))) == 35
