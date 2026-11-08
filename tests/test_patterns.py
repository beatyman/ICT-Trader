"""经典图表形态识别验收测试。"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ict_trader.patterns import detect_patterns  # noqa: E402


def _df(n=300, seed=7):
    rng = np.random.RandomState(seed)
    close = 100 + np.cumsum(rng.normal(0, 1, n))
    open_ = np.r_[close[0], close[:-1]]
    high = np.maximum(open_, close) + abs(rng.normal(0, 0.5, n))
    low = np.minimum(open_, close) - abs(rng.normal(0, 0.5, n))
    return pd.DataFrame({"date": pd.date_range("2026-01-01", periods=n, freq="D"),
                         "open": open_, "high": high, "low": low, "close": close,
                         "volume": np.full(n, 100.0)})


def test_detect_patterns_returns_list():
    df = _df(300)
    pats = detect_patterns(df, left=5, right=5)
    assert isinstance(pats, list)


def test_pattern_fields_valid():
    df = _df(300)
    for p in detect_patterns(df, left=5, right=5):
        assert p.direction in ("long", "short")
        assert p.entry > 0 and p.stop > 0 and p.target > 0
        assert p.rr >= 0.8  # min_rr 过滤
        assert p.type in ("double_top", "double_bottom", "triple_top", "triple_bottom",
                          "head_shoulders", "inverse_head_shoulders",
                          "rising_wedge", "falling_wedge")


def test_no_crash_on_insufficient_data():
    df = _df(10)  # 太少
    assert detect_patterns(df) == []
