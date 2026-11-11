"""Hidden order block (ICT_Corpus doc 570).

Doc 570 L55-L61 defines a hidden OB as an order block that is invisible on the
higher timeframe because it sits inside the overlapping wicks of two consecutive
same-colour candles:

    "It forms when two same-colour candles overlap each other's wicks. Inside
     that wick overlap - on a lower timeframe - an order block has already
     formed."

    "The hidden order block is the price range from one wick extreme to the
     other. For a bullish setup that's the wick low of the second candle to the
     wick high of the first candle. For a bearish setup it's the inverse."

Boundaries (L98, L110):
    bullish : [low(c2), high(c1)]   - first candle's upper wick, second's lower wick
    bearish : [low(c1), high(c2)]   - first candle's lower wick, second's upper wick

The corpus gives no minimum overlap size, so ``min_overlap_atr`` defaults to 0.0
(any strictly positive overlap qualifies) and is an explicit, auditable knob.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import numpy as np
import pandas as pd

Direction = Literal["bullish", "bearish"]


@dataclass(frozen=True, slots=True)
class HiddenOB:
    """One hidden order block zone, as defined by doc 570."""

    direction: Direction
    top: float
    bottom: float
    first_bar: int
    second_bar: int

    @property
    def span(self) -> float:
        return self.top - self.bottom

    @property
    def ce(self) -> float:
        """50% midpoint, so a hidden OB can be treated like any other PD array."""
        return (self.top + self.bottom) / 2.0

    @property
    def proximal(self) -> float:
        """Side closest to price when price returns into the zone.

        A bullish hidden OB is approached from above (proximal = top); a bearish
        one from below (proximal = bottom).  Doc 570 L124: "You enter near the
        proximal line and place your stop beyond the distal line."
        """
        return self.top if self.direction == "bullish" else self.bottom

    @property
    def distal(self) -> float:
        return self.bottom if self.direction == "bullish" else self.top

    def to_public(self) -> dict[str, float | str | int]:
        return {
            "direction": self.direction, "top": self.top, "bottom": self.bottom,
            "first_bar": self.first_bar, "second_bar": self.second_bar,
            "ce": self.ce, "proximal": self.proximal, "distal": self.distal,
        }


def _atr(high: np.ndarray, low: np.ndarray, n: int = 14) -> float:
    if len(high) == 0:
        return 0.0
    window = min(n, len(high))
    return float(np.mean(np.asarray(high[-window:]) - np.asarray(low[-window:])))


def detect_hidden_ob(frame: pd.DataFrame, *, min_overlap_atr: float = 0.0,
                     max_zone: float | None = None) -> list[HiddenOB]:
    """Return hidden order blocks in ``frame`` (doc 570).

    ``max_zone`` caps how many zones to return (most recent kept); ``None``
    returns all.  ``min_overlap_atr`` rejects zones thinner than that fraction of
    the frame ATR - the corpus states no such threshold, so it is off by default.
    """
    if frame is None or len(frame) < 3:
        return []
    needed = {"open", "high", "low", "close"}
    if not needed.issubset(frame.columns):
        return []
    open_ = frame["open"].to_numpy(float)
    high = frame["high"].to_numpy(float)
    low = frame["low"].to_numpy(float)
    close = frame["close"].to_numpy(float)
    atr = _atr(high, low)
    out: list[HiddenOB] = []

    for i in range(1, len(frame)):
        a, b = i - 1, i
        body_a_hi, body_a_lo = max(open_[a], close[a]), min(open_[a], close[a])
        body_b_hi, body_b_lo = max(open_[b], close[b]), min(open_[b], close[b])

        # Bullish: both candles bullish; first has an upper wick, second a lower
        # wick, and the two wicks overlap -> zone [low(c2), high(c1)].
        if close[a] > open_[a] and close[b] > open_[b]:
            zone_top, zone_bottom = float(high[a]), float(low[b])
            if (high[a] > body_a_hi            # first candle has an upper wick
                    and low[b] < body_b_lo     # second candle has a lower wick
                    and zone_top > zone_bottom):
                if min_overlap_atr <= 0 or (zone_top - zone_bottom) >= min_overlap_atr * atr:
                    out.append(HiddenOB("bullish", zone_top, zone_bottom, a, b))
            continue

        # Bearish: both candles bearish; first has a lower wick, second an upper
        # wick, and the two wicks overlap -> zone [low(c1), high(c2)].
        if close[a] < open_[a] and close[b] < open_[b]:
            zone_top, zone_bottom = float(high[b]), float(low[a])
            if (low[a] < body_a_lo             # first candle has a lower wick
                    and high[b] > body_b_hi    # second candle has an upper wick
                    and zone_top > zone_bottom):
                if min_overlap_atr <= 0 or (zone_top - zone_bottom) >= min_overlap_atr * atr:
                    out.append(HiddenOB("bearish", zone_top, zone_bottom, a, b))

    if max_zone is not None and max_zone >= 0:
        out = out[-int(max_zone):] if max_zone else []
    return out


def nearest_hidden_ob(zones: list[HiddenOB], price: float, direction: str | None = None,
                      tol: float = 0.0) -> HiddenOB | None:
    """Closest hidden OB to ``price`` (optionally within ``tol``)."""
    pool = [z for z in zones if direction is None or z.direction == direction]
    if not pool:
        return None
    def dist(z: HiddenOB) -> float:
        if z.bottom <= price <= z.top:
            return 0.0
        return min(abs(price - z.bottom), abs(price - z.top))
    best = min(pool, key=dist)
    if tol > 0 and dist(best) > tol:
        return None
    return best
