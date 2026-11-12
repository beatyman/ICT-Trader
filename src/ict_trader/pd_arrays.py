"""Breaker / mitigation / rejection arrays.

This module is deliberately independent of ``concepts.py``: it consumes
duck-typed swings, order blocks and OHLCV, so the detector can be tested alone
and does not create a circular import.

P1-09 completes the breaker family:

* the **V20 declaration** (:data:`BREAKER_FAILURE_TESTS`,
  :data:`BREAKER_GEOMETRIES`): the failure test and the zone geometry are
  declared, never inferred.  ``AR-21`` attests four failure tests and ``AR-22``
  four geometries; the two axes are independent.
* the **retest gate** (``AR-23``): the break creates the object, the entry
  requires the later return.  There is no entry on the failure candle.  The
  retest window is the labelled **repo** value ``12`` bars
  (``SYSTEM_SPEC.md`` §13.6 "Breaker retest window | 12 bars (first-party plan,
  unimplemented) | repo"): it is not a corpus number and is never promoted.
* the **breaker tier** (``PP:C9``/R4) stays as declared by P0-20.

``PDAArray.entry_eligible`` is the single read path for "may this breaker be
used as an entry array": it is false before the retest and after the declared
retest window.  ``PDAArray.usable_at`` is its bar-indexed form.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Literal

import numpy as np
import pandas as pd

from .context import atr

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


# --------------------------------------------------------------------------- #
# V20: the two breaker declaration axes (AR-21 failure test, AR-22 geometry)
# --------------------------------------------------------------------------- #
#: AR-21: four non-equivalent failure tests.  None is a default in the corpus;
#: a run must declare one.
BREAKER_FAILURE_TESTS: tuple[str, ...] = (
    "trade_through", "close", "full_body", "buffered_0.10_atr")
#: The repo's declared reading: ``close`` -- the corpus's body-close convention
#: (AR-18) and the test ``detect_order_blocks`` already implements.
DEFAULT_BREAKER_FAILURE = "close"
BREAKER_FAILURE_SOURCES: dict[str, dict[str, str]] = {
    "trade_through": {
        "label": "corpus",
        "ar21": "a bearish OB fails when price trades above OB.top (mirror)",
        "r12": "variant (a): a trade through the zone",
    },
    "close": {
        "label": "corpus",
        "ar21": "a body close beyond the far boundary",
        "r12": "variant (b): close beyond",
    },
    "full_body": {
        "label": "corpus",
        "ar21": "the full body closes beyond both boundaries",
        "r12": "variant (c): full body close beyond both",
    },
    "buffered_0.10_atr": {
        "label": "corpus",
        "ar21": "buffered +/- 0.10*ATR beyond the boundary",
        "r12": "variant (d): 0.10*ATR buffer",
    },
}

#: AR-22: four non-equivalent breaker rectangles.  ``body_intersect_fvg`` is the
#: "Unicorn" (G4) and needs the FVG inventory at detection time.
BREAKER_GEOMETRIES: tuple[str, ...] = (
    "full_range", "body", "parent_ob", "body_intersect_fvg")
#: The repo's declared reading: ``parent_ob`` (G3) -- the zone is the parent
#: OB's bounds, which is what this module emitted before V20 was declared.
DEFAULT_BREAKER_GEOMETRY = "parent_ob"
BREAKER_GEOMETRY_SOURCES: dict[str, dict[str, str]] = {
    "full_range": {"label": "corpus", "ar22": "G1: the selected candle's [low, high]"},
    "body": {"label": "corpus", "ar22": "G2: the selected candle's body"},
    "parent_ob": {"label": "corpus", "ar22": "G3: the parent OB's bounds after the flip"},
    "body_intersect_fvg": {
        "label": "corpus", "ar22": "G4: G2 body intersected with an FVG (Unicorn)"},
}

#: AR-21 variant (d): the buffer is corpus (``0.10 x ATR``), not invented.
BREAKER_ATR_BUFFER = 0.10

#: AR-23: "retest is a condition of use"; ``SYSTEM_SPEC.md`` §13.6 labels the
#: 12-bar retest window **repo** (first-party plan, not corpus).
BREAKER_RETEST_WINDOW = 12
BREAKER_RETEST_WINDOW_SOURCE = "repo"
BREAKER_RETEST_WINDOW_CITATION = (
    "SYSTEM_SPEC.md §13.6: breaker retest window 12 bars (first-party plan, "
    "unimplemented); AR-23 requires the later return")


def validate_breaker_failure(test: str) -> str:
    """Refuse an undeclared AR-21 failure test instead of choosing one."""
    if test not in BREAKER_FAILURE_TESTS:
        raise ValueError(
            f"unknown breaker failure test {test!r}; declare one of "
            f"{BREAKER_FAILURE_TESTS} (V20/AR-21)")
    return test


def validate_breaker_geometry(geometry: str) -> str:
    """Refuse an undeclared AR-22 geometry instead of choosing one."""
    if geometry not in BREAKER_GEOMETRIES:
        raise ValueError(
            f"unknown breaker geometry {geometry!r}; declare one of "
            f"{BREAKER_GEOMETRIES} (V20/AR-22)")
    return geometry


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
    #: P1-08 stamp: CB-07 knowledge bar (the flip/confirmation bar).
    known_at: int | None = None
    #: LQ-06 state (the breaker family only ever emits ``intact``).
    state: str = "intact"
    #: AR-23: the bar where the parent OB failed under the declared V20 test.
    failure_bar: int | None = None
    failure_test: str = ""
    #: AR-22: the declared geometry that produced the zone.
    geometry: str = ""
    #: AR-09/AR-23: first return into the zone after ``failure_bar`` (within
    #: :data:`BREAKER_RETEST_WINDOW`).  ``None`` means "not yet retested".
    retest_at: int | None = None
    retest_window: int = 0
    #: AS-09: an array whose construction constant is ``missing`` may be
    #: reference geometry but may not become an entry array.
    reference_only: bool = False

    @property
    def mid(self) -> float:
        return (self.top + self.bottom) / 2

    def distance(self, price: float) -> float:
        if self.bottom <= price <= self.top:
            return 0.0
        return min(abs(price - self.top), abs(price - self.bottom))

    @property
    def retested(self) -> bool:
        """AR-23: the later return has happened (the object is now usable)."""
        return self.retest_at is not None

    @property
    def entry_eligible(self) -> bool:
        """AS-09 + AR-23: reference geometry is never an entry array.

        A breaker becomes entry-eligible only after its retest; the retest
        itself is not an invitation to enter on the failure candle.
        """
        return bool(not self.reference_only and self.retested)

    def usable_at(self, at: int) -> bool:
        """AR-23 at bar ``at``: after the retest and inside the window."""
        if self.kind != "breaker":
            return True
        if not self.retested:
            return False
        start = int(self.retest_at)  # type: ignore[arg-type]
        return start <= int(at) <= start + int(self.retest_window)

    def state_at(self, at: int) -> str | None:
        """LQ-06 state at ``at``; ``None`` before the object was knowable."""
        known = self.known_at if self.known_at is not None else self.confirmed_at
        if int(at) < int(known):
            return None
        return self.state


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


def _ohlc(df: pd.DataFrame) -> tuple[np.ndarray, ...]:
    return (df["open"].to_numpy(float), df["high"].to_numpy(float),
            df["low"].to_numpy(float), df["close"].to_numpy(float))


def breaker_failure_bar(ob: object, df: pd.DataFrame, *,
                        test: str = DEFAULT_BREAKER_FAILURE) -> int | None:
    """AR-21: the first bar after ``ob.created`` at which the OB fails.

    A **bullish** OB (support) fails downward; a **bearish** OB (resistance)
    fails upward.  The four declared tests are not equivalent and the corpus
    supplies no default (V20 ``BLOCK``); :data:`DEFAULT_BREAKER_FAILURE` is the
    labelled repo reading ``close``.
    """
    validate_breaker_failure(test)
    opens, highs, lows, closes = _ohlc(df)
    direction = "bull" if str(getattr(ob, "direction")) == "bull" else "bear"
    top, bottom = float(getattr(ob, "top")), float(getattr(ob, "bottom"))
    start = int(getattr(ob, "created", getattr(ob, "right", 0))) + 1
    n = len(df)
    if start >= n:
        return None
    if test == "trade_through":
        mask = (lows[start:] < bottom) if direction == "bull" else (highs[start:] > top)
    elif test == "close":
        mask = (closes[start:] < bottom) if direction == "bull" else (closes[start:] > top)
    elif test == "full_body":
        body_low = np.minimum(opens, closes)
        body_high = np.maximum(opens, closes)
        mask = ((body_high[start:] < bottom) if direction == "bull"
                else (body_low[start:] > top))
    else:  # buffered_0.10_atr
        buffer = BREAKER_ATR_BUFFER * atr(df).to_numpy(float)
        mask = ((closes[start:] < bottom - buffer[start:]) if direction == "bull"
                else (closes[start:] > top + buffer[start:]))
    if not bool(mask.any()):
        return None
    return start + int(np.argmax(mask))


def breaker_geometry_zone(ob: object, df: pd.DataFrame, *,
                          geometry: str = DEFAULT_BREAKER_GEOMETRY,
                          fvgs: Iterable = ()) -> tuple[float, float] | None:
    """AR-22: the declared breaker rectangle for a failed parent OB.

    ``full_range``/``body``/``body_intersect_fvg`` read the parent candle at
    ``ob.left`` (the AR-15 candidate); ``parent_ob`` keeps the OB's own bounds.
    All bounds are returned normalised as ``(top, bottom)``; a degenerate
    rectangle is refused (``None``) rather than emitted as a line.
    """
    validate_breaker_geometry(geometry)
    if geometry == "parent_ob":
        top, bottom = float(getattr(ob, "top")), float(getattr(ob, "bottom"))
    else:
        opens, highs, lows, closes = _ohlc(df)
        index = int(getattr(ob, "left", getattr(ob, "created", 0)))
        if index < 0 or index >= len(df):
            return None
        if geometry == "full_range":
            top, bottom = float(highs[index]), float(lows[index])
        else:  # body / body_intersect_fvg
            top = float(max(opens[index], closes[index]))
            bottom = float(min(opens[index], closes[index]))
    if geometry == "body_intersect_fvg":
        best: tuple[float, float] | None = None
        for f in fvgs:
            if str(getattr(f, "label", "")) != "FVG" or bool(getattr(f, "inverted", False)):
                continue
            upper = min(top, float(getattr(f, "top")))
            lower = max(bottom, float(getattr(f, "bottom")))
            if upper > lower and (best is None or upper - lower > best[0] - best[1]):
                best = (upper, lower)
        if best is None:
            return None
        top, bottom = best
    if top <= bottom or not np.isfinite([top, bottom]).all():
        return None
    return top, bottom


def breaker_retest_at(breaker: object, df: pd.DataFrame, *,
                      window: int = BREAKER_RETEST_WINDOW) -> int | None:
    """AR-23: first return into the breaker zone **after** the failure candle.

    ``window`` is the labelled repo 12-bar window; a return after it is not a
    retest of this breaker.  The failure candle itself never counts (no entry
    on the failure candle).
    """
    start = int(getattr(breaker, "failure_bar", None)
                if getattr(breaker, "failure_bar", None) is not None
                else getattr(breaker, "confirmed_at", getattr(breaker, "created", 0)))
    top, bottom = float(getattr(breaker, "top")), float(getattr(breaker, "bottom"))
    direction = "bull" if str(getattr(breaker, "direction")) == "bull" else "bear"
    highs, lows = df["high"].to_numpy(float), df["low"].to_numpy(float)
    stop = min(len(df) - 1, start + int(window))
    for t in range(start + 1, stop + 1):
        if direction == "bull":
            if lows[t] <= top:
                return t
        elif highs[t] >= bottom:
            return t
    return None


def detect_breakers(order_blocks: Iterable, sweeps: Iterable = (),
                    *, tier: str = DEFAULT_BREAKER_TIER,
                    df: pd.DataFrame | None = None,
                    fvgs: Iterable = (),
                    failure_test: str = DEFAULT_BREAKER_FAILURE,
                    geometry: str = DEFAULT_BREAKER_GEOMETRY) -> list[PDAArray]:
    """A mitigated order block flips into a breaker on the other side.

    The **tier is declared**, never inferred (PP:C9/R4, P0-20):

    * ``minimal`` -- the labelled repo tier: a failed OB flips, no sequence.
    * ``sequenced`` (default) -- the corpus breaker *proper*: a liquidity sweep
      on the parent OB's side must be known at or before the flip bar (R6).
      With no matching sweep no breaker is emitted; a bare ``mitigated`` flag is
      not a breaker.

    **V20** adds two independent declarations.  ``failure_test`` (AR-21) is the
    event that fails the parent OB; ``geometry`` (AR-22) is the rectangle the
    breaker occupies.  With no ``df`` the detector can only evaluate the
    ``close`` test from ``ob.right`` and the ``parent_ob`` rectangle from the OB
    itself, so any other declared value raises instead of silently degrading.

    **AR-23**: when ``df`` is supplied the later return is recorded as
    ``retest_at`` (inside the labelled repo 12-bar window).  The object exists
    at the failure bar; it becomes usable only at the retest
    (:attr:`PDAArray.entry_eligible`).
    """
    if tier not in BREAKER_TIERS:
        raise ValueError(
            f"unknown breaker tier {tier!r}; declare one of {BREAKER_TIERS}")
    validate_breaker_failure(failure_test)
    validate_breaker_geometry(geometry)
    if df is None and (failure_test != "close" or geometry != "parent_ob"):
        raise ValueError(
            "the declared breaker failure test/geometry needs OHLC: pass df=; "
            "without it only ('close', 'parent_ob') is evaluable")
    sweep_list = list(sweeps)
    fvg_list = list(fvgs)
    out: list[PDAArray] = []
    for ob in order_blocks:
        direction: Direction = "bear" if getattr(ob, "direction") == "bull" else "bull"
        if df is not None:
            failed = breaker_failure_bar(ob, df, test=failure_test)
            if failed is None:
                continue
        else:
            if not bool(getattr(ob, "mitigated", False)):
                continue
            failed = int(getattr(ob, "right", getattr(ob, "created", 0)))
        if tier == "sequenced" and not any(
                _breaker_sweep_ok(s, direction, failed) for s in sweep_list):
            continue
        if df is not None:
            zone = breaker_geometry_zone(ob, df, geometry=geometry, fvgs=fvg_list)
            if zone is None:
                continue
            top, bottom = zone
        else:
            top, bottom = float(ob.top), float(ob.bottom)
        array = PDAArray("breaker", direction, top, bottom,
                         failed, failed, f"{tier}_mitigated_ob", tier,
                         failure_bar=failed, failure_test=failure_test,
                         geometry=geometry,
                         retest_window=(BREAKER_RETEST_WINDOW
                                        if df is not None else 0))
        if df is not None:
            array.retest_at = breaker_retest_at(array, df)
        out.append(array)
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
