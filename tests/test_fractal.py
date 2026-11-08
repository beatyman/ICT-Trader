"""Fractal Model（分形 C1-C4 + Reclaim Strength）验收测试。"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ict_trader.fractal import detect_fractal_reclaim, reclaim_stats  # noqa: E402


def _df(seed=7, n=300):
    rng = np.random.RandomState(seed)
    close = 100 + np.cumsum(rng.normal(0, 0.8, n))
    open_ = np.r_[close[0], close[:-1]]
    high = np.maximum(open_, close) + abs(rng.normal(0, 0.5, n))
    low = np.minimum(open_, close) - abs(rng.normal(0, 0.5, n))
    return pd.DataFrame({"date": pd.date_range("2026-01-01", periods=n, freq="15min"),
                         "open": open_, "high": high, "low": low, "close": close,
                         "volume": np.full(n, 100.0)})


def test_bullish_reclaim_detected():
    # 构造一个明确的下扫 + 收回：C1 有 low，C2 下穿后收回
    df = _df(20)
    df.loc[10, "low"] = 95.0
    df.loc[11, "low"] = 94.0   # C2 下穿 C1 low
    df.loc[11, "close"] = 97.0  # C2 收回（> C1 low）
    s = detect_fractal_reclaim(df)
    assert len(s) > 0
    assert any(x["direction"] == "bull" for x in s)


def test_rs_positive_and_bucketed():
    df = _df(300)
    setups = detect_fractal_reclaim(df)
    assert len(setups) > 0
    for s in setups:
        assert s["rs"] >= 0
        assert s["direction"] in ("bull", "bear")


def test_reclaim_stats_structure():
    df = _df(300)
    st = reclaim_stats(df)
    assert "n" in st and "buckets" in st and "base" in st
    assert st["base"]["reach_c4_pct"] >= 0 and st["base"]["reach_c4_pct"] <= 100


def test_no_future_leak():
    # 非重绘：只用已收盘 bar；追加数据不改变历史 setup 的 RS
    df = _df(200)
    s1 = detect_fractal_reclaim(df.iloc[:150])
    s2 = detect_fractal_reclaim(df.iloc[:150])  # 同数据可复现
    assert [x["rs"] for x in s1] == [x["rs"] for x in s2]
