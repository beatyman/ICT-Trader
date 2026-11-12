"""Breaker / mitigation / rejection arrays.

This module is deliberately independent of ``concepts.py``: it consumes
duck-typed swings, order blocks and OHLCV, so the detector can be tested alone
and does not create a circular import.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Literal

import numpy as np
import pandas as pd

Direction = Literal["bull", "bear"]
ArrayKind = Literal["breaker", "mitigation", "rejection"]


#: PP:C9 / breaker R4: the corpus attests **two** breaker tiers and they are not
#: the same object.  ``minimal`` is the one-sentence "a failed order block that
#: flips"; ``sequenced`` is the breaker *proper* -- liquidity is taken first,
#: then the order block fails, then the opposite side is retested.  Only the
#: sequenced form carries the R6 sweep precondition.
BREAKER_TIERS = ("minimal", "sequenced")
DEFAULT_BREAKER_TIER = "sequenced"
BREAKER_TIER_SOURCES: dict[str, dict[str, str]] = {
    "minimal": {
        "label": "repo",
        "r4": "tier (i): failed OB flips, no sequence",
        "source": "labelled minimal tier; no sweep precondition",
    },
    "sequenced": {
        "label": "corpus",
        "r4": "tier (ii): liquidity taken -> OB fails -> opposite-side retest",
        "r6": "a breaker needs a liquidity sweep first (not only a close)",
        "source": "declared breaker-proper tier",
    },
}

#: The sweep each post-flip side requires (R6): a bullish breaker is a failed
#: *bearish* OB, so sell-side liquidity must have been taken first; a bearish
#: breaker is a failed *bullish* OB and needs buy-side liquidity.  In
#: ``sweeps.detect_sweeps`` an SSL raid is labelled ``direction="bull"`` and a
#: BSL raid ``direction="bear"``, so the post-flip side names its sweep.
BREAKER_SWEEP_DIRECTION: dict[str, str] = {"bull": "bull", "bear": "bear"}


@dataclass
class PDAArray:
    kind: ArrayKind
    direction: Direction
    top: float
    bottom: float
    created: int
    confirmed_at: int
    source: str = ""
    #: The declared tier that produced a breaker (empty for other arrays).
    tier: str = ""

    @property
    def mid(self) -> float:
        return (self.top + self.bottom) / 2

    def distance(self, price: float) -> float:
        if self.bottom <= price <= self.top:
            return 0.0
        return min(abs(price - self.top), abs(price - self.bottom))


def detect_rejections(df: pd.DataFrame, swings: Iterable, wick_ratio: float = 0.5) -> list[PDAArray]:
    """Long-wick rejection at a confirmed swing high/low."""
    high = df["high"].to_numpy(float)
    low = df["low"].to_numpy(float)
    open_ = df["open"].to_numpy(float)
    close = df["close"].to_numpy(float)
    out: list[PDAArray] = []
    for s in swings:
        i = int(s.index)
        if i >= len(df) or int(getattr(s, "confirmed_at", i)) >= len(df):
            continue
        rng = high[i] - low[i]
        if rng <= 0:
            continue
        body_hi = max(open_[i], close[i])
        body_lo = min(open_[i], close[i])
        if s.kind == "H":
            upper = high[i] - body_hi
            if upper / rng >= wick_ratio and upper > 0:
                out.append(PDAArray("rejection", "bear", float(high[i]), float(body_hi),
                                    i, int(getattr(s, "confirmed_at", i)), "swing_high_wick"))
        else:
            lower = body_lo - low[i]
            if lower / rng >= wick_ratio and lower > 0:
                out.append(PDAArray("rejection", "bull", float(body_lo), float(low[i]),
                                    i, int(getattr(s, "confirmed_at", i)), "swing_low_wick"))
    return out


def _breaker_sweep_ok(sweep: object, direction: Direction,
                      flip_bar: int) -> bool:
    """R6: the sweep must hit the parent OB's side and precede the flip."""
    required = BREAKER_SWEEP_DIRECTION[direction]
    if str(getattr(sweep, "direction", "")) != required:
        return False
    known_at = int(getattr(sweep, "reclaim_bar",
                           getattr(sweep, "sweep_bar", -1)))
    return known_at <= int(flip_bar)


def detect_breakers(order_blocks: Iterable, sweeps: Iterable = (),
                    *, tier: str = DEFAULT_BREAKER_TIER) -> list[PDAArray]:
    """A mitigated order block flips into a breaker on the other side.

    The **tier is declared**, never inferred (PP:C9/R4, P0-20):

    * ``minimal`` -- the labelled repo tier: a failed OB flips, no sequence.
    * ``sequenced`` (default) -- the corpus breaker *proper*: a liquidity sweep
      on the parent OB's side must be known at or before the flip bar (R6).
      With no matching sweep no breaker is emitted; a bare ``mitigated`` flag is
      not a breaker.

    The zone is the parent OB's bounds and the direction is the opposite of the
    parent's (AR-20), unchanged by the tier.
    """
    if tier not in BREAKER_TIERS:
        raise ValueError(
            f"unknown breaker tier {tier!r}; declare one of {BREAKER_TIERS}")
    sweep_list = list(sweeps)
    out: list[PDAArray] = []
    for ob in order_blocks:
        if not bool(getattr(ob, "mitigated", False)):
            continue
        right = int(getattr(ob, "right", getattr(ob, "created", 0)))
        direction: Direction = "bear" if getattr(ob, "direction") == "bull" else "bull"
        if tier == "sequenced" and not any(
                _breaker_sweep_ok(s, direction, right) for s in sweep_list):
            continue
        out.append(PDAArray("breaker", direction, float(ob.top), float(ob.bottom),
                            right, right, f"{tier}_mitigated_ob", tier))
    return out


def detect_mitigations(df: pd.DataFrame, swings: Iterable) -> list[PDAArray]:
    """Failed swing without a sweep: H-L-H lower high or L-H-L higher low."""
    zz = [s for s in swings if getattr(s, "kind", "") in {"H", "L"}]
    close = df["close"].to_numpy(float)
    high = df["high"].to_numpy(float)
    low = df["low"].to_numpy(float)
    open_ = df["open"].to_numpy(float)
    n = len(df)
    out: list[PDAArray] = []
    for a, b, c in zip(zz, zz[1:], zz[2:]):
        if a.kind == "H" and b.kind == "L" and c.kind == "H" and c.price < a.price:
            # failed higher high; confirmation is a close below the middle low
            start = int(getattr(c, "confirmed_at", c.index))
            for t in range(start, n):
                if close[t] < b.price:
                    top = float(high[c.index])
                    bottom = float(min(open_[c.index], close[c.index]))
                    if top > bottom:
                        out.append(PDAArray("mitigation", "bear", top, bottom,
                                            int(c.index), t, "failed_swing_high"))
                    break
        if a.kind == "L" and b.kind == "H" and c.kind == "L" and c.price > a.price:
            start = int(getattr(c, "confirmed_at", c.index))
            for t in range(start, n):
                if close[t] > b.price:
                    top = float(max(open_[c.index], close[c.index]))
                    bottom = float(low[c.index])
                    if top > bottom:
                        out.append(PDAArray("mitigation", "bull", top, bottom,
                                            int(c.index), t, "failed_swing_low"))
                    break
    return out


def near_arrays(arrays: Iterable[PDAArray], price: float, direction: str,
                atr: float, at: int, tol_atr: float = 0.75) -> list[PDAArray]:
    """Arrays on the correct side, known at ``at``, and within a distance budget."""
    tol = max(float(atr) * float(tol_atr), 0.0)
    return [
        a for a in arrays
        if a.direction == direction and a.confirmed_at <= at and a.distance(price) <= tol
    ]


def count_path_liquidity(swings: Iterable, entry: float, target: float,
                         direction: str) -> int:
    """Minor swing levels between entry and target (inducement candidates)."""
    lo, hi = sorted((float(entry), float(target)))
    count = 0
    for s in swings:
        if getattr(s, "kind", "") not in {"H", "L"}:
            continue
        if not (lo < float(s.price) < hi):
            continue
        if direction == "bull" and s.kind == "H":
            count += 1
        elif direction == "bear" and s.kind == "L":
            count += 1
    return count
