"""标准化订单流 + 隐藏背离验收测试。"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ict_trader.orderflow import (order_flow, standardized_order_flow,
                                  order_velocity, detect_divergence)  # noqa: E402


def _df(n=600, seed=7):
    rng = np.random.RandomState(seed)
    close = 100 + np.cumsum(rng.normal(0, 1, n))
    open_ = np.r_[close[0], close[:-1]]
    high = np.maximum(open_, close) + abs(rng.normal(0, 0.5, n))
    low = np.minimum(open_, close) - abs(rng.normal(0, 0.5, n))
    return pd.DataFrame({"date": pd.date_range("2026-01-01", periods=n, freq="D"),
                         "open": open_, "high": high, "low": low, "close": close,
                         "volume": rng.uniform(50, 200, n)})


def test_order_flow_sign_matches_direction():
    df = _df(200)
    # 单边上涨 → 订单流为正
    up = df.copy()
    up["close"] = np.linspace(100, 150, 200)
    up["high"] = up["close"] + 1
    up["low"] = up["close"] - 1
    up["open"] = np.r_[up["close"].iloc[0], up["close"].iloc[:-1].to_numpy()]
    of = order_flow(up, window=12)
    assert of.iloc[-1] > 0


def test_standardized_order_flow_zscore_like():
    df = _df(600)
    s = standardized_order_flow(df)
    assert s.isna().sum() < len(s)  # 前段允许 nan
    # 标准化后大致在 z-score 量级（极端值有限）
    valid = s.dropna()
    assert abs(valid.mean()) < 0.5


def test_order_velocity_returns_series():
    df = _df(600)
    v = order_velocity(df)
    assert len(v) == len(df)


def test_divergence_kinds_valid():
    df = _df(600)
    s = standardized_order_flow(df).to_numpy()
    divs = detect_divergence(df, s, left=30, right=1)
    for d in divs:
        assert d.kind in ("bullish", "bearish", "hidden_bullish", "hidden_bearish")
