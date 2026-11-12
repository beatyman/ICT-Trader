"""Optional market-context gates: session, premium/discount and OTE.

These are deliberately pure functions over already-known bars.  They do not
change the default frozen setup; `SetupConfig` opts into them one at a time so
each can be ablated.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import time as dtime
from enum import Enum
from typing import Iterable, Literal
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

Market = Literal["cn", "hk", "crypto", "other"]
SessionGate = Literal["none", "auto", "asian", "asian_dr", "london", "london_close", "ny_am", "ny_pm",
                      "silver_bullet", "macro_0950", "venom", "cn", "hk"]


class BiasState(str, Enum):
    """The one declared bias vocabulary (BI-03; SPEC §13.2).

    The canonical wire values are ``bullish`` / ``bearish`` / ``neutral``.
    The repo's ``flat`` and the realtime layer's ``neutral`` name the same
    state; they are aliases into this enum, never separate states.  The
    corpus's ``+unclear`` / ``+ambiguous`` forks are deliberately absent: they
    are a declared vocabulary choice (P1-02), not interchangeable spellings.
    """
    BULL = "bullish"
    BEAR = "bearish"
    NEUTRAL = "neutral"


#: Every repo spelling of a bias/direction token, mapped onto the enum.
_BIAS_ALIASES: dict[str, BiasState] = {
    "bull": BiasState.BULL,
    "bullish": BiasState.BULL,
    "long": BiasState.BULL,
    "bear": BiasState.BEAR,
    "bearish": BiasState.BEAR,
    "short": BiasState.BEAR,
    "flat": BiasState.NEUTRAL,
    "neutral": BiasState.NEUTRAL,
}


def parse_bias(value: object) -> BiasState:
    """Map any repo spelling of a bias state onto :class:`BiasState` (BI-03).

    Unknown spellings raise instead of being coerced: a second vocabulary is
    exactly the defect this enum removes.
    """
    if isinstance(value, BiasState):
        return value
    key = str(value).strip().lower()
    try:
        return _BIAS_ALIASES[key]
    except KeyError:
        raise ValueError(
            f"unknown bias state {value!r}; use one of "
            f"{[state.value for state in BiasState]}") from None


def serialize_bias(value: object) -> str:
    """The single serializer at every bias output boundary.

    ``None`` means *no reading* and is not ``neutral``: it serializes to the
    empty string so "no bias" is never collapsed into a direction or a neutral
    veto (P0-04/P1-02).  All other spellings go through :func:`parse_bias`.
    """
    if value is None:
        return ""
    return parse_bias(value).value


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
    # Declared half-open: the 00:00 end is exclusive, not 23:59:59.
    "asian": ("America/New_York", [(dtime(19, 0), dtime(0, 0))]),
    # doc 601 (liquiditysweeps.com): "The Asian range (00:00-02:00 EST) functions as
    # a micro-dealing range for the London open."  Kept as a separate key because
    # the legacy "asian" window above uses the 19:00-00:00 NY convention.
    "asian_dr": ("America/New_York", [(dtime(0, 0), dtime(2, 0))]),
    # doc 546: "the 09:50 NY-AM macro window ... concentrated inside that 20-minute
    # window"; doc 491 lists 09:50 as a highest-conviction FVG window.
    "macro_0950": ("America/New_York", [(dtime(9, 50), dtime(10, 10))]),
    # doc 69 (ICT Venom model): "a raid of the 08:00-09:30 New York range after the
    # 09:30 open".  The gate selects the pre-open range that gets raided.
    "venom": ("America/New_York", [(dtime(8, 0), dtime(9, 30))]),
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


def _as_zone(df: pd.DataFrame, idx: int, zone: str, market: str = "other") -> pd.Timestamp:
    ts = pd.Timestamp(df["date"].iloc[idx])
    if ts.tzinfo is None:
        source = {
            "cn": "Asia/Shanghai",
            "hk": "Asia/Hong_Kong",
            "crypto": "UTC",
            "other": "UTC",
        }.get(market, "UTC")
        return ts.tz_localize(source, ambiguous="NaT", nonexistent="shift_forward").tz_convert(zone)
    return ts.tz_convert(zone)


def _in_window(t: dtime, start: dtime, end: dtime) -> bool:
    """Half-open window test: ``[start, end)`` on the session wall clock.

    A declared end of ``00:00`` means the window runs to *next-day* midnight,
    which is the exclusive end instant (``19:00-00:00`` is 19:00 up to but not
    including 00:00).  Such a window crosses the date boundary, so its start is
    later than its end on the clock.
    """
    if start <= end:
        return start <= t < end
    return t >= start or t < end


def in_session(df: pd.DataFrame, idx: int, market: str, gate: SessionGate) -> bool:
    """Return whether a closed bar falls in the requested cash/session window.

    Naive timestamps are interpreted in the market's native timezone (UTC for
    crypto) and then converted to the session timezone.  This matters for the
    New York kill zones: Binance 15m timestamps are UTC, not New York local.
    """
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
    t = _as_zone(df, idx, zone, market).time()
    return any(_in_window(t, start, end) for start, end in windows)


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


class SwingIndex:
    """O(1)-per-bar view over one zigzag swing list.

    ``dealing_range`` and ``structure_bias`` filter the whole swing list on
    every call, which is O(swings) inside a per-signal loop (measured: 3.6 s of
    a 12.8 s ``build_setup`` on 20k BTC 15m bars).  The zigzag list is ordered
    by bar index and ``confirmed_at`` grows with it, so "the last N confirmed
    swings at bar t" is a binary search instead of a scan.  Results are
    identical to the functions above - ``tests/test_context.py`` checks every
    bar against them.
    """

    def __init__(self, swings: Iterable) -> None:
        swings = list(swings)
        for key, kind in (("h", "H"), ("l", "L")):
            sub = [s for s in swings if getattr(s, "kind", "") == kind]
            setattr(self, f"{key}_index", np.array([int(s.index) for s in sub], dtype=np.int64))
            setattr(self, f"{key}_conf", np.array([int(getattr(s, "confirmed_at", s.index))
                                                  for s in sub], dtype=np.int64))
            setattr(self, f"{key}_price", np.array([float(s.price) for s in sub], dtype=float))

    def _n(self, key: str, t: int) -> int:
        return int(np.searchsorted(getattr(self, f"{key}_conf"), int(t), side="right"))

    def structure_bias(self, t: int, lookback: int = 2) -> str:
        """HH/HL (``bullish``) or LH/LL (``bearish``) over confirmed swings.

        This is the only structure read that sets bias (BI-01).  A mixed pair
        or too few confirmed swings is ``neutral``: the repo declares the
        mixed-pair outcome that the corpus leaves undefined (BI-02/BI-03).
        """
        nh, nl = self._n("h", t), self._n("l", t)
        if nh >= lookback and nl >= lookback:
            hp, lp = self.h_price, self.l_price
            hh = hp[nh - 1] > hp[nh - lookback]
            hl = lp[nl - 1] > lp[nl - lookback]
            lh = hp[nh - 1] < hp[nh - lookback]
            ll = lp[nl - 1] < lp[nl - lookback]
            if hh and hl:
                return serialize_bias(BiasState.BULL)
            if lh and ll:
                return serialize_bias(BiasState.BEAR)
        return serialize_bias(BiasState.NEUTRAL)

    def dealing_range(self, t: int, lookback_bars: int = 120) -> DealingRange | None:
        nh, nl = self._n("h", t), self._n("l", t)
        if nh == 0 or nl == 0:
            return None
        hi, li = nh - 1, nl - 1
        if (int(t) - int(self.h_conf[hi]) > lookback_bars
                or int(t) - int(self.l_conf[li]) > lookback_bars):
            return None
        high = max(float(self.h_price[hi]), float(self.l_price[li]))
        low = min(float(self.h_price[hi]), float(self.l_price[li]))
        if high <= low:
            return None
        return DealingRange(high, low, (high + low) / 2,
                            int(self.h_index[hi]), int(self.l_index[li]))


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


def candle2_closure(prior_high: float, prior_low: float,
                    cur_high: float, cur_low: float, cur_close: float) -> str | None:
    """ICT Candle-2 fake-break closure (E1).

    A bullish C2 sweeps the prior low and closes back inside the prior range
    (above the prior low); a bearish C2 sweeps the prior high and closes back
    inside (below the prior high).  ``None`` when neither happens.

    This is a daily/HTF bias filter, not a standalone entry.
    """
    if cur_low < prior_low and cur_close > prior_low:
        return "bull"
    if cur_high > prior_high and cur_close < prior_high:
        return "bear"
    return None


def candle3_closure(prior_high: float, prior_low: float,
                    c2_open: float, c2_close: float, c2_high: float, c2_low: float,
                    c3_open: float, c3_close: float) -> str | None:
    """ICT Candle-3 reversal (E1).

    C2 fails when the next bar (C3) engulfs C2's body and closes through it.
    The returned direction is the *new* bias (opposite of the failed C2):
    a bullish C2 that gets engulfed downward yields ``"bear"``, a bearish C2
    engulfed upward yields ``"bull"``.
    """
    c2_dir = candle2_closure(prior_high, prior_low, c2_high, c2_low, c2_close)
    if c2_dir is None:
        return None
    body_hi = max(float(c2_open), float(c2_close))
    body_lo = min(float(c2_open), float(c2_close))
    if c2_dir == "bull":
        # C2 hinted long; C3 engulfs its body and closes through the low.
        if float(c3_close) < body_lo:
            return "bear"
    else:
        # C2 hinted short; C3 engulfs its body and closes through the high.
        if float(c3_close) > body_hi:
            return "bull"
    return None


def structure_bias(swings: Iterable, t: int, lookback: int = 2) -> str:
    """ICT bias structure: HH/HL vs LH/LL (BI-01, corpus high-consensus).

    Bullish = the last two swing highs are rising AND the last two swing lows
    are rising (higher-high / higher-low).  Bearish = the mirror.  Anything
    else (mixed, or fewer than two swings on each side) is ``"neutral"``, the
    declared repo outcome for the mixed pair the corpus leaves undefined
    (BI-02/BI-03).  This HH/HL read is the **only** ``structure_bias``; the
    realtime pivot/BOS read is named ``pivot_bos_bias`` instead of sharing
    this name (P0-09).
    """
    highs = [s for s in swings if getattr(s, "kind", "") == "H" and int(s.confirmed_at) <= t]
    lows = [s for s in swings if getattr(s, "kind", "") == "L" and int(s.confirmed_at) <= t]
    if len(highs) >= lookback and len(lows) >= lookback:
        h1, h2 = highs[-lookback], highs[-1]
        l1, l2 = lows[-lookback], lows[-1]
        hh = float(h2.price) > float(h1.price)
        hl = float(l2.price) > float(l1.price)
        lh = float(h2.price) < float(h1.price)
        ll = float(l2.price) < float(l1.price)
        if hh and hl:
            return serialize_bias(BiasState.BULL)
        if lh and ll:
            return serialize_bias(BiasState.BEAR)
    return serialize_bias(BiasState.NEUTRAL)
