"""自适应 RSI + 顶/底背离 —— 从 BOSWaves Adaptive RSI 吸收。

补上辅助层的「动量状态 + 背离」维度：
  · rsi：Wilder RSI
  · adaptive_rsi：HMA(4) 平滑价格 → RSI → 可选再平滑 → 百分位自适应阈值 + regime
    （阈值来自历史 RSI 分布的上/下百分位，而非固定 70/30）
  · detect_divergence：常规顶背离（价新高 + RSI 低高点）/ 底背离（价新低 + RSI 高低点）
"""
from __future__ import annotations

import numpy as np
import pandas as pd

__all__ = ["rsi", "adaptive_rsi", "detect_divergence", "Divergence"]


def _hma(x: np.ndarray, length: int) -> np.ndarray:
    """Hull Moving Average。"""
    if length < 2:
        return x.astype(float)
    half = max(1, length // 2)
    wma_half = _wma(x, half)
    wma_full = _wma(x, length)
    raw = 2.0 * wma_half - wma_full
    return _wma(raw, max(1, int(round(np.sqrt(length)))))


def _wma(x: np.ndarray, length: int) -> np.ndarray:
    """Weighted Moving Average。"""
    n = len(x)
    out = np.full(n, np.nan)
    weights = np.arange(1, length + 1, dtype=float)
    wsum = weights.sum()
    for i in range(length - 1, n):
        out[i] = float(np.dot(x[i - length + 1:i + 1], weights) / wsum)
    return out


def rsi(close: np.ndarray, length: int = 14) -> np.ndarray:
    """Wilder RSI（与 ta.rsi 一致）。"""
    close = np.asarray(close, dtype=float)
    n = len(close)
    out = np.full(n, np.nan)
    if n < 2:
        return out
    delta = np.diff(close)
    gain = np.where(delta > 0, delta, 0.0)
    loss = np.where(delta < 0, -delta, 0.0)
    # Wilder 平滑（种子 = 前 length 根简单均值）
    avg_gain = gain[:length].mean()
    avg_loss = loss[:length].mean()
    out[length] = 100.0 - 100.0 / (1.0 + avg_gain / avg_loss) if avg_loss > 0 else 100.0
    for i in range(length + 1, n):
        avg_gain = (avg_gain * (length - 1) + gain[i - 1]) / length
        avg_loss = (avg_loss * (length - 1) + loss[i - 1]) / length
        out[i] = 100.0 - 100.0 / (1.0 + avg_gain / avg_loss) if avg_loss > 0 else 100.0
    return out


def adaptive_rsi(df: pd.DataFrame, length: int = 18, smooth_len: int = 20,
                 adapt_lookback: int = 1000, upper_pct: int = 50,
                 lower_pct: int = 45) -> pd.DataFrame:
    """返回含 osc / upper_thr / lower_thr / regime 的 DataFrame。

    osc = RSI(HMA(close,4), length)，可选 SMA 平滑。
    阈值 = 历史 osc 分布的上/下百分位（自适应）。
    regime = +1（牛）/ -1（熊）/ 保持（0 表示沿用前值）。
    """
    close = df["close"].astype(float).to_numpy()
    base = _hma(close, 4)
    osc_raw = rsi(base, length)
    osc = pd.Series(osc_raw).rolling(smooth_len, min_periods=1).mean().to_numpy()

    n = len(osc)
    upper = np.full(n, np.nan)
    lower = np.full(n, np.nan)
    regime = np.zeros(n, dtype=int)
    hist: list[float] = []
    prev = 0
    for i in range(n):
        v = osc[i]
        if not np.isnan(v):
            hist.append(float(v))
            if len(hist) > adapt_lookback:
                hist = hist[-adapt_lookback:]
            arr = np.sort(np.asarray(hist))
            upper[i] = float(np.percentile(arr, upper_pct))
            lower[i] = float(np.percentile(arr, lower_pct))
            if v > upper[i]:
                prev = 1
            elif v < lower[i]:
                prev = -1
            regime[i] = prev

    return pd.DataFrame({
        "osc": osc,
        "upper_thr": upper,
        "lower_thr": lower,
        "regime": regime,
    }, index=df.index)


class Divergence:
    __slots__ = ("bar", "kind", "price1", "price2", "osc1", "osc2")

    def __init__(self, bar, kind, price1, price2, osc1, osc2):
        self.bar = bar
        self.kind = kind        # "bearish" | "bullish"
        self.price1 = price1
        self.price2 = price2
        self.osc1 = osc1
        self.osc2 = osc2


def detect_divergence(df: pd.DataFrame, osc: np.ndarray, left: int = 15,
                      right: int = 15, min_dist: int = 5,
                      max_dist: int = 60) -> list[Divergence]:
    """常规顶/底背离。

    顶背离（bearish）：价创新高 + RSI 高点下降。
    底背离（bullish）：价创新低 + RSI 低点抬高。
    """
    n = len(df)
    if n < left + right + 2:
        return []
    close = df["close"].astype(float).to_numpy()
    high = df["high"].astype(float).to_numpy()
    low = df["low"].astype(float).to_numpy()
    osc = np.asarray(osc, dtype=float)

    def pivots(values, kind):
        vals, bars, prices = [], [], []
        for i in range(left, n - right):
            win = values[i - left:i + right + 1]
            if kind == "high" and values[i] == win.max() and values[i] > values[i - 1] and values[i] > values[i + 1]:
                vals.append(float(values[i]))
                bars.append(i)
                prices.append(float(high[i]))
            elif kind == "low" and values[i] == win.min() and values[i] < values[i - 1] and values[i] < values[i + 1]:
                vals.append(float(values[i]))
                bars.append(i)
                prices.append(float(low[i]))
        return vals, bars, prices

    out: list[Divergence] = []
    # 顶背离：osc 高点
    phv, phb, php = pivots(osc, "high")
    last_bear = -10 ** 9
    for i in range(len(phb) - 2, max(0, len(phb) - 6) - 1, -1):
        bar1, val1, price1 = phb[i], phv[i], php[i]
        bar2, val2, price2 = phb[-1], phv[-1], php[-1]
        d = bar2 - bar1
        if min_dist <= d <= max_dist and price2 > price1 and val2 < val1:
            if bar2 - last_bear > left * 2:
                out.append(Divergence(bar2, "bearish", price1, price2, val1, val2))
                last_bear = bar2
                break

    # 底背离：osc 低点
    plv, plb, plp = pivots(osc, "low")
    last_bull = -10 ** 9
    for i in range(len(plb) - 2, max(0, len(plb) - 6) - 1, -1):
        bar1, val1, price1 = plb[i], plv[i], plp[i]
        bar2, val2, price2 = plb[-1], plv[-1], plp[-1]
        d = bar2 - bar1
        if min_dist <= d <= max_dist and price2 < price1 and val2 > val1:
            if bar2 - last_bull > left * 2:
                out.append(Divergence(bar2, "bullish", price1, price2, val1, val2))
                last_bull = bar2
                break

    out.sort(key=lambda d: d.bar)
    return out
