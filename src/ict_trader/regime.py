"""Small regime descriptors used for post-trade analysis, not entry gates.

They are computed only from bars available at the signal close, so a later
research script can split outcomes without introducing look-ahead.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class Regime:
    trend: str          # up | down | flat
    volatility: str     # low | normal | high
    trend_strength: float
    atr_pct: float
    vol_rank: float


def market_regime(df: pd.DataFrame, t: int, lookback: int = 500) -> Regime:
    close = df["close"].to_numpy(float)
    high = df["high"].to_numpy(float)
    low = df["low"].to_numpy(float)
    if t < 20 or t >= len(df):
        return Regime("flat", "normal", 0.0, 0.0, 0.5)

    tr = np.maximum.reduce([
        high - low,
        np.abs(high - np.r_[np.nan, close[:-1]]),
        np.abs(low - np.r_[np.nan, close[:-1]]),
    ])
    atr = pd.Series(tr).rolling(14, min_periods=1).mean().to_numpy()
    ema = pd.Series(close).ewm(span=20, adjust=False).mean().to_numpy()
    a = float(atr[t]) if not np.isnan(atr[t]) else 0.0
    slope = float(ema[t] - ema[max(0, t - 10)])
    strength = slope / a if a > 0 else 0.0
    atr_pct = a / float(close[t]) if close[t] else 0.0

    lo = max(0, t - lookback + 1)
    hist = atr[lo:t + 1]
    hist = hist[~np.isnan(hist)]
    rank = float((hist <= a).mean()) if len(hist) else 0.5

    trend = "up" if strength > 0.15 else "down" if strength < -0.15 else "flat"
    vol = "high" if rank >= 0.75 else "low" if rank <= 0.25 else "normal"
    return Regime(trend, vol, strength, atr_pct, rank)
