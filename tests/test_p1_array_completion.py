"""P1-09 - array family completion.

``CODE_MAPPING.md`` P1-09 asks for seven declarations/behaviours that were
missing from the array family:

* FVG ``0.25 / 0.50 / 0.75`` waypoints (AR-05) as **grading only**;
* IFVG ``t_retest`` (AR-09), the V22 removal rule and the new-direction-only
  admission (AR-08);
* the breaker retest gate (AR-23) and the V20 failure-test/geometry axes
  (AR-21/AR-22);
* the OB admission gates V19 (AR-19);
* the BPR ``6+`` candle floor (AR-13) and close-through invalidation (AR-14);
* the array side tag and declared ``side_mode`` (AS-01..AS-04);
* the roster / priority / scan order (V09/V26, AS-05..AS-09, §7.3).

Missing corpus values are never filled: the ambiguous forks either carry the
four/three corpus options and refuse when undeclared (``NT_VARIANT_UNDECLARED``)
or record the declared reading with its source.  The only numbers used are the
corpus ones (``0.25/0.50/0.75``, ``6+``, ``0.10*ATR``) and the labelled repo
breaker retest window (``12``, SYSTEM_SPEC §13.6).

These tests are new on the pre-P1-09 checkout: the objects do not exist there.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from ict_trader.concepts import (
    ARRAY_MEMBER_CLASSES,
    ARRAY_ORDER_MODES,
    ARRAY_ORDER_SOURCES,
    ARRAY_ROSTERS,
    ARRAY_ROSTER_SIZES,
    ARRAY_SIDES,
    ARRAY_TOTAL_ORDERS,
    BPR_CANDLE_FLOOR,
    DEFAULT_ARRAY_ORDER_MODE,
    DEFAULT_ARRAY_SIDE_MODE,
    DEFAULT_ARRAY_TOTAL_ORDER,
    DEFAULT_OB_EXTENT,
    DEFAULT_OB_GATE,
    ENTRY_MENU,
    FVG,
    FVG_QUADRANT_ROLE,
    FVG_QUADRANTS,
    IFVG_REMOVAL_MODES,
    IFVG_REMOVAL_SOURCES,
    OB_ADMISSION_GATES,
    OB_EXTENTS,
    SIDE_MODES,
    ArrayRoster,
    ICTConfig,
    OrderBlock,
    StructureEvent,
    array_side,
    detect_bpr,
    detect_fvgs,
    detect_ifvg,
    detect_order_blocks,
    ifvg_admits,
    ifvg_removal_bar,
    ifvg_retest_bar,
    resolve_array_order,
    resolve_array_roster,
    scan_arrays,
    validate_ob_extent,
    validate_ob_gate,
)
from ict_trader.pd_arrays import (
    BREAKER_FAILURE_SOURCES,
    BREAKER_FAILURE_TESTS,
    BREAKER_GEOMETRIES,
    BREAKER_GEOMETRY_SOURCES,
    BREAKER_RETEST_WINDOW,
    BREAKER_RETEST_WINDOW_SOURCE,
    DEFAULT_BREAKER_FAILURE,
    DEFAULT_BREAKER_GEOMETRY,
    PDAArray,
    breaker_retest_at,
    detect_breakers,
)
from ict_trader.realtime.analysis.arrays import (
    build_ict_arrays,
    scan_ict_arrays,
)
from ict_trader.variants import RefusalCode, VariantUndeclared, read_variant_spec

# --------------------------------------------------------------------------- #
# Fixtures (closed bars, no wall clock / network input)
# --------------------------------------------------------------------------- #
# C3 completes a bullish FVG [100, 102] against C1.
BULL_GAP = [
    (99.0, 100.0, 98.0, 99.5),
    (99.5, 103.0, 99.0, 102.5),
    (102.5, 104.0, 102.0, 103.5),
]
# A body close below the gap's far edge -> bearish IFVG (new direction = bear).
INVERT = (103.5, 104.0, 98.5, 99.0)
# Price returns above the far edge 100 -> AR-09 retest (and close-back removal).
RETURN = (99.0, 101.0, 98.8, 100.5)
# A second close below 100 -> the vendor "second close" removal.
SECOND_CLOSE = (103.5, 104.0, 97.5, 98.0)
FILLER_UP = [
    (101.0, 103.0, 100.5, 102.0),
    (102.0, 103.0, 101.0, 102.5),
    (102.5, 104.0, 102.0, 103.5),
    (103.5, 105.0, 103.0, 104.5),
    (104.5, 106.0, 104.0, 105.5),
    (105.5, 107.0, 105.0, 106.5),
]

# OB fixture: a bullish OB is selected by the bar-4 MSS (AR-15 t* = 3).
_OB_ROWS = [
    (100.0, 101.0, 99.0, 100.5),
    (100.5, 101.0, 90.0, 91.0),
    (91.0, 95.5, 90.8, 95.0),
    (96.0, 96.5, 94.5, 94.8),
    (94.9, 101.0, 94.7, 100.5),
]
# The failure bar closes below the OB bottom 94.5; bars 6-7 then return into it.
_BREAKER_TAIL = [
    (100.5, 101.0, 93.8, 94.0),
    (94.0, 94.2, 93.0, 93.2),
    (93.2, 94.8, 93.0, 94.4),
]


def _frame(rows: list[tuple[float, float, float, float]]) -> pd.DataFrame:
    return pd.DataFrame({
        "date": pd.date_range("2026-01-01", periods=len(rows),
                              freq="15min", tz="America/New_York"),
        "open": [r[0] for r in rows],
        "high": [r[1] for r in rows],
        "low": [r[2] for r in rows],
        "close": [r[3] for r in rows],
        "volume": [1000.0] * len(rows),
    })


def _times(df: pd.DataFrame) -> list[int]:
    return [int(pd.Timestamp(x).timestamp() * 1000) for x in df["date"]]


def _cfg(**overrides) -> ICTConfig:
    values = dict(displacement_filter=False)
    values.update(overrides)
    return ICTConfig(**values)


def _walk(seed: int = 7, n: int = 400) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = 100 + np.cumsum(rng.normal(0, 0.6, n))
    open_ = np.r_[close[0], close[:-1]] + rng.normal(0, 0.15, n)
    high = np.maximum(open_, close) + rng.uniform(0.1, 0.6, n)
    low = np.minimum(open_, close) - rng.uniform(0.1, 0.6, n)
    dates = pd.date_range("2026-01-01", periods=n, freq="15min", tz="UTC")
    return pd.DataFrame({
        "date": dates, "open": open_, "high": high, "low": low,
        "close": close, "volume": 1000.0,
    })


def _parent(df: pd.DataFrame) -> FVG:
    """The fixture's original bullish FVG, independently of later gaps."""
    gap = next(f for f in detect_fvgs(df, _cfg()) if f.created == 2)
    assert gap.direction == "bull"
    return gap


def _ifvg(df: pd.DataFrame, *, removal: str | None = None) -> FVG:
    return detect_ifvg([_parent(df)], df, removal=removal)[0]


def _ob(df: pd.DataFrame | None = None, **cfg) -> OrderBlock:
    frame = _frame(_OB_ROWS) if df is None else df
    event = [StructureEvent(4, 101.0, "MSS", "bull")]
    return detect_order_blocks(frame, event, [], ICTConfig(**cfg))[0]


class _Sweep:
    def __init__(self, direction: str = "bear", *, reclaim_bar: int = 4,
                 level: float = 95.0):
        self.direction = direction
        self.reclaim_bar = reclaim_bar
        self.level = level
        self.extreme = level


# --------------------------------------------------------------------------- #
# 1. AR-05: FVG quadrant waypoints are grading only
# --------------------------------------------------------------------------- #
def test_fvg_waypoints_are_the_declared_quadrants():
    gap = detect_fvgs(_frame(BULL_GAP), _cfg())[0]
    assert FVG_QUADRANTS == (0.25, 0.50, 0.75)
    assert gap.waypoints == {0.25: 100.5, 0.50: 101.0, 0.75: 101.5}
    assert gap.waypoint(0.50) == gap.ce, "AR-05: the CE is the 0.50 quadrant"
    with pytest.raises(ValueError):
        gap.waypoint(0.40)  # never interpolate an unattested level


def test_fvg_waypoints_are_grading_only_never_an_entry_menu():
    gap = detect_fvgs(_frame(BULL_GAP), _cfg())[0]
    assert FVG_QUADRANT_ROLE == "grading_only"
    entries = gap.entry_levels()
    assert set(entries) == set(ENTRY_MENU) - {"ltf_confirmation"}
    assert set(entries) == {"ce", "near_edge", "far_edge"}
    # No quadrant level is admitted as an entry price (AR-05 vs RE:EX-03).
    assert gap.waypoint(0.25) not in entries.values()
    assert gap.waypoint(0.75) not in entries.values()
    assert entries["near_edge"] == gap.top and entries["far_edge"] == gap.bottom


def test_fvg_waypoints_mirror_on_a_bearish_gap():
    rows = [
        (104.0, 105.0, 103.0, 104.5),
        (104.5, 104.8, 100.0, 100.5),
        (100.5, 100.9, 98.0, 98.5),
    ]
    gap = detect_fvgs(_frame(rows), _cfg())[0]
    assert gap.direction == "bear"
    assert (gap.top, gap.bottom) == (103.0, 100.9)
    assert gap.waypoints == pytest.approx(
        {0.25: 101.425, 0.50: 101.95, 0.75: 102.475})


# --------------------------------------------------------------------------- #
# 2. AR-09: IFVG t_retest is a separate, later event
# --------------------------------------------------------------------------- #
def test_ifvg_t_retest_is_the_first_return_after_inversion():
    df = _frame(BULL_GAP + [INVERT, RETURN] + FILLER_UP)
    ifvg = _ifvg(df)
    assert ifvg.direction == "bear" and ifvg.invert_bar == 3
    assert ifvg.t_retest == 4, "AR-09: first return into the zone"
    assert ifvg.known_at == ifvg.invert_bar == 3
    assert ifvg.t_retest > ifvg.known_at, "the retest is later knowledge"

    # Without the return the retest never exists: price stays below the zone.
    quiet = _frame(BULL_GAP + [INVERT] + [
        (99.0, 99.8, 97.0, 98.5),
        (98.5, 99.5, 97.5, 98.8),
        (98.8, 99.4, 97.6, 98.2),
        (98.2, 99.0, 97.0, 97.6),
        (97.6, 98.4, 96.5, 97.0),
        (97.0, 97.8, 96.0, 96.5),
    ])
    assert _ifvg(quiet).t_retest is None


def test_bullish_ifvg_t_retest_mirrors_the_bearish_case():
    # A bearish gap [100.9, 103] broken upward -> a bullish IFVG (support).
    rows = [
        (104.0, 105.0, 103.0, 103.5),
        (103.5, 104.0, 100.0, 100.5),
        (100.5, 100.9, 98.0, 98.5),
        (100.5, 104.0, 100.0, 103.5),   # body close above the far edge 103
        (103.5, 103.8, 102.0, 102.5),   # return into the zone
        (102.5, 104.0, 102.0, 103.5),
        (103.5, 105.0, 103.0, 104.5),
        (104.5, 106.0, 104.0, 105.5),
    ]
    df = _frame(rows)
    parent = next(f for f in detect_fvgs(df, _cfg()) if f.created == 2)
    ifvg = detect_ifvg([parent], df)[0]
    assert ifvg.direction == "bull" and ifvg.invert_bar == 3
    assert ifvg.t_retest == 4
    assert ifvg_admits(ifvg, "bull") is None
    assert ifvg_admits(ifvg, "bear").code is RefusalCode.NT_ARRAY_WRONG_SIDE


# --------------------------------------------------------------------------- #
# 3. V22: the IFVG removal rule must be declared, and all four options fire
# --------------------------------------------------------------------------- #
def test_ifvg_removal_modes_are_the_v22_register_options():
    assert IFVG_REMOVAL_MODES == ("close-back", "dead-return", "second close",
                                  "never")
    assert set(IFVG_REMOVAL_SOURCES) == set(IFVG_REMOVAL_MODES)
    df = _frame(BULL_GAP + [INVERT] + FILLER_UP)
    ifvg = _ifvg(df)
    assert ifvg.removal_mode == "" and ifvg.removed_at is None
    with pytest.raises(ValueError):
        ifvg_removal_bar(ifvg, df, mode="invented")
    with pytest.raises(ValueError):
        detect_ifvg([_parent(df)], df, removal="closeback")


def test_ifvg_close_back_removes_on_a_close_back_through_the_edge():
    df = _frame(BULL_GAP + [INVERT, RETURN] + FILLER_UP)
    ifvg = _ifvg(df, removal="close-back")
    assert ifvg.removed_at == 4
    assert ifvg.active is False and ifvg.mitigated is True
    assert ifvg.state_at(3) == "intact" and ifvg.state_at(4) == "broken"


def test_ifvg_second_close_needs_a_second_close_through_the_far_edge():
    # Price keeps delivering away from the zone: no close ever returns through
    # 100, so only the "second close" rule fires.
    down = [
        (98.0, 98.5, 96.5, 97.0),
        (97.0, 97.5, 95.5, 96.0),
        (96.0, 96.5, 94.5, 95.0),
        (95.0, 95.5, 93.5, 94.0),
        (94.0, 94.5, 92.5, 93.0),
        (93.0, 93.5, 91.5, 92.0),
    ]
    df = _frame(BULL_GAP + [INVERT, SECOND_CLOSE] + down)
    second = _ifvg(df, removal="second close")
    assert second.removed_at == 4
    assert _ifvg(df, removal="close-back").removed_at is None


def test_ifvg_dead_return_removes_only_after_the_return_visit():
    df = _frame(BULL_GAP + [INVERT, RETURN] + FILLER_UP)
    ifvg = _ifvg(df, removal="dead-return")
    assert ifvg.t_retest == 4
    assert ifvg.removed_at == 6, (
        "AR-20: the pass-through after the return retires the zone")
    assert ifvg.state_at(3) == "intact"
    assert ifvg.state_at(4) == "intact", "the retest itself does not retire it"


def test_ifvg_never_removal_keeps_the_zone():
    df = _frame(BULL_GAP + [INVERT, RETURN] + FILLER_UP)
    ifvg = _ifvg(df, removal="never")
    assert ifvg.removed_at is None and ifvg.active is True
    assert ifvg.removal_mode == "never"


def test_build_ict_arrays_applies_only_a_declared_v22_removal():
    df = _frame(BULL_GAP + [INVERT, RETURN] + FILLER_UP)
    times = _times(df)
    formed = times[3]  # the IFVG formed at the inversion bar

    undeclared = next(
        a for a in build_ict_arrays(df, "TESTUSDT", "15m")
        if a.array_type == "IFVG" and a.formed_at == formed)
    assert undeclared.removal_mode == "" and undeclared.is_active is True

    declared = next(
        a for a in build_ict_arrays(
            df, "TESTUSDT", "15m",
            config=ICTConfig(ifvg_removal="close-back"))
        if a.array_type == "IFVG" and a.formed_at == formed)
    assert declared.removal_mode == "close-back"
    assert declared.invalidated_at == times[4]
    assert declared.state == "broken" and declared.is_active is False

    never = next(
        a for a in build_ict_arrays(
            df, "TESTUSDT", "15m", config=ICTConfig(ifvg_removal="never"))
        if a.array_type == "IFVG" and a.formed_at == formed)
    assert never.removal_mode == "never" and never.is_active is True


# --------------------------------------------------------------------------- #
# 4. AR-08: an IFVG is usable only in its new direction
# --------------------------------------------------------------------------- #
def test_ifvg_is_admitted_only_in_its_new_direction():
    df = _frame(BULL_GAP + [INVERT] + FILLER_UP)
    ifvg = _ifvg(df)
    assert ifvg_admits(ifvg, "bear") is None
    refusal = ifvg_admits(ifvg, "bull")
    assert refusal is not None
    assert refusal.code is RefusalCode.NT_ARRAY_WRONG_SIDE
    # A directional FVG has no such restriction.
    assert ifvg_admits(_parent(df), "bull") is None
    assert ifvg_admits(_parent(df), "bear") is None


def test_selection_does_not_use_an_ifvg_against_its_new_direction():
    df = _frame(BULL_GAP + [INVERT] + FILLER_UP)
    ifvg = _ifvg(df)
    ifvg.signal_id = "ifvg"
    # The IFVG is bearish (premium by type): a short may use it...
    chosen, refusal = scan_arrays(
        [ifvg], roster="corpus_8", direction="short", half=None, at=len(df) - 1)
    assert refusal is None and [a for a in chosen] == [ifvg]
    # ... a long may not, even though the roster admits the class.
    chosen, refusal = scan_arrays(
        [ifvg], roster="corpus_8", direction="long", half=None,
        at=len(df) - 1)
    assert chosen == [] and refusal is not None
    assert refusal.code is RefusalCode.NT_MATRIX_EMPTY


# --------------------------------------------------------------------------- #
# 5. AR-23: the breaker retest gate
# --------------------------------------------------------------------------- #
def test_breaker_retest_window_is_the_labelled_repo_value():
    assert BREAKER_RETEST_WINDOW == 12
    assert BREAKER_RETEST_WINDOW_SOURCE == "repo", (
        "the corpus states no retest window; 12 is the first-party plan and "
        "must stay labelled repo")


def test_breaker_becomes_usable_only_at_the_retest():
    df = _frame(_OB_ROWS + _BREAKER_TAIL)
    breaker = detect_breakers([_ob()], [], tier="minimal", df=df)[0]
    assert breaker.failure_bar == 5
    assert breaker.retest_at == 7
    assert breaker.retest_window == BREAKER_RETEST_WINDOW
    assert breaker.entry_eligible is True
    assert breaker.usable_at(4) is False, "no entry on the failure candle"
    assert breaker.usable_at(6) is False
    assert breaker.usable_at(7) is True
    assert breaker.usable_at(7 + BREAKER_RETEST_WINDOW) is True
    assert breaker.usable_at(8 + BREAKER_RETEST_WINDOW) is False


def test_no_entry_on_the_failure_candle_even_when_it_wick_touches_the_zone():
    # The failure bar's high reaches the OB bottom 94.5 but no later bar does:
    # the retest gate must not count the failure candle itself.
    df = _frame(_OB_ROWS + [
        (100.5, 95.5, 93.5, 93.8),
        (93.8, 94.0, 93.0, 93.2),
        (93.2, 94.0, 93.0, 93.4),
    ])
    breaker = detect_breakers([_ob()], [], tier="minimal", df=df)[0]
    assert breaker.failure_bar == 5
    assert breaker.retest_at is None
    assert breaker.entry_eligible is False


def test_retest_bar_is_the_first_return_after_the_failure_candle():
    df = _frame(_OB_ROWS + [(100.5, 101.0, 93.8, 94.0)]
                + [(94.0, 94.2, 92.8, 93.0)] * 3
                + [(93.0, 94.8, 92.9, 94.4)])
    breaker = detect_breakers([_ob()], [], tier="minimal", df=df)[0]
    assert breaker.failure_bar == 5
    # The later return is at bar 9, four bars after the failure: inside 12.
    assert breaker.retest_at == 9


def test_retest_after_the_declared_window_expires():
    # Failure at bar 5; the zone is not revisited until bar 18 (> 5 + 12), so
    # the labelled repo window (12) refuses to call it a retest of this object.
    tail = [(94.0, 94.2, 92.8, 93.0)] * 12 + [(93.0, 94.8, 92.9, 94.4)]
    df = _frame(_OB_ROWS + [(100.5, 101.0, 93.8, 94.0)] + tail)
    breaker = detect_breakers([_ob()], [], tier="minimal", df=df)[0]
    assert breaker.failure_bar == 5
    assert breaker.retest_at is None
    assert breaker.entry_eligible is False


# --------------------------------------------------------------------------- #
# 6. V20: breaker failure test + geometry are declared, independent axes
# --------------------------------------------------------------------------- #
def test_breaker_v20_declarations_are_corpus_and_labelled():
    assert BREAKER_FAILURE_TESTS == (
        "trade_through", "close", "full_body", "buffered_0.10_atr")
    assert BREAKER_GEOMETRIES == (
        "full_range", "body", "parent_ob", "body_intersect_fvg")
    assert set(BREAKER_FAILURE_SOURCES) == set(BREAKER_FAILURE_TESTS)
    assert set(BREAKER_GEOMETRY_SOURCES) == set(BREAKER_GEOMETRIES)
    assert all(s["label"] == "corpus"
               for s in BREAKER_FAILURE_SOURCES.values())
    assert all(s["label"] == "corpus"
               for s in BREAKER_GEOMETRY_SOURCES.values())
    assert DEFAULT_BREAKER_FAILURE == "close"
    assert DEFAULT_BREAKER_GEOMETRY == "parent_ob"
    assert ICTConfig().breaker_failure_test == DEFAULT_BREAKER_FAILURE
    assert ICTConfig().breaker_geometry == DEFAULT_BREAKER_GEOMETRY
    with pytest.raises(ValueError):
        detect_breakers([_ob()], [], failure_test="invented")
    with pytest.raises(ValueError):
        detect_breakers([_ob()], [], geometry="invented")
    # Without OHLC only ('close', 'parent_ob') is evaluable.
    with pytest.raises(ValueError):
        detect_breakers([_ob()], [], failure_test="trade_through")


def test_breaker_v20_failure_tests_are_not_equivalent():
    df = _frame(_OB_ROWS + _BREAKER_TAIL)
    bars = {}
    for test in BREAKER_FAILURE_TESTS:
        found = detect_breakers([_ob()], [], tier="minimal", df=df,
                                failure_test=test)
        assert found, test
        bars[test] = found[0].failure_bar
    assert bars["trade_through"] == bars["close"] == 5
    assert bars["full_body"] == 6, (
        "the full body only clears the boundary on the next candle")
    assert bars["buffered_0.10_atr"] >= bars["close"], (
        "the buffer can only delay the failure, never advance it")


def test_breaker_v20_geometry_rectangles_are_not_equivalent():
    df = _frame(_OB_ROWS + _BREAKER_TAIL)
    fvg = FVG("bear", 96.2, 95.0, 4, 9, 4)
    zones = {}
    for geometry in BREAKER_GEOMETRIES:
        found = detect_breakers([_ob()], [], tier="minimal", df=df,
                                geometry=geometry, fvgs=[fvg])
        assert found, geometry
        zones[geometry] = (found[0].top, found[0].bottom)
    assert zones["full_range"] == (96.5, 94.5)
    assert zones["body"] == (96.0, 94.8)
    assert zones["parent_ob"] == (96.0, 94.5)
    assert zones["body_intersect_fvg"] == (96.0, 95.0)
    assert len(set(zones.values())) == 4, (
        "AR-22's four rectangles are different objects")
    # G4 needs an overlapping FVG; without one it is refused, not fabricated.
    assert detect_breakers([_ob()], [], tier="minimal", df=df,
                           geometry="body_intersect_fvg") == []


def test_breaker_tier_and_v20_failure_test_compose():
    df = _frame(_OB_ROWS + _BREAKER_TAIL)
    ob = _ob()
    assert detect_breakers([ob], [], tier="sequenced", df=df) == []
    # The R6 sweep must be on the parent OB's (post-flip bearish) side and
    # known by the flip bar.
    assert detect_breakers([ob], [_Sweep("bull", reclaim_bar=4)],
                           tier="sequenced", df=df) == []
    assert detect_breakers([ob], [_Sweep("bear", reclaim_bar=6)],
                           tier="sequenced", df=df) == []
    found = detect_breakers([ob], [_Sweep("bear", reclaim_bar=5)],
                            tier="sequenced", df=df)
    assert len(found) == 1
    assert found[0].tier == "sequenced"
    assert found[0].failure_test == DEFAULT_BREAKER_FAILURE


# --------------------------------------------------------------------------- #
# 7. V19: OB extent and admission gates
# --------------------------------------------------------------------------- #
def test_ob_extent_is_declared_and_unspecified_refuses():
    assert OB_EXTENTS == ("full_range", "body", "unspecified", "hybrid")
    assert DEFAULT_OB_EXTENT == "hybrid"
    assert ICTConfig().ob_extent == DEFAULT_OB_EXTENT
    with pytest.raises(VariantUndeclared) as excinfo:
        validate_ob_extent("unspecified")
    assert excinfo.value.refusal.code is RefusalCode.NT_VARIANT_UNDECLARED
    with pytest.raises(ValueError):
        validate_ob_extent("invented")
    with pytest.raises(VariantUndeclared):
        _ob(ob_extent="unspecified")

    hybrid = _ob()
    full = _ob(ob_extent="full_range")
    body = _ob(ob_extent="body")
    assert (hybrid.top, hybrid.bottom) == (96.0, 94.5)
    assert (full.top, full.bottom) == (96.5, 94.5)
    assert (body.top, body.bottom) == (96.0, 94.8)
    assert hybrid.mean == full.mean == body.mean, (
        "AR-17: the mean is the body midpoint, independent of the zone extent")


def test_ob_admission_gates_are_declared_and_conjunctive():
    assert OB_ADMISSION_GATES == (
        "break_required", "imbalance_constitutive", "grab")
    assert DEFAULT_OB_GATE == "break_required"
    assert ICTConfig().ob_gate == DEFAULT_OB_GATE
    with pytest.raises(ValueError):
        validate_ob_gate("invented")
    event = [StructureEvent(4, 101.0, "MSS", "bull")]

    # break_required is the event-driven gate: one block.
    assert len(detect_order_blocks(_frame(_OB_ROWS), event, [],
                                   ICTConfig(ob_gate="break_required"))) == 1

    # imbalance_constitutive needs an FVG in the displacement leg [t*, e].
    no_gap = detect_order_blocks(_frame(_OB_ROWS), event, [],
                                 ICTConfig(ob_gate="imbalance_constitutive"))
    assert no_gap == []
    gap = FVG("bear", 96.2, 95.0, 4, 9, 4)
    with_gap = detect_order_blocks(
        _frame(_OB_ROWS), event, [],
        ICTConfig(ob_gate="imbalance_constitutive"), fvgs=[gap])
    assert len(with_gap) == 1

    # grab needs an OB-local sweep known by the event bar.
    assert detect_order_blocks(_frame(_OB_ROWS), event, [],
                               ICTConfig(ob_gate="grab")) == []
    inside = _Sweep("bear", reclaim_bar=4, level=95.0)
    outside = _Sweep("bear", reclaim_bar=4, level=90.0)
    late = _Sweep("bear", reclaim_bar=6, level=95.0)
    assert len(detect_order_blocks(_frame(_OB_ROWS), event, [],
                                   ICTConfig(ob_gate="grab"),
                                   sweeps=[inside])) == 1
    assert detect_order_blocks(_frame(_OB_ROWS), event, [],
                               ICTConfig(ob_gate="grab"),
                               sweeps=[outside]) == []
    assert detect_order_blocks(_frame(_OB_ROWS), event, [],
                               ICTConfig(ob_gate="grab"),
                               sweeps=[late]) == []


# --------------------------------------------------------------------------- #
# 8. AR-13/AR-14: BPR candle floor and close-through invalidation
# --------------------------------------------------------------------------- #
def test_bpr_candle_floor_is_corpus_and_optional():
    assert BPR_CANDLE_FLOOR == 6
    bear = FVG("bear", 108.0, 103.0, 1, 9, 1)
    bull = FVG("bull", 105.0, 100.0, 4, 9, 4)          # span 6 candles
    assert len(detect_bpr([bear, bull], candle_floor=6)) == 1
    early_bull = FVG("bull", 105.0, 100.0, 1, 9, 1)
    early_bear = FVG("bear", 108.0, 103.0, 3, 9, 3)    # span 5 candles
    assert detect_bpr([early_bull, early_bear], candle_floor=6) == []
    assert len(detect_bpr([early_bull, early_bear])) == 1, (
        "None is the labelled 'no temporal constraint' reading, not a default "
        "invented floor")
    with pytest.raises(ValueError):
        detect_bpr([bear, bull], candle_floor=5)


def test_bpr_invalidates_on_a_close_not_a_wick():
    # bear created first, bull later -> the newer gap makes the BPR bullish.
    bear = FVG("bear", 108.0, 103.0, 1, 9, 1)
    bull = FVG("bull", 105.0, 100.0, 4, 9, 4)
    rows = [(100, 101, 99, 100)] * 5 + [
        (104.0, 104.5, 102.5, 103.8),   # wick into the band, close inside
        (103.5, 104.0, 102.0, 102.5),   # body close below the far edge 103
        (102.5, 103.0, 101.5, 102.0),
    ]
    bpr = detect_bpr([bear, bull], _frame(rows))[0]
    assert bpr.direction == "bull"
    assert bpr.touched_at == 5, "a wick through is a test, not a break"
    assert bpr.active is False and bpr.mitigated is True
    assert bpr.right == 6, "AR-14: the far-side close retires the band"

    rows[5] = (104.0, 104.5, 102.5, 104.2)   # closes back inside
    rows[6] = (104.0, 104.5, 103.5, 104.0)
    rows[7] = (103.5, 104.0, 103.0, 103.2)
    intact = detect_bpr([bear, bull], _frame(rows))[0]
    assert intact.active is True and intact.mitigated is False
    assert intact.touched_at == 5, "the wick still marks a tested band"

    # Bear mirror: bull first, bear later -> a bearish BPR whose far edge is
    # the top; only a close above it retires the band.
    bull_first = FVG("bull", 105.0, 100.0, 1, 9, 1)
    bear_later = FVG("bear", 108.0, 103.0, 4, 9, 4)
    bear_rows = [(100, 101, 99, 100)] * 5 + [
        (104.0, 105.5, 103.5, 104.5),   # wick above the top 105, close inside
        (104.5, 106.0, 104.0, 105.5),   # body close above the far edge 105
    ]
    bear_bpr = detect_bpr([bull_first, bear_later], _frame(bear_rows))[0]
    assert bear_bpr.direction == "bear"
    assert bear_bpr.active is False and bear_bpr.right == 6


# --------------------------------------------------------------------------- #
# 9. AS-01..AS-04: the side tag and the declared side_mode
# --------------------------------------------------------------------------- #
def test_side_modes_are_declared_and_produce_opposite_labels():
    assert SIDE_MODES == ("by_type", "by_position", "by_role")
    assert DEFAULT_ARRAY_SIDE_MODE == "by_type"
    # A bearish array below price: by type it is premium, by position discount.
    by_type = array_side("bear", price=120.0, top=110.0, bottom=105.0)
    by_position = array_side("bear", price=120.0, top=110.0, bottom=105.0,
                             mode="by_position")
    assert (by_type.side, by_position.side) == ("premium", "discount")
    assert by_type.mode == "by_type" and by_position.mode == "by_position"
    assert by_type.source and by_position.source
    # AS-02: the matrix is not closed under inversion -- role wins by mode.
    assert array_side("bear", mode="by_role", role="supply").side == "premium"
    assert array_side("bull", mode="by_role", role="demand").side == "discount"
    with pytest.raises(ValueError):
        array_side("bear", mode="by_role")
    with pytest.raises(ValueError):
        array_side("bear", mode="invented")


def test_side_by_position_refuses_a_straddling_array():
    tag = array_side("bear", price=107.5, top=110.0, bottom=105.0,
                     mode="by_position")
    assert tag.side is None and tag.at_price is True
    down = array_side("bear", price=120.0, top=110.0, bottom=105.0,
                      mode="by_position")
    assert down.side == "discount" and down.at_price is False


def test_ict_arrays_carry_the_side_tag_and_retest_fields():
    arrays = build_ict_arrays(_walk(), "TESTUSDT", "15m")
    assert arrays
    for array in arrays:
        assert array.side_mode == DEFAULT_ARRAY_SIDE_MODE
        assert array.side in (None, *ARRAY_SIDES)
        public = array.to_public()
        assert public["side"] == array.side
        assert public["side_mode"] == array.side_mode
        assert "retest_at" in public and "failure_bar" in public
    assert any(a.array_type == "BREAKER" and a.failure_bar is not None
               for a in arrays)
    assert any(a.array_type == "BREAKER" and a.retest_at is not None
               for a in arrays)
    # V22 is undeclared by default, so no IFVG removal is applied silently.
    for array in arrays:
        if array.array_type == "IFVG":
            assert array.removal_mode == ""

    # AS-04's mode is read from the run's declaration, not hard-coded.
    positional = build_ict_arrays(
        _walk(), "TESTUSDT", "15m",
        config=ICTConfig(array_side_mode="by_position"))
    assert {a.side_mode for a in positional} == {"by_position"}
    assert all(a.side in (None, *ARRAY_SIDES) for a in positional)


# --------------------------------------------------------------------------- #
# 10. V09: the six corpus rosters are separate and never max-unioned
# --------------------------------------------------------------------------- #
def test_corpus_rosters_are_six_separate_declarations():
    assert set(ARRAY_ROSTERS) == {
        "corpus_3", "corpus_6", "corpus_7", "corpus_7_ladder", "corpus_8",
        "corpus_9"}
    assert [ARRAY_ROSTERS[name].declared_size for name in ARRAY_ROSTERS] == [
        3, 6, 7, 7, 8, 9]
    assert set(ARRAY_ROSTER_SIZES) == {3, 6, 7, 8, 9}
    nine = ARRAY_ROSTERS["corpus_9"]
    assert len(nine.members) == 8 and nine.count_note, (
        "the 9-member header prints eight names; recorded, not repaired")
    # AS-06: a max-union roster is forbidden.
    with pytest.raises(ValueError):
        ArrayRoster("max", 9, ARRAY_MEMBER_CLASSES, "invented union")
    with pytest.raises(ValueError):
        # A count note may only record the corpus's own 9-vs-8 defect.
        ArrayRoster("bad_note", 9, ("fvg", "order_block", "breaker"),
                    "invented", count_note="pretend defect")
    with pytest.raises(ValueError):
        ArrayRoster("dup", 3, ("fvg", "fvg", "order_block"), "invented")
    with pytest.raises(ValueError):
        ArrayRoster("unknown", 3, ("fvg", "nonsense", "order_block"),
                    "invented")
    assert ArrayRoster("custom", 3, ("fvg", "order_block", "breaker"),
                       "declared for a test")


def test_roster_admission_is_exact_and_undeclared_refuses():
    corpus8 = ARRAY_ROSTERS["corpus_8"]
    assert corpus8.admits("IFVG") and corpus8.admits("BREAKER")
    assert not corpus8.admits("BPR"), "no corpus roster lists the BPR object"
    assert not corpus8.admits("CISD")
    assert not ARRAY_ROSTERS["corpus_6"].admits("IFVG")
    with pytest.raises(VariantUndeclared) as excinfo:
        resolve_array_roster(None)
    assert excinfo.value.refusal.code is RefusalCode.NT_VARIANT_UNDECLARED
    with pytest.raises(VariantUndeclared):
        scan_arrays([FVG("bull", 105.0, 100.0, 1, 9, 1)], roster=None)
    with pytest.raises(ValueError):
        resolve_array_roster("corpus_99")


# --------------------------------------------------------------------------- #
# 11. V26 / §7.3: declared priority and scan order
# --------------------------------------------------------------------------- #
def test_declared_axes_match_the_variant_register():
    """The implementation options are the register's axes, not private forks."""
    assert IFVG_REMOVAL_MODES == read_variant_spec("V22").options

    v19 = read_variant_spec("V19").options
    extents = {option.split(" · ")[0].replace(" ", "_") for option in v19}
    gates = {option.split(" · ")[1].replace(" ", "_") for option in v19}
    assert extents | {"hybrid"} == set(OB_EXTENTS)
    assert gates == set(OB_ADMISSION_GATES)

    v20 = read_variant_spec("V20").options
    assert len(v20) == len(BREAKER_FAILURE_TESTS) * len(BREAKER_GEOMETRIES)

    v09 = read_variant_spec("V09").options
    assert len(v09) == len(ARRAY_ROSTER_SIZES) * len(ARRAY_ORDER_MODES)

    assert read_variant_spec("V26").options == ("declared explicit ordering",)
    assert ARRAY_TOTAL_ORDERS
    assert all(name in ARRAY_ORDER_SOURCES for name in ARRAY_TOTAL_ORDERS)


def _ordered_objects() -> list:
    fvg = FVG("bull", 105.0, 100.0, 1, 9, 1)
    ob = OrderBlock("bear", 108.0, 103.0, 2, 9, 2)
    breaker = PDAArray("breaker", "bull", 110.0, 106.0, 3, 9, known_at=3)
    mitigation = PDAArray("mitigation", "bull", 101.0, 99.0, 4, 9, known_at=4)
    return [fvg, ob, breaker, mitigation]


def test_total_order_declares_breaker_precedence():
    assert ARRAY_ORDER_MODES == ("total_order", "proximity", "none")
    assert DEFAULT_ARRAY_ORDER_MODE == "total_order"
    assert DEFAULT_ARRAY_TOTAL_ORDER == "breaker_first"
    assert ARRAY_TOTAL_ORDERS["breaker_first"][0] == "breaker"
    ladder, refusal = scan_arrays(_ordered_objects(), roster="corpus_6",
                                  total_order="ladder_reversed", at=9)
    priority, refusal = scan_arrays(_ordered_objects(), roster="corpus_6",
                                    total_order="breaker_first", at=9)
    assert refusal is None
    assert [type(a).__name__ for a in ladder] == [
        "PDAArray", "PDAArray", "FVG", "OrderBlock"]
    assert ladder[0].kind == "mitigation" and ladder[1].kind == "breaker"
    assert priority[0].kind == "breaker" and priority[1].kind == "mitigation"


def test_proximity_order_and_none_rejection():
    objects = _ordered_objects()
    # Distances from 104.0: mitigation [99,101]=3, fvg [100,105]=0,
    # ob [103,108]=0, breaker [106,110]=2.  Tie-break is the declared stable
    # id, so only the nearest/farthest ends are guaranteed.
    chosen, refusal = scan_arrays(objects, roster="corpus_6",
                                  order_mode="proximity", price=104.0, at=9)
    assert refusal is None
    assert chosen[0].top == 105.0 or chosen[0].top == 108.0
    assert chosen[-1].top == 101.0, "the mitigation block is farthest away"
    with pytest.raises(ValueError):
        resolve_array_order("none")
    with pytest.raises(ValueError):
        resolve_array_order("invented")
    with pytest.raises(ValueError):
        resolve_array_order(total_order="invented")


def test_scan_hard_filters_and_freshness_demotion():
    intact = FVG("bull", 105.0, 100.0, 1, 9, 1)
    swept = FVG("bull", 106.0, 101.0, 2, 9, 2)
    swept.touched_at = 3
    broken = FVG("bull", 107.0, 102.0, 3, 9, 3)
    broken.active = False
    broken.mitigated = True
    broken.right = 8
    assert swept.state == "swept" and broken.state == "broken"

    chosen, refusal = scan_arrays([swept, broken, intact], roster="corpus_6",
                                  half=None, at=9)
    assert refusal is None
    assert chosen[0] is intact, "swept arrays are demoted, never deleted"
    assert swept in chosen and broken not in chosen

    # CB-07: nothing is consumable before known_at.
    late = FVG("bull", 105.0, 100.0, 8, 9, 8)
    chosen, refusal = scan_arrays([late], roster="corpus_6", half=None, at=5)
    assert chosen == [] and refusal is not None
    assert refusal.code is RefusalCode.NT_MATRIX_EMPTY


def test_scan_half_filter_follows_pd01():
    bullish = FVG("bull", 105.0, 100.0, 1, 9, 1)     # discount by type
    bearish = FVG("bear", 108.0, 103.0, 2, 9, 2)     # premium by type
    long_side, _ = scan_arrays([bearish], roster="corpus_6", direction="long",
                               half=None, at=9)
    assert long_side == [], "PD-01: a long is taken from discount"
    short_side, _ = scan_arrays([bearish], roster="corpus_6",
                                direction="short", half=None, at=9)
    assert short_side == [bearish]
    chosen, _ = scan_arrays([bullish, bearish], roster="corpus_6",
                            half="discount", at=9)
    assert chosen == [bullish]


# --------------------------------------------------------------------------- #
# 12. The realtime inventory exposes the same declarations
# --------------------------------------------------------------------------- #
def test_scan_ict_arrays_delegates_the_declared_order():
    arrays = [
        a for a in build_ict_arrays(_walk(), "TESTUSDT", "15m")
        if a.array_type in ("FVG", "OB", "BREAKER")
    ]
    assert arrays
    chosen, refusal = scan_ict_arrays(arrays, roster="corpus_6",
                                      side_mode=DEFAULT_ARRAY_SIDE_MODE,
                                      at=len(_walk()) - 1)
    if refusal is None:
        assert chosen, "a non-empty inventory yields a selection"
        assert all(a.array_type in ("FVG", "OB", "BREAKER") for a in chosen)
    else:
        assert refusal.code is RefusalCode.NT_MATRIX_EMPTY
