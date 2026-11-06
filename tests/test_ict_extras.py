"""ict_extras（SMT 配对 / BPR / VI）验收测试。"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ict_trader.ict_extras import resolve_pair, detect_bpr, detect_vimbalances  # noqa: E402


class _F:
    def __init__(self, direction, top, bottom, created, active=True):
        self.direction = direction
        self.top = top
        self.bottom = bottom
        self.created = created
        self.active = active


def test_resolve_pair():
    assert resolve_pair("BTCUSDT") == "ETHUSDT"
    assert resolve_pair("ETHUSDT") == "BTCUSDT"
    assert resolve_pair("BINANCE:BTCUSDT") == "ETHUSDT"
    assert resolve_pair("CME_MINI:ES1!") == "NQ"
    assert resolve_pair("ES1!") == "NQ"
    assert resolve_pair("XAUUSD") == "XAGUSD"
    assert resolve_pair("UNKNOWN") == ""


def test_bpr_overlap():
    fvgs = [_F("bull", 105, 101, 1), _F("bear", 103, 98, 2)]
    r = detect_bpr(fvgs)
    assert r is not None
    # bull [101,105] 与 bear [98,103] 重叠 = [101,103]
    assert r == (101.0, 103.0)


def test_bpr_no_overlap():
    fvgs = [_F("bull", 105, 102, 1), _F("bear", 100, 98, 2)]
    assert detect_bpr(fvgs) is None


def test_bpr_price_inside_returns_none():
    fvgs = [_F("bull", 105, 101, 1), _F("bear", 103, 98, 2)]
    # 价格在均衡区 [101,103] 内 → 不算有效回踩区
    assert detect_bpr(fvgs, price=102.0) is None


def test_vimbalance_columns():
    rng = np.random.RandomState(0)
    n = 120
    close = 100 + np.cumsum(rng.normal(0, 1, n))
    open_ = close + rng.normal(0, 0.3, n)
    high = np.maximum(open_, close) + rng.uniform(0.1, 0.6, n)
    low = np.minimum(open_, close) - rng.uniform(0.1, 0.6, n)
    vol = rng.uniform(50, 200, n)
    df = pd.DataFrame({"date": pd.date_range("2026-01-01", periods=n, freq="D"),
                       "open": open_, "high": high, "low": low, "close": close, "volume": vol})
    out = detect_vimbalances(df)
    assert list(out.columns) == ["vim_bull", "vim_bear"]
    assert len(out) == n
    assert out["vim_bull"].dtype == bool
    assert out["vim_bear"].dtype == bool
