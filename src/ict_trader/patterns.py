"""经典图表形态识别 —— 从 Pattern Scanner AI AlgoCode v2.3 吸收。

检测：双顶/双底、三顶/三底、头肩顶/底（含反向）、上升/下降楔形。
每个形态输出：类型、方向、颈线入场、极值止损、测量目标、R:R。

补充 ICT 引擎的「结构形态」维度：swing 高低点 → 经典反转形态。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

__all__ = ["detect_patterns", "Pattern"]


class Pattern:
    __slots__ = ("type", "direction", "entry", "stop", "target", "rr", "bars")

    def __init__(self, type_, direction, entry, stop, target, bars):
        self.type = type_
        self.direction = direction   # "long" / "short"
        self.entry = entry           # 颈线
        self.stop = stop             # 极值
        self.target = target         # 测量目标
        self.bars = bars
        risk = abs(entry - stop)
        self.rr = abs(target - entry) / risk if risk > 0 else 0.0


def _atr(df: pd.DataFrame, period: int = 14) -> float:
    h = df["high"].astype(float)
    l = df["low"].astype(float)
    c = df["close"].astype(float)
    tr = pd.DataFrame({"hl": h - l, "hc": (h - c.shift(1)).abs(),
                       "lc": (l - c.shift(1)).abs()}).max(axis=1)
    return float(tr.rolling(period, min_periods=1).mean().iloc[-1])


def _pivots(values: np.ndarray, left: int, right: int) -> tuple[list[float], list[int]]:
    """返回 (pivot 值, pivot bar index)。"""
    n = len(values)
    vals, bars = [], []
    for i in range(left, n - right):
        win = values[i - left:i + right + 1]
        if values[i] == win.max() and values[i] > values[i - 1] and values[i] > values[i + 1]:
            vals.append(float(values[i]))
            bars.append(i)
    return vals, bars


def detect_patterns(df: pd.DataFrame, left: int = 5, right: int = 5,
                    tol_atr: float = 1.0, min_height_atr: float = 0.5,
                    min_rr: float = 0.8, breakout_buffer_atr: float = 0.05,
                    atr_period: int = 14) -> list[Pattern]:
    """检测经典图表形态。返回 Pattern 列表（按确认 bar 排序）。"""
    n = len(df)
    if n < left + right + 5:
        return []
    high = df["high"].astype(float).to_numpy()
    low = df["low"].astype(float).to_numpy()
    close = df["close"].astype(float).to_numpy()
    atr = _atr(df, atr_period)
    if atr <= 0:
        return []
    tol = atr * tol_atr
    buf = atr * breakout_buffer_atr

    hv, hb = _pivots(high, left, right)
    lv, lb = _pivots(low, left, right)

    def gh(i):  # 倒数第 i 个 pivot high
        return hv[-1 - i] if len(hv) > i else np.nan

    def ghb(i):
        return hb[-1 - i] if len(hb) > i else np.nan

    def gl(i):
        return lv[-1 - i] if len(lv) > i else np.nan

    def glb(i):
        return lb[-1 - i] if len(lb) > i else np.nan

    def near(a, b):
        return not np.isnan(a) and not np.isnan(b) and abs(a - b) <= tol

    # 破位（收盘穿越颈线 + 缓冲）
    def br_dn(lvl):
        return not np.isnan(lvl) and close[-1] < lvl - buf and close[-2] >= lvl - buf

    def br_up(lvl):
        return not np.isnan(lvl) and close[-1] > lvl + buf and close[-2] <= lvl + buf

    out: list[Pattern] = []

    # 双顶
    if near(gh(0), gh(1)) and not np.isnan(gl(0)) and gl(0) < min(gh(0), gh(1)):
        neck = gl(0)
        top = max(gh(0), gh(1))
        h = (gh(0) + gh(1)) / 2 - neck
        if br_dn(neck) and abs(neck - top) >= atr * min_height_atr:
            out.append(Pattern("double_top", "short", neck, top, neck - h, int(ghb(1))))

    # 双底
    if near(gl(0), gl(1)) and not np.isnan(gh(0)) and gh(0) > max(gl(0), gl(1)):
        neck = gh(0)
        bot = min(gl(0), gl(1))
        h = neck - (gl(0) + gl(1)) / 2
        if br_up(neck) and abs(neck - bot) >= atr * min_height_atr:
            out.append(Pattern("double_bottom", "long", neck, bot, neck + h, int(glb(1))))

    # 三顶
    if near(gh(0), gh(1)) and near(gh(1), gh(2)) and not np.isnan(gl(0)) and not np.isnan(gl(1)):
        neck = min(gl(0), gl(1))
        top = max(gh(0), gh(1), gh(2))
        h = (gh(0) + gh(1)) / 2 - neck
        if br_dn(neck) and abs(neck - top) >= atr * min_height_atr:
            out.append(Pattern("triple_top", "short", neck, top, neck - h, int(ghb(2))))

    # 三底
    if near(gl(0), gl(1)) and near(gl(1), gl(2)) and not np.isnan(gh(0)) and not np.isnan(gh(1)):
        neck = max(gh(0), gh(1))
        bot = min(gl(0), gl(1), gl(2))
        h = neck - (gl(0) + gl(1)) / 2
        if br_up(neck) and abs(neck - bot) >= atr * min_height_atr:
            out.append(Pattern("triple_bottom", "long", neck, bot, neck + h, int(glb(2))))

    # 头肩顶
    if (not np.isnan(gh(2)) and not np.isnan(gl(1))
            and gh(1) > gh(0) + tol and gh(1) > gh(2) + tol and near(gh(0), gh(2))):
        neck = (gl(0) + gl(1)) / 2
        h = gh(1) - neck
        if br_dn(neck) and h >= atr * min_height_atr:
            out.append(Pattern("head_shoulders", "short", neck, gh(1), neck - h, int(ghb(2))))

    # 反向头肩
    if (not np.isnan(gl(2)) and not np.isnan(gh(1))
            and gl(1) < gl(0) - tol and gl(1) < gl(2) - tol and near(gl(0), gl(2))):
        neck = (gh(0) + gh(1)) / 2
        h = neck - gl(1)
        if br_up(neck) and h >= atr * min_height_atr:
            out.append(Pattern("inverse_head_shoulders", "long", neck, gl(1), neck + h, int(glb(2))))

    # 楔形（收敛边界 + 破位）
    if not np.isnan(gh(2)) and not np.isnan(gl(2)):
        r2 = gh(2) - gl(2)
        r0 = gh(0) - gl(0)
        converging = r0 < r2 * 0.7 and r2 > atr * min_height_atr
        rising = gh(0) > gh(1) > gh(2) and gl(0) > gl(1) > gl(2)
        falling = gh(0) < gh(1) < gh(2) and gl(0) < gl(1) < gl(2)
        if converging and rising and br_dn(gl(0)):
            out.append(Pattern("rising_wedge", "short", gl(0), gh(0), gl(0) - r2, int(min(ghb(2), glb(2)))))
        if converging and falling and br_up(gh(0)):
            out.append(Pattern("falling_wedge", "long", gh(0), gl(0), gh(0) + r2, int(min(ghb(2), glb(2)))))

    # R:R 过滤 + 排序
    out = [p for p in out if p.rr >= min_rr]
    out.sort(key=lambda p: p.bars)
    return out
