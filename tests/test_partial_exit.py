"""Partial scale-out (t1) added for the exit study."""
import numpy as np
import pandas as pd
import pytest

from ict_trader.cisd import CISD
from ict_trader.concepts import ICTConfig, ICTResult
from ict_trader.strategy import StrategyConfig, simulate_cisd


def frame(prices, wick=1.0):
    n = len(prices)
    return pd.DataFrame({
        "date": pd.date_range("2026-01-01", periods=n, freq="15min"),
        "open": prices,
        "high": [p + wick for p in prices],
        "low": [p - wick for p in prices],
        "close": prices,
        "volume": [1000] * n,
    })


def run(df, cfg, sl=95, target=110):
    c = CISD("bull", level=100, extreme=95, body_extreme=95, start=1, end=2,
             sl=sl, target=target, grade=4, checks={"sweep": True})
    res = ICTResult(bars=df, cisds=[c], config=ICTConfig())
    base = dict(min_grade=0, min_confluence=0, entry_delay=1, max_bars=5,
                min_rr=0.0, commission_bps=0.0, slippage_bps=0.0)
    return simulate_cisd(df, res, StrategyConfig(**{**base, **cfg}), "test")


def test_t1_off_is_the_previous_behaviour():
    df = frame([90, 95, 100, 102, 108, 111, 112, 110, 109, 108])
    t = run(df, {}).trades[0]
    assert t.t1_filled is False and t.t1_price is None
    assert t.reason == "tp"
    # entry ~102 (next open + slippage 0), stop 95 -> risk 7, target 110 -> 8/7
    assert t.r_multiple == pytest.approx((110 - t.entry) / (t.entry - 95), abs=1e-6)


def test_t1_banks_half_at_one_R_and_blends_the_R():
    # entry ~102, risk 7 -> t1 at 109; price then reaches the 110 target
    df = frame([90, 95, 100, 102, 108, 111, 112, 110, 109, 108])
    t = run(df, {"t1_at_r": 1.0, "t1_fraction": 0.5, "t1_move_be": True}).trades[0]
    assert t.t1_filled is True
    assert t.t1_price == pytest.approx(t.entry + (t.entry - 95), abs=1e-9)
    risk = t.entry - 95
    leg1 = (t.t1_price - t.entry) / risk
    leg2 = (t.exit - t.entry) / risk
    assert t.r_multiple == pytest.approx(0.5 * leg1 + 0.5 * leg2, abs=1e-9)
    assert t.r_multiple > leg1  # the runner added something


def test_t1_runner_stopped_at_breakeven_still_books_a_win():
    # entry ~102, risk 7 -> t1 at 109.  Target far away, price comes back.
    df = frame([90, 95, 100, 102, 108, 109, 108, 104, 101, 99], wick=1.0)
    t = run(df, {"t1_at_r": 1.0, "t1_fraction": 0.5, "t1_move_be": True},
            target=130).trades[0]
    assert t.t1_filled is True
    assert t.reason == "t1_be"
    assert t.r_multiple == pytest.approx(0.5, abs=1e-6)  # half kept at 1R, half flat


def test_t1_without_be_lets_the_runner_lose():
    df = frame([90, 95, 100, 102, 108, 109, 108, 104, 101, 99], wick=1.0)
    t = run(df, {"t1_at_r": 1.0, "t1_fraction": 0.5, "t1_move_be": False},
            target=130).trades[0]
    assert t.t1_filled is True
    assert t.reason == "time"          # never reached the original stop
    risk = t.entry - 95
    leg2 = (t.exit - t.entry) / risk
    assert t.r_multiple == pytest.approx(0.5 * 1.0 + 0.5 * leg2, abs=1e-9)
    assert t.r_multiple < 0.5


def test_t1_never_fires_when_price_does_not_reach_it():
    df = frame([90, 95, 100, 102, 103, 103.5, 103, 102.8, 102.5, 102.4], wick=0.4)
    t = run(df, {"t1_at_r": 1.0, "t1_fraction": 0.5}, target=120).trades[0]
    assert t.t1_filled is False
    assert t.reason == "time"


def test_fraction_is_clamped():
    df = frame([90, 95, 100, 102, 108, 111, 112, 110, 109, 108])
    t = run(df, {"t1_at_r": 1.0, "t1_fraction": 3.0, "t1_move_be": True}).trades[0]
    # fraction >1 behaves as "all out at t1": the blend must stay finite
    assert np.isfinite(t.r_multiple)
