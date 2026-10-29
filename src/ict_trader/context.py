"""Optional market-context gates: session, premium/discount and OTE.

These are deliberately pure functions over already-known bars.  They do not
change the default frozen setup; `SetupConfig` opts into them one at a time so
each can be ablated.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import time as dtime
from typing import Iterable, Literal
from zoneinfo import ZoneInfo

import pandas as pd

Market = Literal["cn", "hk", "crypto", "other"]
SessionGate = Literal["none", "auto", "asian", "london", "london_close", "ny_am", "ny_pm", "silver_bullet", "cn", "hk"]


@dataclass(frozen=True)
class DealingRange:
    high: float
    low: float
    eq: float
    high_bar: int
    low_bar: int

    @property
    def span(self) -> float:
        return self.high - self.low

    def position(self, price: float) -> float:
        """0 at the low, 1 at the high, clamped."""
        if self.span <= 0:
            return 0.5
        return min(max((price - self.low) / self.span, 0.0), 1.0)


_MARKET_TZ: dict[str, str] = {
    "cn": "Asia/Shanghai",
    "hk": "Asia/Hong_Kong",
    "crypto": "UTC",
    "other": "UTC",
}

_SESSION_WINDOWS: dict[str, tuple[str, list[tuple[dtime, dtime]]]] = {
    "cn": ("Asia/Shanghai", [(dtime(9, 30), dtime(11, 30)), (dtime(13, 0), dtime(15, 0))]),
    "hk": ("Asia/Hong_Kong", [(dtime(9, 30), dtime(12, 0)), (dtime(13, 0), dtime(16, 0))]),
    "london": ("America/New_York", [(dtime(2, 0), dtime(5, 0))]),
    # ICT teaching: Asian range accumulates 19:00-00:00 NY; London close 10:00-12:00 NY.
    "asian": ("America/New_York", [(dtime(19, 0), dtime(23, 59, 59))]),
    "london_close": ("America/New_York", [(dtime(10, 0), dtime(12, 0))]),
    "ny_am": ("America/New_York", [(dtime(7, 0), dtime(10, 0))]),
    "ny_pm": ("America/New_York", [(dtime(13, 30), dtime(16, 0))]),
    # ICT Silver Bullet windows, New York local time.
    "silver_bullet": ("America/New_York", [
        (dtime(3, 0), dtime(4, 0)),
        (dtime(10, 0), dtime(11, 0)),
        (dtime(14, 0), dtime(15, 0)),
    ]),
}


def _as_zone(df: pd.DataFrame, idx: int, zone: str) -> pd.Timestamp:
    ts = pd.Timestamp(df["date"].iloc[idx])
    if ts.tzinfo is None:
        return ts.tz_localize(zone, ambiguous="NaT", nonexistent="shift_forward")
    return ts.tz_convert(zone)


def in_session(df: pd.DataFrame, idx: int, market: str, gate: SessionGate) -> bool:
    """Return whether a closed bar falls in the requested cash/session window."""
    if gate == "none":
        return True
    if gate == "auto":
        if market in {"cn", "hk"}:
            key = market
        elif market == "other":
            key = "ny_am"
        else:
            return True
    else:
        key = gate
    zone, windows = _SESSION_WINDOWS[key]
    t = _as_zone(df, idx, zone).time()
    return any(start <= t <= end for start, end in windows)


def dealing_range(swings: Iterable, idx: int, lookback_bars: int = 120) -> DealingRange | None:
    """Last confirmed swing high/low pair, with both sides recent enough."""
    highs = [s for s in swings if getattr(s, "kind", "") == "H" and getattr(s, "confirmed_at", 0) <= idx]
    lows = [s for s in swings if getattr(s, "kind", "") == "L" and getattr(s, "confirmed_at", 0) <= idx]
    if not highs or not lows:
        return None
    h = highs[-1]
    l = lows[-1]
    if idx - int(h.confirmed_at) > lookback_bars or idx - int(l.confirmed_at) > lookback_bars:
        return None
    high = max(float(h.price), float(l.price))
    low = min(float(h.price), float(l.price))
    if high <= low:
        return None
    return DealingRange(high, low, (high + low) / 2, int(h.index), int(l.index))


def ote_from_signal(df: pd.DataFrame, start: int, end: int, entry: float, direction: str) -> float | None:
    """Retracement measured from the signal's own delivery range.

    The global zigzag is often mid-leg when a CISD fires, so using the last two
    zigzag points can point the wrong way.  The CISD start/end range is known at
    the signal bar and is the range that actually produced the entry.
    """
    if start < 0 or end < start or end >= len(df):
        return None
    high = float(df["high"].iloc[start:end + 1].max())
    low = float(df["low"].iloc[start:end + 1].min())
    if high <= low or not (low <= entry <= high):
        return None
    if direction == "bull":
        return (high - entry) / (high - low)
    return (entry - low) / (high - low)


def ote_retracement(swings: Iterable, entry: float, direction: str) -> float | None:
    """Retracement of the latest completed impulse leg, as a 0..1 fraction.

    For a bullish leg the leg is low -> high and retracement is
    ``(high - entry) / (high - low)``.  For a bearish leg the mirror is used.
    """
    zz = [s for s in swings if getattr(s, "kind", "") in {"H", "L"}]
    if len(zz) < 2:
        return None
    a, b = zz[-2], zz[-1]
    if direction == "bull":
        if a.kind != "L" or b.kind != "H":
            return None
        low, high = float(a.price), float(b.price)
        if high <= low:
            return None
        return min(max((high - entry) / (high - low), 0.0), 1.0)
    if a.kind != "H" or b.kind != "L":
        return None
    high, low = float(a.price), float(b.price)
    if high <= low:
        return None
    return min(max((entry - low) / (high - low), 0.0), 1.0)
