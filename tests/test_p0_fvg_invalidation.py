"""P0-11: FVG/IFVG invalidation is a body close; BPR polarity is real.

Pins the corpus rules named by ``CODE_MAPPING.md`` P0-11:

* AR-04 -- an FVG is invalidated by a **body close** through the far edge
  (bull ``close < bottom``, bear ``close > top``); a wick beyond that edge which
  closes back inside leaves a tested FVG, it does not delete the gap.
* AR-10(a) -- a wick through plus a close back inside is *still* a valid
  directional FVG, so no IFVG flips on a wick.
* AR-11/AR-12 -- BPR is the intersection only (``bottom < top``) of two
  opposite-direction *strict FVGs*; an IFVG (or a flipped gap) is never a
  constituent and the polarity is not hardcoded ``bull``.
"""
from __future__ import annotations

import pandas as pd

from ict_trader.concepts import FVG, ICTConfig, detect_bpr, detect_fvgs, detect_ifvg

# (open, high, low, close) -- C3 completes a bull gap [100, 102] against C1.
BULL_GAP = [
    (99.0, 100.0, 98.0, 99.5),
    (99.5, 103.0, 99.0, 102.5),
    (102.5, 104.0, 102.0, 103.5),
]

# Mirror image: C3 completes a bear gap [103, 105] against C1.
BEAR_GAP = [
    (106.0, 107.0, 105.0, 105.5),
    (105.5, 106.0, 102.0, 102.5),
    (102.5, 103.0, 100.5, 101.0),
]


def _frame(rows: list[tuple[float, float, float, float]]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "date": pd.date_range("2026-01-01", periods=len(rows), freq="15min"),
            "open": [r[0] for r in rows],
            "high": [r[1] for r in rows],
            "low": [r[2] for r in rows],
            "close": [r[3] for r in rows],
            "volume": [1000.0] * len(rows),
        }
    )


def _cfg() -> ICTConfig:
    # Isolation: no displacement gate, no size floors -- only the lifecycle rule
    # under test may decide the outcome.
    return ICTConfig(displacement_filter=False)


# --------------------------------------------------------------------------- #
# AR-04: a wick through the far edge is a test, not an invalidation
# --------------------------------------------------------------------------- #
def test_bull_fvg_survives_a_wick_through_the_far_edge():
    df = _frame(BULL_GAP + [
        (103.5, 104.0, 99.0, 101.0),   # wick below bottom=100, close 101 inside
        (101.0, 103.0, 100.5, 102.0),
    ])
    fvgs = detect_fvgs(df, _cfg())
    assert len(fvgs) == 1
    f = fvgs[0]
    assert (f.direction, f.top, f.bottom, f.created) == ("bull", 102.0, 100.0, 2)
    assert f.active is True, "a wick through the far edge must not delete the gap"
    assert f.mitigated is False
    assert f.touched_at == 3, "the wick is recorded as the tested-at bar"
    assert f.fresh is False, "a tested gap is no longer fresh"
    assert f.right == len(df) - 1, "a live gap extends to the last bar"


def test_bear_fvg_survives_a_wick_through_the_far_edge():
    df = _frame(BEAR_GAP + [
        (101.0, 105.5, 100.5, 103.5),  # wick above top=105, close 103.5 inside
        (103.5, 104.0, 101.5, 103.0),
    ])
    fvgs = detect_fvgs(df, _cfg())
    assert len(fvgs) == 1
    f = fvgs[0]
    assert (f.direction, f.top, f.bottom, f.created) == ("bear", 105.0, 103.0, 2)
    assert f.active is True
    assert f.mitigated is False
    assert f.touched_at == 3
    assert f.fresh is False
    assert f.right == len(df) - 1


# --------------------------------------------------------------------------- #
# AR-04: a body close through the far edge invalidates
# --------------------------------------------------------------------------- #
def test_bull_fvg_dies_on_a_body_close_below_the_far_edge():
    df = _frame(BULL_GAP + [
        (103.5, 104.0, 98.5, 99.0),    # body closes below bottom=100
        (99.0, 103.0, 98.0, 102.5),
    ])
    fvgs = detect_fvgs(df, _cfg())
    assert len(fvgs) == 1
    f = fvgs[0]
    assert f.active is False
    assert f.mitigated is True
    assert f.right == 3, "the invalidation bar ends the gap"
    assert f.touched_at == 3, "the invalidating bar also traded inside the gap"


def test_bear_fvg_dies_on_a_body_close_above_the_far_edge():
    df = _frame(BEAR_GAP + [
        (101.0, 105.5, 100.5, 105.2),  # body closes above top=105
        (105.2, 106.5, 102.0, 106.0),
    ])
    fvgs = detect_fvgs(df, _cfg())
    assert len(fvgs) == 1
    f = fvgs[0]
    assert f.active is False
    assert f.mitigated is True
    assert f.right == 3
    assert f.touched_at == 3


# --------------------------------------------------------------------------- #
# AR-10(a): the IFVG lifecycle inherits the same test
# --------------------------------------------------------------------------- #
def test_ifvg_does_not_flip_on_a_wick_through():
    df = _frame(BULL_GAP + [
        (103.5, 104.0, 99.0, 101.0),   # wick below bottom, closes back inside
        (101.0, 103.0, 100.5, 102.0),
    ])
    parent = detect_fvgs(df, _cfg())[0]
    out = detect_ifvg([parent], df)
    assert out == [], "a wick is not an inversion"
    assert parent.inverted is False
    assert parent.invert_bar is None
    assert parent.active is True


def test_ifvg_flips_on_a_body_close_through_the_far_edge():
    df = _frame(BULL_GAP + [
        (103.5, 104.0, 98.5, 99.0),
        (99.0, 103.0, 98.0, 102.5),
    ])
    parent = detect_fvgs(df, _cfg())[0]
    out = detect_ifvg([parent], df)
    assert len(out) == 1
    ifvg = out[0]
    assert ifvg.direction == "bear"
    assert ifvg.label == "IFVG"
    assert ifvg.invert_bar == 3
    # AR-07/AR-09: the flipped object keeps the parent's bounds and CE.
    assert (ifvg.top, ifvg.bottom, ifvg.ce) == (parent.top, parent.bottom, parent.ce)
    assert parent.inverted is True


# --------------------------------------------------------------------------- #
# AR-11/AR-12: BPR constituents and polarity
# --------------------------------------------------------------------------- #
def test_bpr_is_the_intersection_and_the_newer_gap_sets_polarity():
    bear = FVG("bear", 108.0, 103.0, 1, 6, 1)
    bull = FVG("bull", 105.0, 100.0, 4, 9, 4)  # newer -> BPR is bullish
    out = detect_bpr([bear, bull])
    assert len(out) == 1
    bpr = out[0]
    assert bpr.label == "BPR"
    assert (bpr.top, bpr.bottom) == (105.0, 103.0), "intersection, never the union"
    assert bpr.direction == "bull"
    assert bpr.created == 4

    # Same geometry, opposite order: the newer bearish gap makes it bearish.
    bull_first = FVG("bull", 105.0, 100.0, 1, 4, 1)
    bear_later = FVG("bear", 108.0, 103.0, 3, 9, 3)
    out = detect_bpr([bull_first, bear_later])
    assert len(out) == 1
    assert out[0].direction == "bear", "polarity must not be hardcoded bull"


def test_bpr_never_uses_an_ifvg_as_a_constituent():
    bull = FVG("bull", 105.0, 100.0, 1, 4, 1)
    bear = FVG("bear", 108.0, 103.0, 3, 6, 3)
    # A later IFVG sits on top of the bear gap; it must not be picked instead.
    ifvg = FVG("bear", 112.0, 106.0, 8, 9, 8, label="IFVG", inverted=True)
    out = detect_bpr([bull, bear, ifvg])
    assert len(out) == 1, "an IFVG is a flipped gap, not a BPR constituent"
    assert (out[0].top, out[0].bottom) == (105.0, 103.0)

    # An IFVG alone on one side is not a pair.
    assert detect_bpr([bull, ifvg]) == []

    # A gap that has already flipped in place is not a strict directional FVG.
    flipped = FVG("bear", 108.0, 103.0, 3, 6, 3)
    flipped.inverted = True
    assert detect_bpr([bull, flipped]) == []


def test_bpr_requires_a_strict_overlap():
    bull = FVG("bull", 110.0, 105.0, 1, 4, 1)
    bear = FVG("bear", 105.0, 100.0, 3, 6, 3)  # L == H: touching, no band
    assert detect_bpr([bull, bear]) == []


def test_bpr_needs_opposite_directions():
    bull_a = FVG("bull", 105.0, 100.0, 1, 4, 1)
    bull_b = FVG("bull", 108.0, 103.0, 3, 6, 3)
    assert detect_bpr([bull_a, bull_b]) == []
