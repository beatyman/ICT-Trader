"""BSL/SSL sweep events with an explicit age/TTL.

A sweep is an event, not a permanent boolean.  Consumers receive the event
time, the reclaimed level and an expiry so a stale sweep cannot authorize a
new setup hours later.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pandas as pd

from ...concepts import (ICTConfig, PIVOT_LEFT, PIVOT_RIGHT, build_zigzag,
                         detect_liquidity, find_pivots)
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

    @property
    def known_at(self) -> int:
        """P1-08/CB-07: the bar whose close confirms the sweep.

        It is the reclaim bar (``bar_index``), never the penetration bar --
        SW-02 says the event is unknowable at the penetration candle.
        """
        return int(self.bar_index)

    @property
    def known_at_ms(self) -> int:
        """The knowledge bar's wall-clock time (same value as ``time``)."""
        return int(self.time)

    @property
    def is_active(self) -> bool:
        """Compatibility view of the declared TTL flag ``active``."""
        return bool(self.active)

    def to_public(self) -> dict[str, Any]:
        return {
            "sweep_type": self.sweep_type, "direction": self.direction,
            "time": self.time, "level": self.level, "close": self.close,
            "reclaim_strength": round(self.reclaim_strength, 4),
            "expires_at": self.expires_at, "bar_index": self.bar_index,
            "known_at": self.known_at, "known_at_ms": self.known_at_ms,
            "active": self.active, "is_active": self.is_active,
            "sweep_price": round(self.sweep_price, 8),
        }


def build_sweep_events(frame: pd.DataFrame, *, ttl_bars: int = 8,
                       left: int = PIVOT_LEFT,
                       right: int = PIVOT_RIGHT) -> list[SweepEvent]:
    """Return the same sweep events used by the ICT strategy engine.

    The canonical detector is ``ict_trader.sweeps.detect_sweeps`` over
    ``detect_key_levels``.  The realtime overlay must not maintain a second
    swing-only sweep algorithm.

    ``left``/``right`` are the pivot width and are **honoured** (P0-20): a
    caller that declares a different width gets that width, instead of silently
    receiving the old hardcoded 5/1.  The defaults are the one declared
    :data:`~ict_trader.concepts.PIVOT_WIDTH` (repo 5/1), so the default path is
    unchanged.
    """
    if frame is None or len(frame) < 12:
        return []
    if int(left) < 1 or int(right) < 0:
        raise ValueError(
            f"pivot width must be left >= 1 and right >= 0; got ({left}, {right})")
    df = frame.reset_index(drop=True).copy()
    if "date" not in df.columns:
        return []
    pivots = find_pivots(df, int(left), int(right))
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
        # The sweep is known only when the reclaim prints, so its time is the
        # reclaim bar (not the penetration bar) and the TTL counts from there.
        r = max(0, min(int(s.reclaim_bar), len(times) - 1))
        t = times[r]
        ev = SweepEvent(
            sweep_type="SSL" if s.direction == "bull" else "BSL",
            direction="bullish" if s.direction == "bull" else "bearish",
            time=t, level=float(s.level), close=float(s.reclaim_close),
            reclaim_strength=float(s.penetration_atr),
            expires_at=t + max(1, int(ttl_bars)) * step,
            bar_index=r, active=False,
            sweep_price=float(s.sweep_price), source_name=str(s.name),
        )
        ev.active = now <= ev.expires_at
        out.append(ev)
    return out
