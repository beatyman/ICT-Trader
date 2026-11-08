"""主趋势线 + 突破验收测试。"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ict_trader.trendlines import (falling_resistance, rising_support,
                                   detect_pivots, line_breakout)  # noqa: E402


def test_detect_pivots_finds_highs():
    x = np.array([1, 2, 3, 5, 3, 2, 4, 6, 4, 2, 3, 5, 3, 1], dtype=float)
    vals, bars = detect_pivots(x, left=1, right=1, kind="high")
    assert len(vals) >= 2
    assert 5.0 in vals and 6.0 in vals


def test_falling_resistance_from_peaks():
    # 下降趋势：高点 10 -> 8 -> 6（斜率 -1 每单位）
    highs = [10.0, 8.0, 6.0]
    bars = [0, 2, 4]
    line = falling_resistance(highs, bars, min_touches=2, max_viol=0)
    assert line is not None
    assert line.kind == "resistance"
    assert abs(line.slope - (-1.0)) < 1e-9
    assert line.touches >= 2
    assert abs(line.at(4) - 6.0) < 1e-9


def test_rising_support_from_troughs():
    lows = [1.0, 2.0, 3.0]
    bars = [0, 2, 4]
    line = rising_support(lows, bars, min_touches=2, max_viol=0)
    assert line is not None
    assert line.kind == "support"
    assert abs(line.slope - 0.5) < 1e-9


def test_no_line_when_flat():
    # 无下降趋势（高点相等）→ 找不到下降阻力线
    highs = [5.0, 5.0, 5.0]
    bars = [0, 1, 2]
    line = falling_resistance(highs, bars, min_touches=2, max_viol=0)
    assert line is None


def test_line_breakout_detection():
    line = falling_resistance([10.0, 8.0], [0, 2], min_touches=2, max_viol=0)
    close = np.array([11.0, 9.0, 7.0, 8.5, 9.5, 8.5, 7.5])
    # line.at(5) = 10 + (-1)*(5-0) = 5.0；close[5]=8.5 > 5 → up（margin 0）
    assert line_breakout(close, line, 5) == "up"
