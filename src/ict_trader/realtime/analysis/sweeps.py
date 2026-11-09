"""BSL/SSL sweep events with an explicit age/TTL.

A sweep is an event, not a permanent boolean.  Consumers receive the event
time, the reclaimed level and an expiry so a stale sweep cannot authorize a
new setup hours later.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pandas as pd


@dataclass(slots=True)
class SweepEvent:
    sweep_type: str          # BSL or SSL
    direction: str           # resulting bias
    time: int
    level: float
    close: float
    reclaim_strength: float
    expires_at: int
    bar_index: int
    active: bool

    def to_public(self) -> dict[str, Any]:
        return {
            "sweep_type": self.sweep_type, "direction": self.direction,
            "time": self.time, "level": self.level, "close": self.close,
            "reclaim_strength": round(self.reclaim_strength, 4),
            "expires_at": self.expires_at, "bar_index": self.bar_index,
            "active": self.active,
        }


def build_sweep_events(frame: pd.DataFrame, *, ttl_bars: int = 8,
                       left: int = 2, right: int = 2) -> list[SweepEvent]:
    if frame is None or len(frame) < 12:
        return []
    df = frame.reset_index(drop=True)
    highs = df["high"].astype(float).to_numpy()
    lows = df["low"].astype(float).to_numpy()
    closes = df["close"].astype(float).to_numpy()
    times = [int(pd.Timestamp(x).timestamp() * 1000) for x in df["date"]]
    n = len(df)
    step = max(1, times[-1] - times[-2]) if n > 1 else 900_000
    atr = sum(float(highs[i] - lows[i]) for i in range(max(0, n - 14), n))
    atr = max(atr / max(1, min(14, n)), 1e-9)
    piv_h: dict[int, float] = {}
    piv_l: dict[int, float] = {}
    for i in range(left, n - right):
        if highs[i] >= max(highs[i - left:i]) and highs[i] >= max(highs[i + 1:i + right + 1]):
            piv_h[i] = float(highs[i])
        if lows[i] <= min(lows[i - left:i]) and lows[i] <= min(lows[i + 1:i + right + 1]):
            piv_l[i] = float(lows[i])
    out: list[SweepEvent] = []
    last_h: float | None = None
    last_l: float | None = None
    used_h: set[float] = set()
    used_l: set[float] = set()
    for i in range(left + 1, n):
        if last_h is not None and i > left and highs[i] > last_h and closes[i] < last_h:
            if last_h not in used_h:
                used_h.add(last_h)
                out.append(SweepEvent("BSL", "bearish", times[i], last_h,
                                      float(closes[i]), abs(float(closes[i]) - last_h) / atr,
                                      times[i] + ttl_bars * step, i, False))
            last_h = None
        if last_l is not None and i > left and lows[i] < last_l and closes[i] > last_l:
            if last_l not in used_l:
                used_l.add(last_l)
                out.append(SweepEvent("SSL", "bullish", times[i], last_l,
                                      float(closes[i]), abs(float(closes[i]) - last_l) / atr,
                                      times[i] + ttl_bars * step, i, False))
            last_l = None
        if i in piv_h:
            last_h = piv_h[i]
        if i in piv_l:
            last_l = piv_l[i]
    now = times[-1]
    for ev in out:
        ev.active = now <= ev.expires_at
    return out
