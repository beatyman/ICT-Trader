"""Time Machine + Risk Manager（从 day-trader-skill 吸收）验收测试。"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ict_trader.timemachine import time_machine  # noqa: E402
from ict_trader.trade_risk import risk_check  # noqa: E402


def _df(n=200, seed=7):
    rng = np.random.RandomState(seed)
    close = 100 + np.cumsum(rng.normal(0, 1, n))
    open_ = np.r_[close[0], close[:-1]]
    high = np.maximum(open_, close) + abs(rng.normal(0, 0.4, n))
    low = np.minimum(open_, close) - abs(rng.normal(0, 0.4, n))
    return pd.DataFrame({"date": pd.date_range("2026-01-01", periods=n, freq="15min"),
                         "open": open_, "high": high, "low": low, "close": close,
                         "volume": np.full(n, 100.0)})


def test_timemachine_reports_odds():
    df = _df(200)
    price = float(df["close"].iloc[-1])
    tm = time_machine(df, entry=price, sl=price - 1, tp=price + 2,
                      direction="long", n_bars=16, replays=500, seed=19)
    assert "tp_first_pct" in tm and "sl_first_pct" in tm and "neither_pct" in tm
    assert 0 <= tm["tp_first_pct"] <= 100
    assert "verdict" in tm and "history" in tm
    assert tm["seed"] == 19


def test_timemachine_deterministic_seed():
    df = _df(200)
    price = float(df["close"].iloc[-1])
    a = time_machine(df, entry=price, sl=price - 1, tp=price + 2, n_bars=8, replays=300, seed=19)
    b = time_machine(df, entry=price, sl=price - 1, tp=price + 2, n_bars=8, replays=300, seed=19)
    assert a["tp_first_pct"] == b["tp_first_pct"]  # 同 seed 可复现


def test_risk_check_block_low_rr():
    setup = {"id": "S1", "entry_zone": {"low": 100.0, "high": 101.0},
             "sl": 101.5, "tp": [{"level": 99.0, "rr": 1.0}]}
    rc = risk_check(setup, limits={"min_reward_risk": 2.0})
    assert rc["status"] == "BLOCK"
    assert any("reward:risk" in r for r in rc["reasons"])


def test_risk_check_pass_good_rr():
    setup = {"id": "L1", "entry_zone": {"low": 100.0, "high": 101.0},
             "sl": 98.0, "tp": [{"level": 106.0, "rr": 2.5}]}
    rc = risk_check(setup, limits={"min_reward_risk": 2.0})
    assert rc["status"] == "PASS"


def test_risk_check_block_no_stop():
    setup = {"id": "L1", "entry_zone": {"low": 100.0, "high": 101.0},
             "sl": None, "tp": [{"level": 106.0, "rr": 2.5}]}
    rc = risk_check(setup)
    assert rc["status"] == "BLOCK"
    assert any("stop" in r for r in rc["reasons"])


def test_risk_check_trades_per_day():
    setup = {"id": "L1", "entry_zone": {"low": 100.0, "high": 101.0},
             "sl": 98.0, "tp": [{"level": 106.0, "rr": 2.5}]}
    rc = risk_check(setup, placed_today={"trades": 3, "daily_loss_usd": 0},
                    limits={"max_trades_per_day": 3})
    assert rc["status"] == "BLOCK"
    assert any("trades today" in r for r in rc["reasons"])
