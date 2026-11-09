from __future__ import annotations

import asyncio

from ict_trader.realtime.analysis import EngineSignal, PriceZone
from ict_trader.realtime.setups import SetupManager


def signal(evidence: dict | None = None) -> EngineSignal:
    return EngineSignal(
        engine="wyckoff", symbol="BTCUSDT", interval="15m", direction="long",
        signal_type="wyckoff_spring", trigger_price_zone=PriceZone(99.0, 101.0),
        invalidation_price=95.0, confidence=0.7,
        source_kline_open_time=1_700_000_000_000 // 900_000 * 900_000,
        engine_version="wyckoff-test",
        evidence=evidence or {"target": 110.0},
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
        await manager.on_price("BTCUSDT", 100.0, interval="15m", high=100.5, low=98.8,
                               now=1_700_000_200_000)
        assert setup.status == "OPEN"
        await manager.on_price("BTCUSDT", 110.0, interval="15m", high=110.5, low=109.5,
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
            await manager.on_bar("BTCUSDT", interval="15m", high=102.0, low=101.5, close=101.8,
                                 now=1_700_000_200_000 + i * 900_000)
        assert setup.status == "EXPIRED"
        assert setup.bars_alive == 5

    asyncio.run(run())
    assert rows


def test_setup_records_resonance_and_structural_invalidation():
    rows = []

    async def persist_setup(row):
        rows.append(row)

    async def persist_event(row):
        rows.append(row)

    async def run():
        from ict_trader.realtime.analysis.auxiliary import build_auxiliary_context
        s = signal({"target": 110.0, "structural_invalidation_price": 98.0})
        aux = build_auxiliary_context("long", htf_bias="bullish",
                                      chan_bias="bullish", ichimoku_bias="bearish",
                                      wyckoff_bias="bullish")
        manager = SetupManager(persist_setup, persist_event, max_bars=10)
        setup = await manager.on_signal(s, now=1_700_000_100_000, resonance=aux)
        assert setup.resonance_score == 3
        assert setup.structural_invalidation_price == 98.0
        await manager.on_price("BTCUSDT", 100.0, interval="15m", high=100.5,
                               low=98.8, now=1_700_000_200_000)
        assert setup.status == "OPEN"
        await manager.on_price("BTCUSDT", 97.0, interval="15m", high=97.5,
                               low=96.5, now=1_700_000_300_000)
        assert setup.status == "INVALIDATED"
        assert setup.structural_invalidation_status == "hit"

    asyncio.run(run())


def test_short_waits_for_upper_edge_and_uses_actual_fill():
    rows = []

    async def persist_setup(row):
        rows.append(row)

    async def persist_event(row):
        rows.append(row)

    async def run():
        s = EngineSignal(
            engine="wyckoff", symbol="ETHUSDT", interval="15m", direction="short",
            signal_type="wyckoff_upthrust", trigger_price_zone=PriceZone(2151.26, 2235.24),
            invalidation_price=2245.99, confidence=0.9,
            source_kline_open_time=1_700_000_000_000 // 900_000 * 900_000, engine_version="test",
            evidence={"target": 2060.0},
        )
        manager = SetupManager(persist_setup, persist_event, max_bars=40)
        setup = await manager.on_signal(s, now=1_700_000_100_000)
        assert setup.entry == 2235.24
        await manager.on_price("ETHUSDT", 2151.26, interval="15m",
                               high=2158.19, low=2128.0, now=1_700_000_200_000)
        assert setup.status == "DETECTED"
        await manager.on_price("ETHUSDT", 2235.24, interval="15m",
                               high=2236.0, low=2200.0, now=1_700_000_300_000)
        assert setup.status == "OPEN"
        assert setup.fill_price == 2235.24
        assert setup.rr > 2.0

    asyncio.run(run())


def test_setup_requires_current_ltf_structure_alignment():
    rows = []

    async def persist_setup(row):
        rows.append(row)

    async def persist_event(row):
        rows.append(row)

    async def run():
        manager = SetupManager(persist_setup, persist_event)
        blocked = await manager.on_signal(signal(), structure_bias="bearish")
        assert blocked is None
        allowed = await manager.on_signal(signal(), structure_bias="bullish")
        assert allowed is not None
        assert allowed.structure_bias == "bullish"

    asyncio.run(run())


def test_setup_targets_are_sorted_and_rr_uses_fill_edge():
    rows = []

    async def persist_setup(row):
        rows.append(row)

    async def persist_event(row):
        rows.append(row)

    async def run():
        s = EngineSignal(
            engine="wyckoff", symbol="ETHUSDT", interval="15m", direction="short",
            signal_type="wyckoff_upthrust", trigger_price_zone=PriceZone(2151.26, 2235.24),
            invalidation_price=2245.99, confidence=0.9,
            source_kline_open_time=1_700_000_000_000 // 900_000 * 900_000,
            engine_version="test",
            evidence={"target": 2060.0, "targets": [2060.0, 2139.3, 2139.7]},
        )
        manager = SetupManager(persist_setup, persist_event, max_bars=40)
        setup = await manager.on_signal(s, now=1_700_000_100_000)
        assert setup.tp_levels == [2139.7, 2060.0]
        assert setup.rr_for(2139.7) > 2.0
        assert setup.rr_for(2060.0) > 2.0
        assert setup.targets == [2060.0, 2139.3, 2139.7]

    asyncio.run(run())


def test_setup_sends_accepted_entry_to_paper_callback():
    entered = []

    async def persist(row):
        pass

    async def on_entry(setup):
        entered.append(setup)
        return True

    async def run():
        manager = SetupManager(persist, persist, on_entry=on_entry)
        setup = await manager.on_signal(signal(), now=1_700_000_100_000)
        await manager.on_price("BTCUSDT", 100.0, interval="15m",
                               high=100.5, low=98.8, now=1_700_000_200_000)
        assert setup.status == "OPEN"
        assert setup.fill_price == 99.0
        assert entered and entered[0] is setup

    asyncio.run(run())
