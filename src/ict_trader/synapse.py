"""市场状态分类 + SuperTrend ATR 棘轮轨迹 —— 从 WillyAlgoTrader Synapse Trail Pro 吸收。

补上引擎缺的「市场状态 / 波动结构」维度（独立于 ICT 主信号，做辅助过滤）：
  · choppiness_index：震荡指数（0-100，越高越震荡）
  · r_squared：价格线性回归 R²（线性度，越高越趋势）
  · adx：平均趋向指数
  · regime_classify：状态评分 0-100（ADX 40% + 震荡 35% + R² 25%）
                     → Trending(≥60) / Choppy(<35) / Mixed
  · super_trend：ATR 棘轮轨迹（EMA ± ATR×mult，方向翻转 = 信号）
"""
from __future__ import annotations

import numpy as np
import pandas as pd

__all__ = ["choppiness_index", "r_squared", "adx", "regime_classify", "super_trend"]

REGIME_TRENDING = 60
REGIME_CHOPPY = 35


def _ema(x: np.ndarray, length: int) -> np.ndarray:
    out = np.full(len(x), np.nan)
    if len(x) == 0:
        return out
    k = 2.0 / (length + 1)
    out[0] = x[0]
    for i in range(1, len(x)):
        out[i] = x[i] * k + out[i - 1] * (1 - k)
    return out


def _atr_series(df: pd.DataFrame, length: int = 14) -> np.ndarray:
    h = df["high"].astype(float)
    l = df["low"].astype(float)
    c = df["close"].astype(float)
    tr = pd.concat([h - l, (h - c.shift(1)).abs(), (l - c.shift(1)).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1.0 / length, adjust=False).mean().to_numpy()


def choppiness_index(df: pd.DataFrame, length: int = 14) -> np.ndarray:
    """震荡指数（Choppiness Index）。0-100，越高越震荡（无方向）。"""
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    close = df["close"].astype(float)
    tr = pd.concat([high - low, (high - close.shift(1)).abs(),
                    (low - close.shift(1)).abs()], axis=1).max(axis=1)
    atr_sum = tr.rolling(length, min_periods=1).sum().to_numpy()
    hi = high.rolling(length, min_periods=1).max().to_numpy()
    lo = low.rolling(length, min_periods=1).min().to_numpy()
    rng = hi - lo
    log_n = np.log10(max(length, 2))
    out = np.full(len(df), np.nan)
    for i in range(len(df)):
        if rng[i] > 0 and atr_sum[i] > 0:
            out[i] = 100.0 * np.log10(atr_sum[i] / rng[i]) / log_n
    return np.where(np.isnan(out), 50.0, out)


def r_squared(close: np.ndarray, length: int = 50) -> np.ndarray:
    """价格 vs 时间的线性回归 R²（线性度）。"""
    close = np.asarray(close, dtype=float)
    n = len(close)
    out = np.full(n, np.nan)
    x = np.arange(length, dtype=float)
    for i in range(length - 1, n):
        y = close[i - length + 1:i + 1]
        if np.std(y) == 0:
            out[i] = 1.0
            continue
        corr = np.corrcoef(x, y)[0, 1]
        out[i] = corr ** 2 if not np.isnan(corr) else 0.0
    return np.where(np.isnan(out), 0.0, out)


def adx(df: pd.DataFrame, length: int = 14) -> np.ndarray:
    """平均趋向指数（ADX）。"""
    high = df["high"].astype(float).to_numpy()
    low = df["low"].astype(float).to_numpy()
    close = df["close"].astype(float).to_numpy()
    n = len(df)
    tr = np.zeros(n)
    plus_dm = np.zeros(n)
    minus_dm = np.zeros(n)
    for i in range(1, n):
        up = high[i] - high[i - 1]
        down = low[i - 1] - low[i]
        plus_dm[i] = up if (up > down and up > 0) else 0.0
        minus_dm[i] = down if (down > up and down > 0) else 0.0
        tr[i] = max(high[i] - low[i], abs(high[i] - close[i - 1]), abs(low[i] - close[i - 1]))
    tr_s = _wilder(tr, length)
    plus_s = _wilder(plus_dm, length)
    minus_s = _wilder(minus_dm, length)
    safe_tr = np.where(tr_s > 0, tr_s, 1.0)
    di_plus = np.where(tr_s > 0, 100.0 * plus_s / safe_tr, 0.0)
    di_minus = np.where(tr_s > 0, 100.0 * minus_s / safe_tr, 0.0)
    di_sum = di_plus + di_minus
    dx = np.where(di_sum > 0, 100.0 * np.abs(di_plus - di_minus) / np.where(di_sum > 0, di_sum, 1.0), 0.0)
    return _wilder(dx, length)


def _wilder(x: np.ndarray, length: int) -> np.ndarray:
    out = np.full(len(x), np.nan)
    if len(x) <= length + 1:
        return np.where(np.isnan(out), 0.0, out)
    out[length] = x[1:length + 1].mean()   # 种子 = 前 length 个实际值均值
    for i in range(length + 1, len(x)):
        out[i] = (out[i - 1] * (length - 1) + x[i]) / length
    return np.where(np.isnan(out), 0.0, out)


def regime_classify(df: pd.DataFrame, adx_len: int = 14, chop_len: int = 14,
                    r2_len: int = 50) -> pd.DataFrame:
    """返回 regime_score / regime_label / adx / chop / r2。

    score = ADX(40%) + 震荡指数(35%) + R²(25%)。
    label = Trending(≥60) / Choppy(<35) / Mixed。
    """
    adx_vals = adx(df, adx_len)
    chop = choppiness_index(df, chop_len)
    r2 = r_squared(df["close"].astype(float).to_numpy(), r2_len)

    adx_score = np.clip(adx_vals / 50.0 * 100.0, 0, 100)
    chop_score = np.clip(100.0 - chop, 0, 100)
    r2_score = r2 * 100.0
    score = adx_score * 0.40 + chop_score * 0.35 + r2_score * 0.25
    label = np.where(score >= REGIME_TRENDING, "Trending",
                     np.where(score < REGIME_CHOPPY, "Choppy", "Mixed"))
    return pd.DataFrame({
        "regime_score": score,
        "regime_label": label,
        "adx": adx_vals,
        "chop": chop,
        "r2": r2,
    }, index=df.index)


def super_trend(df: pd.DataFrame, atr_len: int = 13, mult: float = 1.618,
                trail_len: int = 21, ratchet: bool = True) -> pd.DataFrame:
    """ATR 棘轮轨迹（SuperTrend 家族）。

    返回 trail / direction（+1 多 / -1 空 / 0 无）。
    方向翻转（-1→+1 = 买入，+1→-1 = 卖出）。
    """
    close = df["close"].astype(float).to_numpy()
    atr = _atr_series(df, atr_len)
    center = _ema(close, trail_len)
    raw_upper = center + atr * mult
    raw_lower = center - atr * mult

    n = len(df)
    upper = np.full(n, np.nan)
    lower = np.full(n, np.nan)
    direction = np.zeros(n, dtype=int)
    prev = 0
    for i in range(n):
        if i == 0:
            upper[i], lower[i] = raw_upper[i], raw_lower[i]
            continue
        if prev == 1 and close[i] < lower[i - 1]:
            prev = -1
        elif prev == -1 and close[i] > upper[i - 1]:
            prev = 1
        elif prev == 0:
            prev = 1 if close[i] > upper[i - 1] else (-1 if close[i] < lower[i - 1] else 0)

        flipped = (direction[i - 1] if i > 0 else 0) != prev
        if ratchet:
            if prev == 1:
                lower[i] = raw_lower[i] if flipped else max(raw_lower[i], lower[i - 1])
                upper[i] = raw_upper[i]
            elif prev == -1:
                upper[i] = raw_upper[i] if flipped else min(raw_upper[i], upper[i - 1])
                lower[i] = raw_lower[i]
            else:
                upper[i], lower[i] = raw_upper[i], raw_lower[i]
        else:
            upper[i], lower[i] = raw_upper[i], raw_lower[i]
        direction[i] = prev

    trail = np.where(direction == 1, lower, np.where(direction == -1, upper, np.nan))
    return pd.DataFrame({
        "trail": trail,
        "upper": upper,
        "lower": lower,
        "direction": direction,
    }, index=df.index)
