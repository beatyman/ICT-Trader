"""缠论（chan.py）分析层验收测试。"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ict_trader.chanlun import analyze_chanlun, df_to_klus, chanpy_available  # noqa: E402

pytestmark = pytest.mark.skipif(
    not chanpy_available(), reason="chan.py repo not available (CHANPY_PATH)")

from ict_trader.chanlun import ChanlunResult  # noqa: E402


def _df(n=200, seed=7):
    rng = np.random.RandomState(seed)
    close = 100 + np.cumsum(rng.normal(0, 1, n))
    open_ = np.r_[close[0], close[:-1]] + rng.normal(0, 0.3, n)
    high = np.maximum(open_, close) + abs(rng.normal(0, 0.5, n))
    low = np.minimum(open_, close) - abs(rng.normal(0, 0.5, n))
    return pd.DataFrame({"date": pd.date_range("2026-01-01", periods=n, freq="D"),
                         "open": open_, "high": high, "low": low, "close": close,
                         "volume": np.full(n, 1000.0)})


def test_df_to_klus():
    df = _df(50)
    klus = df_to_klus(df)
    assert len(klus) == 50


def test_analyze_chanlun_structure():
    df = _df(200)
    r = analyze_chanlun(df, "TEST", "1d")
    assert isinstance(r, ChanlunResult)
    assert r.bi_count > 0
    assert r.bias in ("long", "short", "wait")
    assert r.position in ("above", "inside", "below", "none")
    assert r.last_bi_direction in ("up", "down", "none")


def test_analyze_chanlun_insufficient():
    df = _df(10)
    r = analyze_chanlun(df, "TEST", "1d")
    assert r.bias == "wait"
    assert "insufficient" in " ".join(r.reasons)


def test_chanlun_zs_bounds():
    df = _df(200)
    r = analyze_chanlun(df, "TEST", "1d")
    if r.last_zs_zd is not None and r.last_zs_zg is not None:
        assert r.last_zs_zd < r.last_zs_zg  # 中枢下沿 < 上沿
        if r.position == "above":
            assert r.price > r.last_zs_zg
        elif r.position == "below":
            assert r.price < r.last_zs_zd
        else:
            assert r.last_zs_zd <= r.price <= r.last_zs_zg
