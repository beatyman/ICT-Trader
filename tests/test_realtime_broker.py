from __future__ import annotations

import asyncio

import pytest

from ict_trader.realtime.broker import PaperBroker
from ict_trader.realtime.config import Settings
from ict_trader.realtime.metrics import Metrics
from ict_trader.realtime.strategy_runner import TradeSignal
from ict_trader.realtime.models import utc_ms


def signal(direction: str = "long", *, stop: float = 98.0, target: float = 106.0) -> TradeSignal:
    return TradeSignal(
        id="SIG-TEST", symbol="BTCUSDT", interval="15m", bar_time=utc_ms(),
        created_at=utc_ms(), direction=direction, entry=100.0, stop=stop,
        target=target, rr=3.0, grade=4, quality=0.8, status="ARMED",
        actionable=True, executed=False, reason="unit test",
        checks={"sweep": True, "pda": True, "volume": True},
        setup_level=100.0,
    )


def broker(tmp_path, **overrides) -> PaperBroker:
    cfg = Settings(symbols=["BTCUSDT"], intervals=["15m"], mode="demo",
                   data_dir=tmp_path, **overrides)
    return PaperBroker(cfg, Metrics())


def test_fixed_fractional_sizing_and_entry_fee(tmp_path):
    b = broker(tmp_path)
    result = asyncio.run(b.execute(signal(), 100.0))
    assert result.accepted
    pos = b.positions["BTCUSDT"]
    # risk = 0.5% of 100k / ~2.02 stop distance, capped below 30% notional
    assert 240 < pos.quantity < 255
    assert pos.notional < 100_000 * 0.30
    assert b.equity < 100_000  # entry fee is paid immediately
    assert len(b.trades) == 0


def test_stop_is_hit_before_target_on_same_bar(tmp_path):
    b = broker(tmp_path)
    asyncio.run(b.execute(signal(), 100.0))
    asyncio.run(b.on_bar_closed("BTCUSDT", high=107.0, low=97.0, close=101.0))
    assert "BTCUSDT" not in b.positions
    assert len(b.trades) == 1
    trade = b.trades[0]
    assert trade.exit_reason == "stop"
    assert trade.pnl < 0
    assert trade.r_multiple < 0


def test_target_exit_and_trade_stats(tmp_path):
    b = broker(tmp_path)
    asyncio.run(b.execute(signal(), 100.0))
    asyncio.run(b.on_bar_closed("BTCUSDT", high=107.0, low=99.5, close=106.5))
    assert "BTCUSDT" not in b.positions
    trade = b.trades[0]
    assert trade.exit_reason == "target"
    assert trade.pnl > 0
    snap = b.snapshot()
    assert snap["stats"]["win_rate"] == 100.0
    assert snap["account"]["realized"] > 0


def test_one_position_per_symbol_and_min_rr(tmp_path):
    b = broker(tmp_path)
    first = asyncio.run(b.execute(signal(), 100.0))
    assert first.accepted
    second = asyncio.run(b.execute(signal(), 100.0))
    assert not second.accepted
    assert "already open" in second.reason

    b2 = broker(tmp_path / "b2")
    poor = signal(stop=99.9, target=100.1)
    result = asyncio.run(b2.execute(poor, 100.0))
    assert not result.accepted
    assert "RR" in result.reason or "order" in result.reason


def test_short_direction(tmp_path):
    b = broker(tmp_path)
    result = asyncio.run(b.execute(signal("short", stop=102.0, target=94.0), 100.0))
    assert result.accepted
    asyncio.run(b.on_price("BTCUSDT", 93.0))
    assert b.trades[-1].direction == "short"
    assert b.trades[-1].pnl > 0


def test_time_stop(tmp_path):
    b = broker(tmp_path, max_bars=2)
    sig = signal()
    asyncio.run(b.execute(sig, 100.0, bar_time=sig.bar_time))
    b.increment_bar("BTCUSDT", sig.bar_time + 900_000, 100.5)
    assert "BTCUSDT" in b.positions
    assert b.increment_bar("BTCUSDT", sig.bar_time + 2 * 900_000, 101.0) == ["BTCUSDT"]
    asyncio.run(b.close_time_stop("BTCUSDT", 101.0))
    assert b.trades[-1].exit_reason == "time"
