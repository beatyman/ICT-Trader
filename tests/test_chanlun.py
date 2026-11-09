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


def test_chanlun_exposes_drawable_structure():
    r = analyze_chanlun(_df(300), "TEST", "1d")
    pub = r.to_public()
    for key in ("bis", "segs", "zss", "bsps", "divergences"):
        assert key in pub
    for b in r.bis:
        assert b["start_time"] > 0 and b["end_time"] >= b["start_time"]
        if b["direction"] == "up":
            assert b["end_price"] > b["start_price"]
        else:
            assert b["end_price"] < b["start_price"]
    for z in r.zss:
        assert z["low"] < z["high"]
        assert z["start_time"] <= z["end_time"]
    for b in r.bsps:
        assert b["time"] > 0
        assert b["type"]


def test_requested_chan_config_is_loaded_exactly():
    from ict_trader.chan_config import flat_config
    from ChanConfig import CChanConfig

    cfg = CChanConfig(flat_config())
    assert cfg.bi_conf.bi_algo == "normal"
    assert cfg.bi_conf.is_strict is False
    assert cfg.bi_conf.gap_as_kl is True
    assert cfg.bi_conf.bi_end_is_peak is True
    assert cfg.bi_conf.bi_allow_sub_peak is False
    assert cfg.seg_conf.seg_algo == "chan"
    assert cfg.zs_conf.need_combine is True
    assert cfg.zs_conf.one_bi_zs is False
    assert cfg.bs_point_conf.b_conf.min_zs_cnt == 1
    assert cfg.bs_point_conf.b_conf.bsp1_only_multibi_zs is True
    assert cfg.bs_point_conf.b_conf.bsp2_follow_1 is False
    assert cfg.bs_point_conf.b_conf.bsp3_follow_1 is False
    assert cfg.seg_bs_point_conf.b_conf.macd_algo.name == "SLOPE"
    assert cfg.bs_point_conf.b_conf.macd_algo.name == "PEAK"
    assert [t.value for t in cfg.bs_point_conf.b_conf.target_types] == [
        "1", "1p", "2", "2s", "3a", "3b",
    ]
    assert cfg.macd_config == {"fast": 12, "slow": 26, "signal": 9}
    assert cfg.boll_n == 20
    assert cfg.rsi_cycle == 14
    assert cfg.kdj_cycle == 9
