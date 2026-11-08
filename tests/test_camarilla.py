"""Camarilla 枢轴 + VWAP 验收测试。"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ict_trader.camarilla import camarilla_pivots, vwap, vwap_band  # noqa: E402


def test_camarilla_level_ordering():
    p = camarilla_pivots(high=110.0, low=100.0, close=105.0)
    # 上方层级严格递增：H1 < H2 < H3 < H4 < H5 < H6
    for a, b in (("H1", "H2"), ("H2", "H3"), ("H3", "H4"), ("H4", "H5"), ("H5", "H6")):
        assert p[a] < p[b]
    # 下方层级严格递减：L1 > L2 > L3 > L4 > L5 > L6
    for a, b in (("L1", "L2"), ("L2", "L3"), ("L3", "L4"), ("L4", "L5"), ("L5", "L6")):
        assert p[a] > p[b]
    # Pivot 居中
    assert p["L4"] < p["pivot"] < p["H4"]


def test_camarilla_h5_l5_symmetry():
    p = camarilla_pivots(high=110.0, low=100.0, close=105.0)
    # L5 = close - (H5 - close) → H5 与 L5 关于 close 对称
    assert abs((p["H5"] - 105.0) - (105.0 - p["L5"])) < 1e-9


def test_vwap_between_high_low():
    n = 200
    rng = np.random.RandomState(0)
    close = 100 + np.cumsum(rng.normal(0, 1, n))
    high = close + 2
    low = close - 2
    df = pd.DataFrame({"high": high, "low": low, "close": close,
                       "volume": rng.uniform(50, 200, n)})
    v = vwap(df)
    assert len(v) == n
    assert v.isna().sum() == 0
    # VWAP 应始终在历史 high/low 包络内（大致）
    assert v.min() >= low.min() - 1e-6
    assert v.max() <= high.max() + 1e-6


def test_vwap_falls_back_without_volume():
    n = 50
    close = np.linspace(100, 110, n)
    df = pd.DataFrame({"high": close + 1, "low": close - 1, "close": close,
                       "volume": np.zeros(n)})
    v = vwap(df)
    assert v.isna().sum() == 0


def test_vwap_band_encloses_vwap():
    n = 200
    rng = np.random.RandomState(0)
    close = 100 + np.cumsum(rng.normal(0, 1, n))
    df = pd.DataFrame({"high": close + 2, "low": close - 2, "close": close,
                       "volume": rng.uniform(50, 200, n)})
    b = vwap_band(df)
    valid = b.dropna()
    assert len(valid) > 0
    assert (valid["upper"] >= valid["vwap"]).all()
    assert (valid["vwap"] >= valid["lower"]).all()
