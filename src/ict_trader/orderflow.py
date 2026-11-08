"""标准化订单流（成交量差值）+ 订单速度 + 隐藏背离 —— 从 AlgoAlpha Standardized Orderflow 吸收。

补上引擎缺的「订单流 / 资金流」维度：
  · order_flow：成交量差值（涨计入 +vol，跌计入 -vol）的滚动和，HMA 平滑
  · standardized_order_flow：z-score 标准化（order_flow / 2×std），跨品种可比
  · order_velocity：标准化订单流的变化率（速率）
  · detect_divergence：常规 + 隐藏 顶/底背离（在订单流曲线上）
"""
from __future__ import annotations

import numpy as np
import pandas as pd

__all__ = ["order_flow", "standardized_order_flow", "order_velocity",
           "detect_divergence", "Divergence"]


def _wma(x: np.ndarray, length: int) -> np.ndarray:
    n = len(x)
    out = np.full(n, np.nan)
    if length < 1:
        return out
    w = np.arange(1, length + 1, dtype=float)
    wsum = w.sum()
    for i in range(length - 1, n):
        out[i] = float(np.dot(x[i - length + 1:i + 1], w) / wsum)
    return out


def _hma(x: np.ndarray, length: int) -> np.ndarray:
    if length < 2:
        return x.astype(float)
    half = max(1, length // 2)
    raw = 2.0 * _wma(x, half) - _wma(x, length)
    return _wma(raw, max(1, int(round(np.sqrt(length)))))


def order_flow(df: pd.DataFrame, window: int = 12, smooth_len: int = 10,
               use_smoothing: bool = True) -> pd.Series:
    """成交量差值（order flow）：涨计入 +volume，跌计入 -volume，滚动和 + HMA 平滑。"""
    close = df["close"].astype(float).to_numpy()
    vol = df["volume"].astype(float).to_numpy() if "volume" in df else np.zeros(len(df))
    n = len(df)
    signed = np.zeros(n)
    for i in range(1, n):
        if close[i] > close[i - 1]:
            signed[i] = vol[i]
        elif close[i] < close[i - 1]:
            signed[i] = -vol[i]
    raw = pd.Series(signed).rolling(window, min_periods=1).sum().to_numpy()
    if use_smoothing:
        raw = _hma(raw, smooth_len)
    return pd.Series(raw, index=df.index)


def standardized_order_flow(df: pd.DataFrame, window: int = 12, smooth_len: int = 10,
                            std_len: int = 45) -> pd.Series:
    """标准化订单流：order_flow / (2×std)，z-score 式归一。"""
    of = order_flow(df, window, smooth_len).to_numpy()
    std = pd.Series(of).rolling(std_len, min_periods=2).std().to_numpy()
    denom = np.where(std > 0, std, np.nan)
    out = of / (denom + denom)   # = of / (2*std)
    return pd.Series(out, index=df.index)


def order_velocity(df: pd.DataFrame, window: int = 12, smooth_len: int = 10,
                   std_len: int = 45, velocity_calc_len: int = 21,
                   velocity_signal_len: int = 14) -> pd.Series:
    """订单速度：标准化订单流相对其均线的偏离 × 10 再平滑。"""
    norm = standardized_order_flow(df, window, smooth_len, std_len).to_numpy()
    sig = pd.Series(norm).rolling(velocity_signal_len, min_periods=1).mean().to_numpy()
    diff = (norm - sig) * 10.0
    vel = pd.Series(diff).rolling(velocity_calc_len, min_periods=1).mean().to_numpy()
    return pd.Series(vel, index=df.index)


class Divergence:
    __slots__ = ("bar", "kind", "price1", "price2", "osc1", "osc2")

    def __init__(self, bar, kind, price1, price2, osc1, osc2):
        self.bar = bar
        self.kind = kind   # "bullish" | "bearish" | "hidden_bullish" | "hidden_bearish"
        self.price1 = price1
        self.price2 = price2
        self.osc1 = osc1
        self.osc2 = osc2


def detect_divergence(df: pd.DataFrame, osc: np.ndarray, left: int = 30,
                      right: int = 1, max_dist: int = 80) -> list[Divergence]:
    """订单流曲线上的常规 + 隐藏背离。

    常规顶背离：价 HH + 曲线 LH；常规底背离：价 LL + 曲线 HL。
    隐藏顶背离：价 LH + 曲线 HH（趋势延续中的动能衰竭）；隐藏底背离：价 HL + 曲线 LL。
    """
    n = len(df)
    if n < left + right + 2:
        return []
    high = df["high"].astype(float).to_numpy()
    low = df["low"].astype(float).to_numpy()
    osc = np.asarray(osc, dtype=float)

    def pivots(values, kind):
        vals, bars, prices = [], [], []
        for i in range(left, n - right):
            win = values[i - left:i + right + 1]
            if kind == "high" and values[i] == win.max() and values[i] > values[i - 1] and values[i] > values[i + 1]:
                vals.append(float(values[i])); bars.append(i); prices.append(float(high[i]))
            elif kind == "low" and values[i] == win.min() and values[i] < values[i - 1] and values[i] < values[i + 1]:
                vals.append(float(values[i])); bars.append(i); prices.append(float(low[i]))
        return vals, bars, prices

    out: list[Divergence] = []
    # 高点（顶背离 / 隐藏顶背离）
    hv, hb, hp = pivots(osc, "high")
    if len(hb) >= 2:
        b1, v1, p1 = hb[-2], hv[-2], hp[-2]
        b2, v2, p2 = hb[-1], hv[-1], hp[-1]
        if 0 <= b2 - b1 <= max_dist:
            if p2 > p1 and v2 < v1:
                out.append(Divergence(b2, "bearish", p1, p2, v1, v2))       # 常规顶背离
            elif p2 < p1 and v2 > v1:
                out.append(Divergence(b2, "hidden_bearish", p1, p2, v1, v2))  # 隐藏顶背离
    # 低点（底背离 / 隐藏底背离）
    lv, lb_, lp = pivots(osc, "low")
    if len(lb_) >= 2:
        b1, v1, p1 = lb_[-2], lv[-2], lp[-2]
        b2, v2, p2 = lb_[-1], lv[-1], lp[-1]
        if 0 <= b2 - b1 <= max_dist:
            if p2 < p1 and v2 > v1:
                out.append(Divergence(b2, "bullish", p1, p2, v1, v2))        # 常规底背离
            elif p2 > p1 and v2 < v1:
                out.append(Divergence(b2, "hidden_bullish", p1, p2, v1, v2))  # 隐藏底背离

    out.sort(key=lambda d: d.bar)
    return out
