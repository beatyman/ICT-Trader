"""Current LTF market-structure bias from confirmed pivots.

The bias is intentionally based on the *latest closed bar* and confirmed
swings; a BOS from two hours ago must not authorize a new setup.
"""
from __future__ import annotations

from typing import Any

import pandas as pd


def structure_bias(frame: pd.DataFrame, *, left: int = 2, right: int = 2,
                   lookback: int = 120) -> dict[str, Any]:
    if frame is None or len(frame) < 20:
        return {"bias": "neutral", "event": "", "event_time": 0,
                "swing_high": 0.0, "swing_low": 0.0}
    df = frame.tail(max(40, lookback)).reset_index(drop=True)
    highs = df["high"].astype(float).to_numpy()
    lows = df["low"].astype(float).to_numpy()
    closes = df["close"].astype(float).to_numpy()
    times = [int(pd.Timestamp(x).timestamp() * 1000) for x in df["date"]]
    n = len(df)
    piv_h: dict[int, float] = {}
    piv_l: dict[int, float] = {}
    for i in range(left, n - right):
        if highs[i] >= max(highs[i - left:i]) and highs[i] >= max(highs[i + 1:i + right + 1]):
            piv_h[i] = float(highs[i])
        if lows[i] <= min(lows[i - left:i]) and lows[i] <= min(lows[i + 1:i + right + 1]):
            piv_l[i] = float(lows[i])

    swing_h: tuple[int, float] | None = None
    swing_l: tuple[int, float] | None = None
    trend = 0
    last_event = ""
    last_time = 0
    for i in range(left + 1, n):
        if i in piv_h:
            swing_h = (i, piv_h[i])
        if i in piv_l:
            swing_l = (i, piv_l[i])
        c = float(closes[i])
        if swing_h and i > swing_h[0] and c > swing_h[1]:
            trend = 1
            last_event = "BOS_UP"
            last_time = times[i]
            swing_h = None
        if swing_l and i > swing_l[0] and c < swing_l[1]:
            trend = -1
            last_event = "BOS_DOWN"
            last_time = times[i]
            swing_l = None
    if trend == 0 and n >= 20:
        if closes[-1] > closes[-20] and lows[-1] >= lows[-20]:
            trend, last_event = 1, "TREND_UP"
        elif closes[-1] < closes[-20] and highs[-1] <= highs[-20]:
            trend, last_event = -1, "TREND_DOWN"
    return {
        "bias": "bullish" if trend > 0 else "bearish" if trend < 0 else "neutral",
        "event": last_event,
        "event_time": last_time,
        "swing_high": swing_h[1] if swing_h else 0.0,
        "swing_low": swing_l[1] if swing_l else 0.0,
    }
