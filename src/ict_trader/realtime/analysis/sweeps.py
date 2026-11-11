"""BSL/SSL sweep events with an explicit age/TTL.

A sweep is an event, not a permanent boolean.  Consumers receive the event
time, the reclaimed level and an expiry so a stale sweep cannot authorize a
new setup hours later.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pandas as pd

from ...concepts import ICTConfig, build_zigzag, detect_liquidity, find_pivots
from ...enhancements import detect_session_levels
from ...sweeps import detect_key_levels, detect_sweeps


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
    # doc 69 L178/L181: the stop belongs beyond the sweep's EXTREME (the wick that
    # took the stops), not at the swept level - "a retest of that wick can take a
    # stop placed there before the reversal gets going".
    sweep_price: float = 0.0
    source_name: str = ""

    def to_public(self) -> dict[str, Any]:
        return {
            "sweep_type": self.sweep_type, "direction": self.direction,
            "time": self.time, "level": self.level, "close": self.close,
            "reclaim_strength": round(self.reclaim_strength, 4),
            "expires_at": self.expires_at, "bar_index": self.bar_index,
            "active": self.active, "sweep_price": round(self.sweep_price, 8),
        }


def build_sweep_events(frame: pd.DataFrame, *, ttl_bars: int = 8,
                       left: int = 2, right: int = 2) -> list[SweepEvent]:
    """Return the same sweep events used by the ICT strategy engine.

    The canonical detector is ``ict_trader.sweeps.detect_sweeps`` over
    ``detect_key_levels``.  The realtime overlay must not maintain a second
    swing-only sweep algorithm.
    """
    if frame is None or len(frame) < 12:
        return []
    df = frame.reset_index(drop=True).copy()
    if "date" not in df.columns:
        return []
    pivots = find_pivots(df, 5, 1)
    swings = build_zigzag(pivots)
    sessions = detect_session_levels(df)
    liq = detect_liquidity(df, pivots, ICTConfig())
    levels = detect_key_levels(df, swings, sessions, liq)
    raw = detect_sweeps(df, levels, reclaim_window=3)
    times = [int(pd.Timestamp(x).timestamp() * 1000) for x in df["date"]]
    step = max(1, times[-1] - times[-2]) if len(times) > 1 else 900_000
    now = times[-1]
    # ICT analyze(mode="present") exposes the last 500 bars only.
    cutoff = max(0, len(df) - 500)
    out: list[SweepEvent] = []
    for s in raw:
        if int(s.reclaim_bar) < cutoff:
            continue
        i = max(0, min(int(s.sweep_bar), len(times) - 1))
        t = times[i]
        ev = SweepEvent(
            "SSL" if s.direction == "bull" else "BSL",
            "bullish" if s.direction == "bull" else "bearish",
            t, float(s.level), float(s.reclaim_close),
            float(s.penetration_atr), t + max(1, int(ttl_bars)) * step,
            int(s.reclaim_bar), False, float(s.sweep_price), str(s.name),
        )
        ev.active = now <= ev.expires_at
        out.append(ev)
    return out
