"""市场状态 + SuperTrend 验收测试。"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ict_trader.synapse import (choppiness_index, r_squared, adx,
                                regime_classify, super_trend)  # noqa: E402


def _df(n=400, seed=7):
    rng = np.random.RandomState(seed)
    close = 100 + np.cumsum(rng.normal(0, 1, n))
    open_ = np.r_[close[0], close[:-1]]
    high = np.maximum(open_, close) + abs(rng.normal(0, 0.5, n))
    low = np.minimum(open_, close) - abs(rng.normal(0, 0.5, n))
    return pd.DataFrame({"date": pd.date_range("2026-01-01", periods=n, freq="D"),
                         "open": open_, "high": high, "low": low, "close": close,
                         "volume": np.full(n, 100.0)})


def test_choppiness_bounds():
    df = _df(400)
    c = choppiness_index(df, 14)
    valid = c[~np.isnan(c)]
    assert valid.min() >= 0 and valid.max() <= 100


def test_r_squared_bounds():
    df = _df(400)
    r2 = r_squared(df["close"].to_numpy(), 50)
    valid = r2[~np.isnan(r2)]
    assert valid.min() >= 0 and valid.max() <= 1.0 + 1e-9


def test_adx_bounds():
    df = _df(400)
    a = adx(df, 14)
    assert a.max() >= 0 and a.max() <= 100


def test_market_regime_labels():
    df = _df(400)
    m = regime_classify(df)
    assert "regime_score" in m and "regime_label" in m
    assert set(m["regime_label"].unique()) <= {"Trending", "Choppy", "Mixed"}


def test_super_trend_direction_and_trail():
    df = _df(400)
    st = super_trend(df)
    assert set(st["direction"].unique()) <= {-1, 0, 1}
    # trail 在非零方向时有限
    active = st["trail"].dropna()
    assert active.notna().all() and len(active) > 0
    # 棘轮：单段连续多头内，下轨只升不降（跨段可能重置）
    d = st["direction"].to_numpy()
    lo = st["lower"].to_numpy()
    run_start = None
    for i in range(1, len(d)):
        if d[i] == 1 and not np.isnan(lo[i]) and not np.isnan(lo[i - 1]):
            if d[i - 1] == 1:
                assert lo[i] >= lo[i - 1] - 1e-9
