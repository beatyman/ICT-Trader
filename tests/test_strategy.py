import numpy as np
import pandas as pd

from ict_trader.cisd import CISD
from ict_trader.concepts import ICTConfig, ICTResult
from ict_trader.strategy import StrategyConfig, simulate_cisd


def frame(prices):
    n = len(prices)
    return pd.DataFrame({
        "date": pd.date_range("2026-01-01", periods=n, freq="15min"),
        "open": prices,
        "high": [p + 1 for p in prices],
        "low": [p - 1 for p in prices],
        "close": prices,
        "volume": [1000] * n,
    })


def test_entry_next_bar_and_tp():
    df = frame([90, 95, 100, 102, 108, 111, 112, 110, 109, 108])
    c = CISD("bull", level=100, extreme=95, body_extreme=95, start=1, end=2,
             sl=95, target=110, grade=4, checks={"sweep": True})
    res = ICTResult(bars=df, cisds=[c], config=ICTConfig())
    r = simulate_cisd(df, res, StrategyConfig(min_grade=0, min_confluence=0,
                                              entry_delay=1, max_bars=5,
                                              min_rr=1.0), "test")
    assert len(r.trades) == 1
    t = r.trades[0]
    assert t.entry_time == df.date.iloc[3]
    assert t.entry > df.open.iloc[3]
    assert t.exit >= 110
    assert t.r_multiple > 0


def test_no_overlapping_trade():
    df = frame([90, 95, 100, 102, 108, 111, 112, 110, 109, 108])
    c1 = CISD("bull", level=100, extreme=95, body_extreme=95, start=1, end=2,
              sl=95, target=110, grade=4, checks={"sweep": True})
    c2 = CISD("bull", level=101, extreme=96, body_extreme=96, start=2, end=3,
              sl=96, target=112, grade=4, checks={"sweep": True})
    res = ICTResult(bars=df, cisds=[c1, c2], config=ICTConfig())
    r = simulate_cisd(df, res, StrategyConfig(min_grade=0, min_confluence=0,
                                              entry_delay=1, max_bars=10), "test")
    assert len(r.trades) == 1


def test_retest_entry_fills_at_key():
    df = frame([90, 95, 100, 102, 99, 105, 110, 112, 111, 110])
    c = CISD("bull", level=100, extreme=95, body_extreme=95, start=1, end=2,
             sl=95, target=110, grade=4, checks={"sweep": True})
    res = ICTResult(bars=df, cisds=[c], config=ICTConfig())
    r = simulate_cisd(df, res, StrategyConfig(min_grade=0, min_confluence=0,
                                              entry_mode="retest_key",
                                              retest_max_bars=5, max_bars=5), "test")
    assert len(r.trades) == 1
    t = r.trades[0]
    assert t.entry_time == df.date.iloc[4]
    assert 99.0 <= t.entry <= 100.01
    assert r.metrics["not_triggered"] == 0


def test_retest_entry_not_filled_is_recorded():
    df = frame([90, 95, 100, 102, 108, 111, 112, 110, 109, 108])
    c = CISD("bull", level=100, extreme=95, body_extreme=95, start=1, end=2,
             sl=95, target=110, grade=4, checks={"sweep": True})
    res = ICTResult(bars=df, cisds=[c], config=ICTConfig())
    r = simulate_cisd(df, res, StrategyConfig(min_grade=0, min_confluence=0,
                                              entry_mode="retest_key",
                                              retest_max_bars=3, max_bars=5), "test")
    assert len(r.trades) == 0
    assert r.metrics["not_triggered"] == 1
    assert r.metrics["signals"] == 1


def test_low_measured_target_falls_back_to_two_r():
    df = frame([90, 95, 100, 102, 108, 111, 112, 110, 109, 108])
    c = CISD("bull", level=100, extreme=95, body_extreme=95, start=1, end=2,
             sl=95, target=103, grade=4, checks={"sweep": True})
    res = ICTResult(bars=df, cisds=[c], config=ICTConfig())
    r = simulate_cisd(df, res, StrategyConfig(min_grade=0, min_confluence=0,
                                              entry_delay=1, max_bars=5,
                                              min_rr=2.0), "test")
    assert len(r.trades) == 1
    t = r.trades[0]
    assert t.features["entry_rr"] >= 2.0
    assert t.tp > 110


def test_atr_trailing_exit():
    df = frame([90, 95, 100, 102, 108, 112, 108, 104, 100])
    c = CISD("bull", level=100, extreme=95, body_extreme=95, start=1, end=2,
             sl=95, target=130, grade=4, checks={"sweep": True})
    res = ICTResult(bars=df, cisds=[c], config=ICTConfig())
    r = simulate_cisd(df, res, StrategyConfig(min_grade=0, min_confluence=0,
                                              trail_activate_r=0.5,
                                              trail_atr_mult=1.0,
                                              max_bars=8), "test")
    assert len(r.trades) == 1
    t = r.trades[0]
    assert t.reason == "trail"
    assert t.exit > t.entry


def test_daily_loss_gate_blocks_next_entry():
    df = frame([100, 100, 100, 98, 97, 100, 100, 100])
    c1 = CISD("bull", level=100, extreme=99, body_extreme=99, start=1, end=1,
              sl=99, target=105, grade=4, checks={"sweep": True})
    c2 = CISD("bull", level=100, extreme=96, body_extreme=96, start=4, end=4,
              sl=96, target=110, grade=4, checks={"sweep": True})
    res = ICTResult(bars=df, cisds=[c1, c2], config=ICTConfig())
    r = simulate_cisd(df, res, StrategyConfig(min_grade=0, min_confluence=0,
                                              max_daily_loss_r=1.0,
                                              max_bars=3), "test")
    assert len(r.trades) == 1
    assert r.metrics["skipped_daily_loss"] == 1


def test_a_share_t_plus_1_cannot_exit_same_day():
    # two sessions; entry on day 1, same-day stop must wait for day 2
    dates = pd.to_datetime([
        "2026-01-05 14:00", "2026-01-05 14:15", "2026-01-05 14:30",
        "2026-01-05 14:45", "2026-01-05 15:00",
        "2026-01-06 09:45", "2026-01-06 10:00", "2026-01-06 10:15",
    ])
    prices = [100, 100, 100, 90, 88, 89, 91, 92]
    df = pd.DataFrame({
        "date": dates, "open": prices, "high": [p + 1 for p in prices],
        "low": [p - 1 for p in prices], "close": prices, "volume": [1000] * len(prices),
    })
    c = CISD("bull", level=100, extreme=90, body_extreme=90, start=0, end=1,
             sl=95, target=120, grade=4, checks={"sweep": True})
    res = ICTResult(bars=df, cisds=[c], config=ICTConfig())
    r = simulate_cisd(df, res, StrategyConfig(min_grade=0, min_confluence=0,
                                              entry_delay=1, max_bars=10,
                                              t_plus_1=True, sell_tax_bps=5), "test")
    assert len(r.trades) == 1
    t = r.trades[0]
    assert t.entry_time.date() != t.exit_time.date()
    assert t.reason in {"gap_sl", "sl"}
    assert t.t1_locked is False
    assert t.mae_r < 0

