"""Wyckoff 质量评分 + 量价 effort ratio 验收测试。"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ict_trader.wyckoff_quality import effort_ratio, climax_score, spring_score, sos_score  # noqa: E402


def _df(n=200, seed=7):
    rng = np.random.RandomState(seed)
    close = 100 + np.cumsum(rng.normal(0, 1, n))
    open_ = np.r_[close[0], close[:-1]]
    high = np.maximum(open_, close) + abs(rng.normal(0, 0.5, n))
    low = np.minimum(open_, close) - abs(rng.normal(0, 0.5, n))
    vol = rng.uniform(50, 200, n)
    return pd.DataFrame({"date": pd.date_range("2026-01-01", periods=n, freq="D"),
                         "open": open_, "high": high, "low": low, "close": close,
                         "volume": vol})


def test_effort_ratio_normalized_around_one():
    df = _df(200)
    e = effort_ratio(df, baseline_len=50)
    assert len(e) == len(df)
    assert e.isna().sum() == 0
    # 比率围绕 1.0 波动（相对自己的基线均值）
    assert 0.5 < e.mean() < 1.5


def test_effort_ratio_falls_back_to_range():
    df = _df(200).drop(columns=["volume"])
    e = effort_ratio(df, baseline_len=50)
    assert len(e) == len(df)
    assert e.isna().sum() == 0


def test_climax_score_range():
    rng = np.random.RandomState(0)
    run_eff = rng.uniform(1.0, 3.0, 20)
    s = climax_score(run_eff, run_atr=4.0, run_bars=10)
    assert 0 <= s <= 100


def test_spring_and_sos_score_range():
    s1 = spring_score(depth_atr=1.0, spring_eff=0.8, climax_eff=2.0, bars_since=3)
    s2 = sos_score(leg_eff=2.0, leg_atr=3.0, close_pos=0.9)
    assert 0 <= s1 <= 100
    assert 0 <= s2 <= 100


def test_strong_effort_scores_higher():
    # 高努力/深回抽 → 分数更高
    weak = spring_score(depth_atr=1.4, spring_eff=1.5, climax_eff=2.0, bars_since=9)
    strong = spring_score(depth_atr=0.3, spring_eff=0.6, climax_eff=2.0, bars_since=1)
    assert strong > weak
