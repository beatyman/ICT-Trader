"""筹码分布（CYQ）/ 成交量分布验收测试。"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ict_trader.cyq import volume_profile, cyq_chip_distribution, chip_density_at  # noqa: E402


def _df(n=120, seed=0):
    rng = np.random.RandomState(seed)
    # 在 100 附近震荡，成交量集中在 100 附近
    close = 100 + rng.normal(0, 1, n)
    high = close + abs(rng.normal(0, 0.5, n))
    low = close - abs(rng.normal(0, 0.5, n))
    open_ = close + rng.normal(0, 0.3, n)
    vol = rng.uniform(50, 200, n)
    return pd.DataFrame({"date": pd.date_range("2026-01-01", periods=n, freq="D"),
                         "open": open_, "high": high, "low": low, "close": close,
                         "volume": vol})


def test_volume_profile_shape_and_norm():
    df = _df()
    prices, density = volume_profile(df, window=100, bins=150)
    assert len(prices) == 150
    assert len(density) == 150
    assert abs(sum(density) - 1.0) < 1e-9
    # 价格递增
    assert prices == sorted(prices)


def test_chip_density_peaks_at_congestion():
    df = _df()
    prices, density = volume_profile(df, window=100, bins=150)
    # 100（震荡中枢）附近的筹码密度应高于远离中枢的价格
    d_mid = chip_density_at(prices, density, 100.0, 2.0)
    d_far = chip_density_at(prices, density, 90.0, 2.0)  # 远离中枢
    assert d_mid > 0
    # 中枢密度 >= 远端密度（成交量集中在 100 附近）
    assert d_mid >= d_far


def test_cyq_falls_back_to_volume_profile():
    df = _df()
    # 无 turnover 列 → 回退到成交量分布
    prices, density = cyq_chip_distribution(df, window=100, bins=150)
    assert len(prices) == 150
    assert abs(sum(density) - 1.0) < 1e-9


def test_cyq_with_turnover():
    df = _df()
    df["turnover"] = np.full(len(df), 5.0)  # 5% 换手率
    prices, density = cyq_chip_distribution(df, window=100, bins=150)
    assert len(prices) == 150
    assert abs(sum(density) - 1.0) < 1e-9
