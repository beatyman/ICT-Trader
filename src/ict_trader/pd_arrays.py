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


@dataclass
class PDAArray:
    kind: ArrayKind
    direction: Direction
    top: float
    bottom: float
    created: int
    confirmed_at: int
    source: str = ""

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


def detect_breakers(order_blocks: Iterable) -> list[PDAArray]:
    """A mitigated order block flips into a breaker on the other side."""
    out: list[PDAArray] = []
    for ob in order_blocks:
        if not bool(getattr(ob, "mitigated", False)):
            continue
        right = int(getattr(ob, "right", getattr(ob, "created", 0)))
        direction: Direction = "bear" if getattr(ob, "direction") == "bull" else "bull"
        out.append(PDAArray("breaker", direction, float(ob.top), float(ob.bottom),
                            right, right, "mitigated_ob"))
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
