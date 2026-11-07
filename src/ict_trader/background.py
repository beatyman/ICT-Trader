"""模块 2：背景过滤层（Wyckoff + Ichimoku，只输出 allow / veto / size_factor）。

辅助策略不得输出 5m 入场价 / 止损 / 目标；只决定「允不允许做、做多大」。
约束：Wyckoff 阶段 B 不得提前断定积累/派发；区间内 veto；薄区间 veto。
Ichimoku 显示偏移 ≠ 数值偏移；当前云用 leading_a[26]/leading_b[26]（非 [52]）；
迟行线确认用 close 与 close[26] 比较。
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .wyckoff import detect_wyckoff, atr as wyckoff_atr
from ichimoku.indicators import ichimoku as ichi_ind
from ichimoku.signals import signals as ichi_sig


@dataclass
class WyckoffBackground:
    bias: str = "neutral"                      # long / short / neutral
    range_state: str = "unclear"               # accumulation/distribution/re-accumulation/re-distribution/trend/unclear
    phase: str = "-"                           # A / B / C / D / E / -
    allow_long: bool = False
    allow_short: bool = False
    size_factor: float = 0.0                   # 0-1
    in_range: bool = False
    thin_range: bool = False
    reasons: list = field(default_factory=list)


@dataclass
class IchimokuBackground:
    cloud_state: str = "inside"                # above / below / inside
    cloud_color: str = "bearish"               # bullish / bearish
    cloud_thickness: str = "thin"              # thick / thin
    tk_state: str = "neutral"                  # bull_cross / bear_cross / neutral
    kijun_state: str = "below"                 # above / below
    allow_long: bool = False
    allow_short: bool = False
    size_factor: float = 0.0
    reasons: list = field(default_factory=list)


@dataclass
class ChanlunBackground:
    bias: str = "neutral"                      # long / short / neutral（HTF 方向）
    position: str = "none"                     # above / inside / below 中枢
    bsp_type: str = "none"                     # 一买/二买/三买/一卖/二卖/三卖
    allow_long: bool = False
    allow_short: bool = False
    size_factor: float = 0.0
    reasons: list = field(default_factory=list)


def wyckoff_background(df_htf: pd.DataFrame, min_range_atr: float = 2.0) -> WyckoffBackground:
    """Wyckoff 背景（4H/1D）：输出 bias / range_state / phase / allow / size。"""
    b = WyckoffBackground()
    st, _ = detect_wyckoff(df_htf)
    if st.direction == 0:
        b.reasons.append("no Wyckoff structure detected")
        return b

    b.bias = "long" if st.direction == 1 else "short"
    b.range_state = "accumulation" if st.direction == 1 else "distribution"
    b.phase = {1: "A", 2: "B", 3: "C", 4: "D", 5: "E"}.get(st.stage, "-")

    atr_v = wyckoff_atr(df_htf).iloc[-1]
    if np.isnan(atr_v) or atr_v <= 0:
        atr_v = 1.0

    # 区间内 veto：价格还在区间内 → 不开新仓
    price = float(df_htf["close"].iloc[-1])
    if not np.isnan(st.range_top) and not np.isnan(st.range_bottom):
        b.in_range = bool(st.range_bottom <= price <= st.range_top)
    # 薄区间 veto
    if not np.isnan(st.range_height):
        b.thin_range = bool(st.range_height < min_range_atr * atr_v)

    # 阶段 B 不得提前断定积累/派发
    if st.stage == 2:
        b.bias = "neutral"
        b.reasons.append("phase B: cause still building, do not assert accumulation/distribution")

    # allow/veto/size
    if b.in_range:
        b.allow_long = b.allow_short = False
        b.size_factor = 0.0
        b.reasons.append("price inside range, veto new positions")
    elif b.thin_range:
        b.allow_long = b.allow_short = False
        b.size_factor = 0.0
        b.reasons.append("thin range, veto")
    elif st.stage == 2:
        b.allow_long = b.allow_short = False
        b.size_factor = 0.0
    else:
        # 方向允许 + 阶段调节 size
        if st.direction == 1:
            b.allow_long = True
        else:
            b.allow_short = True
        # size：C/D/E 阶段结构已确认 → 1.0；A 阶段刚起步 → 0.5
        b.size_factor = 1.0 if st.stage in (3, 4, 5) else 0.5
    return b


def ichimoku_background(df_htf: pd.DataFrame, thin_cloud_atr: float = 0.5) -> IchimokuBackground:
    """Ichimoku 背景（4H/1D）：输出 cloud/tk/kijun/allow/size。"""
    b = IchimokuBackground()
    d = df_htf.copy()
    d.index = pd.to_datetime(d["date"])
    ind = ichi_ind(d, 9, 26, 52, "PLATFORM")
    sig = ichi_sig(d, ind, 26)
    last = -1
    close = float(d["close"].iloc[last])
    la = float(ind["leading_a"].iloc[last])
    lb = float(ind["leading_b"].iloc[last])
    tenkan = float(ind["tenkan"].iloc[last])
    kijun = float(ind["kijun"].iloc[last])
    chikou_bull = bool(sig["chikou_bull"].iloc[last])

    top = max(la, lb)
    bottom = min(la, lb)
    if close > top:
        b.cloud_state = "above"
    elif close < bottom:
        b.cloud_state = "below"
    else:
        b.cloud_state = "inside"
    b.cloud_color = "bullish" if la > lb else "bearish"
    b.tk_state = "bull_cross" if tenkan > kijun else "bear_cross" if tenkan < kijun else "neutral"
    b.kijun_state = "above" if close > kijun else "below"

    # 云厚度（相对 ATR）
    a = wyckoff_atr(df_htf).iloc[-1]
    thick_atr = a if not np.isnan(a) and a > 0 else 1.0
    b.cloud_thickness = "thick" if (top - bottom) >= thin_cloud_atr * thick_atr else "thin"

    # allow / veto / size
    if b.cloud_state == "inside":
        b.allow_long = b.allow_short = False
        b.size_factor = 0.0
        b.reasons.append("price inside cloud, veto")
    elif b.cloud_state == "above" and b.cloud_color == "bullish":
        b.allow_long = True
        b.size_factor = 1.0
        b.reasons.append("above cloud + bullish color")
    elif b.cloud_state == "below" and b.cloud_color == "bearish":
        b.allow_short = True
        b.size_factor = 1.0
        b.reasons.append("below cloud + bearish color")
    else:
        # 云上与云色冲突（比如云上但颜色偏空）→ size 降
        b.size_factor = 0.4
        b.reasons.append("cloud position and color conflict, reduced size")
    # 迟行线确认：close vs close[26]
    if not chikou_bull and b.allow_long:
        b.size_factor *= 0.7
        b.reasons.append("chikou bearish, long size reduced")
    if b.cloud_thickness == "thin":
        b.size_factor *= 0.5
        b.reasons.append("thin cloud, size reduced")
    return b


def chanlun_background(df: pd.DataFrame, freq: str = "1d") -> ChanlunBackground:
    """缠论背景（HTF：日线/周线）：输出 bias / position / bsp / allow / size。

    HTF 缠论决定方向（与 Wyckoff/Ichimoku 同级）；LTF 缠论在 fuse 里做择时。
    """
    b = ChanlunBackground()
    try:
        from .chanlun import analyze_chanlun
    except Exception as e:
        b.reasons.append(f"chanlun unavailable: {e}")
        return b

    r = analyze_chanlun(df, freq=freq)
    b.bias = r.bias if r.bias in ("long", "short") else "neutral"
    b.position = r.position
    b.bsp_type = r.bsp_type

    if b.position == "inside":
        # 中枢内：方向未定 → veto
        b.allow_long = b.allow_short = False
        b.size_factor = 0.0
        b.reasons.append("price inside zhongshu, veto")
    elif b.bias == "long":
        b.allow_long = True
        b.size_factor = 1.0 if b.bsp_type in ("2买", "3买") else 0.7
        b.reasons.append(f"long ({b.bsp_type}, pos={b.position})")
    elif b.bias == "short":
        b.allow_short = True
        b.size_factor = 1.0 if b.bsp_type in ("2卖", "3卖") else 0.7
        b.reasons.append(f"short ({b.bsp_type}, pos={b.position})")
    else:
        b.size_factor = 0.0
        b.reasons.append("no chanlun bias")
    return b
