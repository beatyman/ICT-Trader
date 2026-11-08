"""主趋势线（下降阻力 / 上升支撑）+ 突破 —— 从 SMTREND V2 吸收。

补上引擎缺的「从枢轴画主趋势线」维度（经典技术分析，与 ICT 流动性/PDH-PDL/Camarilla/VWAP 互补）：
  · falling_resistance：主下降阻力线（从最高峰向下，未被后续高点突破的外包络）
  · rising_support：主上升支撑线（从最低谷向上，未被后续低点跌破的外包络）
  · TrendLine：锚点/第二点/斜率/触点数/违规数
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

__all__ = ["TrendLine", "falling_resistance", "rising_support",
           "detect_pivots", "line_breakout"]


@dataclass(frozen=True)
class TrendLine:
    anchor_bar: int      # 锚点 bar
    anchor_px: float     # 锚点价格
    second_bar: int      # 第二点 bar
    second_px: float     # 第二点价格
    slope: float         # 单位 bar 的斜率
    touches: int         # 触点数
    violations: int      # 违规（突破）数
    kind: str            # "resistance" | "support"

    def at(self, bar: int) -> float:
        return self.anchor_px + self.slope * (bar - self.anchor_bar)


def detect_pivots(values: np.ndarray, left: int = 5, right: int = 5,
                  kind: str = "high") -> tuple[list[float], list[int]]:
    """确认的枢轴（值 + bar）。kind = high/low。"""
    n = len(values)
    vals, bars = [], []
    for i in range(left, n - right):
        win = values[i - left:i + right + 1]
        if kind == "high" and values[i] == win.max() and values[i] > values[i - 1] and values[i] > values[i + 1]:
            vals.append(float(values[i])); bars.append(i)
        elif kind == "low" and values[i] == win.min() and values[i] < values[i - 1] and values[i] < values[i + 1]:
            vals.append(float(values[i])); bars.append(i)
    return vals, bars


def _outer_envelope(pivots: list[float], pibars: list[int], kind: str,
                    min_touches: int, max_viol: int, tol: float) -> TrendLine | None:
    """从极值向下（阻力）/向上（支撑）扫描外包络主趋势线。"""
    n = len(pivots)
    if n < 2:
        return None
    # 阻力：从最高峰向下；支撑：从最低谷向上
    order = sorted(range(n), key=lambda i: -pivots[i] if kind == "resistance" else pivots[i])
    for ai in order:
        a_p, a_b = pivots[ai], pibars[ai]
        for bi in range(n):
            if bi == ai:
                continue
            b_p, b_b = pivots[bi], pibars[bi]
            if b_b <= a_b:
                continue
            slope = (b_p - a_p) / (b_b - a_b)
            if kind == "resistance" and slope >= 0:
                continue
            if kind == "support" and slope <= 0:
                continue
            viol = 0
            touches = 0
            bad = False
            for ci in range(n):
                line_v = a_p + slope * (pibars[ci] - a_b)
                if kind == "resistance":
                    if pivots[ci] > line_v + tol:
                        viol += 1
                        if viol > max_viol:
                            bad = True
                            break
                    if abs(pivots[ci] - line_v) <= tol:
                        touches += 1
                else:
                    if pivots[ci] < line_v - tol:
                        viol += 1
                        if viol > max_viol:
                            bad = True
                            break
                    if abs(pivots[ci] - line_v) <= tol:
                        touches += 1
            if not bad and touches >= min_touches:
                return TrendLine(a_b, a_p, b_b, b_p, slope, touches, viol, kind)
    return None


def falling_resistance(highs: list[float] | np.ndarray,
                       bars: list[int] | np.ndarray | None = None,
                       min_touches: int = 2, max_viol: int = 2,
                       tol: float = 0.0) -> TrendLine | None:
    """主下降阻力线。bars 为每个高点对应的 bar index（默认 0..n-1）。"""
    highs = list(highs)
    bars = list(range(len(highs))) if bars is None else list(bars)
    return _outer_envelope(highs, bars, "resistance", min_touches, max_viol, tol)


def rising_support(lows: list[float] | np.ndarray,
                   bars: list[int] | np.ndarray | None = None,
                   min_touches: int = 2, max_viol: int = 2,
                   tol: float = 0.0) -> TrendLine | None:
    """主上升支撑线。"""
    lows = list(lows)
    bars = list(range(len(lows))) if bars is None else list(bars)
    return _outer_envelope(lows, bars, "support", min_touches, max_viol, tol)


def line_breakout(close: np.ndarray, line: TrendLine, bar: int,
                  margin: float = 0.0) -> str | None:
    """在 bar 处是否突破趋势线。返回 "up"/"down"/None。

    阻力线收盘突破上沿 → "up"；支撑线收盘跌破下沿 → "down"。
    """
    if bar >= len(close) or bar <= line.anchor_bar:
        return None
    lv = line.at(bar)
    if line.kind == "resistance" and close[bar] > lv + margin:
        return "up"
    if line.kind == "support" and close[bar] < lv - margin:
        return "down"
    return None
