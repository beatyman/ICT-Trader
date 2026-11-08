"""自适应 RSI + 背离检测验收测试。"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ict_trader.adaptive_rsi import rsi, adaptive_rsi, detect_divergence  # noqa: E402


def _df(n=600, seed=7):
    rng = np.random.RandomState(seed)
    close = 100 + np.cumsum(rng.normal(0, 1, n))
    open_ = np.r_[close[0], close[:-1]]
    high = np.maximum(open_, close) + abs(rng.normal(0, 0.5, n))
    low = np.minimum(open_, close) - abs(rng.normal(0, 0.5, n))
    return pd.DataFrame({"date": pd.date_range("2026-01-01", periods=n, freq="D"),
                         "open": open_, "high": high, "low": low, "close": close,
                         "volume": np.full(n, 100.0)})


def test_rsi_bounds():
    close = _df(100)["close"].to_numpy()
    r = rsi(close, 14)
    valid = r[~np.isnan(r)]
    assert len(valid) > 0
    assert valid.min() >= 0 and valid.max() <= 100


def test_adaptive_rsi_regime_and_thresholds():
    df = _df(600)
    out = adaptive_rsi(df, length=18, adapt_lookback=300)
    assert "osc" in out and "upper_thr" in out and "lower_thr" in out and "regime" in out
    # 阈值有效（末根柱状图已满，上百分位应 ≥ 下百分位）
    mask = out["upper_thr"].notna() & out["lower_thr"].notna()
    assert (out.loc[mask, "upper_thr"] >= out.loc[mask, "lower_thr"]).all()
    assert out.loc[mask, "upper_thr"].iloc[-1] > out.loc[mask, "lower_thr"].iloc[-1]
    assert set(out["regime"].unique()) <= {-1, 0, 1}


def test_divergence_returns_list():
    df = _df(600)
    out = adaptive_rsi(df, length=18, adapt_lookback=300)
    divs = detect_divergence(df, out["osc"].to_numpy(), left=15, right=15)
    assert isinstance(divs, list)
    for d in divs:
        assert d.kind in ("bullish", "bearish")
        assert d.bar >= 0


def test_rsi_increasing_market_high():
    # 单边上涨 → RSI 高位
    close = np.linspace(100, 200, 200)
    r = rsi(close, 14)
    assert r[-1] > 50
