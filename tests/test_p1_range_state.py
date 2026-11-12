"""P1-12 - range state (DR-02 / DR-06) and the IPDA window as its own type.

``CODE_MAPPING.md`` P1-12 asks for three things that were absent from the
checkout:

* the **DR-02 validity gate** -- each structural anchor must have taken
  prior-side liquidity (the high an old high, the low an old low); without it
  the pair is, in the corpus's own words, "just two random pivots";
* ``RangeState {valid, redraw_reason}`` plus the DR-06 redraw triggers, with a
  redraw being a **replacement** of both anchors, never a partial repair; and
* the IPDA window as a **separate type** over a fixed count, which is legal
  only for that object (IP-01..IP-07, DR-00/SN-R07).

Two corpus forks stay declared, never defaulted silently: T11 permits either a
refusal (``NT_RANGE_INVALID``) or a downgrade with a declared flag, and DR-06
permits either redraw-on-trigger or the session-open-plus-BOS cadence.  The
repo choices (``downgrade`` / ``on_trigger``) are named constants, exercised
both ways below.

These tests are new on the pre-P1-12 checkout: ``RangeState``, the gate and the
redraw API do not exist there.
"""
from __future__ import annotations

import inspect

import numpy as np
import pandas as pd
import pytest

from ict_trader.concepts import (
    IPDA_LEVEL_KINDS,
    IPDA_WINDOWS,
    IPDALevel,
    IPDAWindow,
    IPDAWindowSet,
    ICTConfig,
    Swing,
    analyze,
    detect_dealing_range_state,
    detect_ipda_window,
    detect_ipda_windows,
)
from ict_trader.concepts import DealingRange as ConceptDealingRange
from ict_trader.context import (
    DEFAULT_RANGE_ANCHOR_CLASS,
    DEFAULT_RANGE_MEASUREMENT_BASIS,
    DEFAULT_RANGE_REDRAW_CADENCE,
    DEFAULT_RANGE_VALIDITY_POLICY,
    NT_RANGE_INVALID,
    RANGE_ANCHOR_CLASSES,
    RANGE_MEASUREMENT_BASES,
    RANGE_REDRAW_CADENCES,
    RANGE_REDRAW_REASONS,
    RANGE_REDRAW_TRIGGERS,
    RANGE_VALIDITY_POLICIES,
    DealingRange as ContextDealingRange,
    RangeDiagnostic,
    RangeState,
    SwingIndex,
    anchor_took_prior_liquidity,
    build_range_state,
    dealing_range_state,
    range_redraw_reason,
    redraw_range_state,
    validate_range_measurement_basis,
)
from ict_trader.realtime.analysis.arrays import build_ict_arrays
from ict_trader.variants import RefusalCode


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
def _flat_frame(n: int = 120) -> pd.DataFrame:
    """A frame only used for its length: the anchors are supplied explicitly."""
    return pd.DataFrame({
        "date": pd.date_range("2026-01-05", periods=n, freq="15min",
                              tz="America/New_York"),
        "open": 100.0, "high": 100.5, "low": 99.5, "close": 100.0,
        "volume": 1000.0,
    })


def _walk(n: int = 120, seed: int = 17) -> pd.DataFrame:
    """Deterministic random walk with confirmed swings (mirrors P0 fixtures)."""
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


def _tent(n: int = 45) -> pd.DataFrame:
    """One confirmed swing high and no swing low: no structural pair exists."""
    dates = pd.date_range("2026-01-01", periods=n, freq="15min", tz="UTC")
    half = n // 2
    price = np.r_[np.linspace(100.0, 120.0, half),
                  np.linspace(120.0, 100.0, n - half)]
    return pd.DataFrame({
        "date": dates, "open": price, "high": price + 0.4,
        "low": price - 0.4, "close": price, "volume": 1000.0,
    })


def _swing(index: int, price: float, kind: str,
           confirmed_at: int | None = None) -> Swing:
    return Swing(index, float(price), kind,
                 index + 1 if confirmed_at is None else confirmed_at)


#: High 110 took the old high 105; low 85 took the old low 90.  Both anchors
#: are liquidity-taking pivots, so the pair passes DR-02.
VALID_SWINGS = [
    _swing(2, 105.0, "H"), _swing(5, 90.0, "L"),
    _swing(10, 110.0, "H"), _swing(15, 85.0, "L"),
]

#: The newest high (102) is below every old high and the newest low (95) is
#: above every old low: two random pivots, no validity.
INVALID_SWINGS = [
    _swing(2, 105.0, "H"), _swing(5, 90.0, "L"),
    _swing(10, 110.0, "H"), _swing(15, 85.0, "L"),
    _swing(20, 102.0, "H"), _swing(25, 95.0, "L"),
]

#: The high took prior liquidity, the low did not: one failed anchor is enough.
HALF_VALID_SWINGS = [
    _swing(2, 105.0, "H"), _swing(5, 90.0, "L"),
    _swing(10, 120.0, "H"), _swing(15, 95.0, "L"),
]


# --------------------------------------------------------------------------- #
# 1. DR-02 validity gate
# --------------------------------------------------------------------------- #
def test_dr02_anchor_must_penetrate_an_old_extreme():
    # A high takes prior-side liquidity by trading above an old high.
    assert anchor_took_prior_liquidity(110.0, "H", [105.0]) is True
    # Equality is a touch, not a take: no numeric tolerance is invented.
    assert anchor_took_prior_liquidity(105.0, "H", [105.0]) is False
    # A low takes prior-side liquidity by trading below an old low.
    assert anchor_took_prior_liquidity(85.0, "L", [90.0]) is True
    assert anchor_took_prior_liquidity(90.0, "L", [90.0]) is False
    # No prior reference is an unproven anchor, so the gate fails closed.
    assert anchor_took_prior_liquidity(110.0, "H", []) is False
    assert anchor_took_prior_liquidity(85.0, "L", []) is False
    with pytest.raises(ValueError):
        anchor_took_prior_liquidity(110.0, "sideways", [105.0])


def test_valid_pair_produces_a_valid_range_state_with_dr03_dr04_arithmetic():
    state = build_range_state(120.0, 100.0, prior_highs=[119.0],
                              prior_lows=[101.0])
    assert state.valid is True
    assert state.redraw_reason == "" and state.needs_redraw is False
    assert state.high_took_prior_liquidity is True
    assert state.low_took_prior_liquidity is True
    assert state.code == ""
    # DR-03 / DR-04.
    assert state.span == 20.0
    assert state.eq == 110.0
    assert state.q25 == 105.0
    assert state.q75 == 115.0
    public = state.to_public()
    assert public["valid"] is True
    assert public["redraw_reason"] == ""
    assert public["eq"] == 110.0


def test_an_anchor_that_took_no_prior_liquidity_invalidates_the_pair():
    state = build_range_state(102.0, 95.0, prior_highs=[105.0, 110.0],
                              prior_lows=[85.0, 90.0])
    assert state.valid is False
    assert state.redraw_reason == "anchor_no_prior_liquidity"
    assert state.code == NT_RANGE_INVALID
    assert state.high_took_prior_liquidity is False
    assert state.low_took_prior_liquidity is False

    one_side = build_range_state(120.0, 95.0, prior_highs=[105.0],
                                 prior_lows=[90.0])
    assert one_side.valid is False
    assert one_side.high_took_prior_liquidity is True
    assert one_side.low_took_prior_liquidity is False

    # A pair with no prior references at all cannot prove validity.
    unknown = build_range_state(120.0, 100.0)
    assert unknown.valid is False
    assert unknown.redraw_reason == "anchor_no_prior_liquidity"


def test_degenerate_anchors_are_an_invalid_range_state():
    state = build_range_state(100.0, 100.0, prior_highs=[99.0],
                              prior_lows=[101.0])
    assert state.valid is False
    assert state.redraw_reason == "degenerate_anchors"
    assert state.code == NT_RANGE_INVALID


# --------------------------------------------------------------------------- #
# 2. The gate is applied by both range constructors
# --------------------------------------------------------------------------- #
def test_context_range_constructor_refuses_or_downgrades_the_invalid_pair():
    refused, diagnostic = dealing_range_state(INVALID_SWINGS, 30, validity="refuse")
    assert refused is None
    assert diagnostic is not None
    assert diagnostic.code == NT_RANGE_INVALID
    assert diagnostic.redraw_reason == "anchor_no_prior_liquidity"
    assert "prior" in diagnostic.reason.lower()

    downgraded, no_diagnostic = dealing_range_state(
        INVALID_SWINGS, 30, validity="downgrade")
    assert no_diagnostic is None
    assert isinstance(downgraded, ContextDealingRange)
    assert downgraded.range_state is not None
    assert downgraded.range_state.valid is False
    assert downgraded.range_state.redraw_reason == "anchor_no_prior_liquidity"

    # The declared repo default is the downgrade, and it is not silent: the
    # state says the range failed DR-02.
    default, default_diagnostic = dealing_range_state(INVALID_SWINGS, 30)
    assert DEFAULT_RANGE_VALIDITY_POLICY == "downgrade"
    assert default_diagnostic is None and default is not None
    assert default.range_state.valid is False
    assert set(RANGE_VALIDITY_POLICIES) == {"downgrade", "refuse"}

    with pytest.raises(ValueError):
        dealing_range_state(INVALID_SWINGS, 30, validity="prefer_not")


def test_valid_pair_passes_the_gate_in_both_constructors():
    value, diagnostic = dealing_range_state(VALID_SWINGS, 30, validity="refuse")
    assert diagnostic is None and value is not None
    assert value.range_state is not None
    assert value.range_state.valid is True
    assert value.range_state.redraw_reason == ""
    assert value.range_state.known_at == 16

    concept_value, concept_diagnostic = detect_dealing_range_state(
        _flat_frame(), VALID_SWINGS, validity="refuse")
    assert concept_diagnostic is None and concept_value is not None
    assert concept_value.range_state is not None
    assert concept_value.range_state.valid is True

    refused, concept_diag = detect_dealing_range_state(
        _flat_frame(), INVALID_SWINGS, validity="refuse")
    assert refused is None and concept_diag is not None
    assert concept_diag.code == NT_RANGE_INVALID
    assert concept_diag.redraw_reason == "anchor_no_prior_liquidity"
    with pytest.raises(ValueError):
        detect_dealing_range_state(_flat_frame(), VALID_SWINGS,
                                   validity="undeclared")


def test_swing_index_reports_the_same_gate_verdict():
    reference, reference_diag = dealing_range_state(
        INVALID_SWINGS, 30, validity="refuse")
    indexed, indexed_diag = SwingIndex(INVALID_SWINGS).dealing_range_state(
        30, validity="refuse")
    assert reference is None and indexed is None
    assert indexed_diag == reference_diag
    assert isinstance(indexed_diag, RangeDiagnostic)
    assert indexed_diag.redraw_reason == "anchor_no_prior_liquidity"

    value, diagnostic = SwingIndex(VALID_SWINGS).dealing_range_state(
        30, validity="refuse")
    assert diagnostic is None and value is not None
    assert value.range_state.valid is True


def test_stale_and_degenerate_diagnostics_name_their_redraw_reason():
    stale = [_swing(2, 100.0, "L", 3), _swing(5, 200.0, "H", 6)]
    value, diagnostic = dealing_range_state(stale, 200)
    assert value is None and diagnostic is not None
    assert diagnostic.code == NT_RANGE_INVALID
    assert diagnostic.redraw_reason == "stale_anchors"
    assert diagnostic.to_public()["redraw_reason"] == "stale_anchors"

    degenerate = [_swing(2, 100.0, "H"), _swing(5, 100.0, "L")]
    value, diagnostic = dealing_range_state(degenerate, 30)
    assert value is None and diagnostic is not None
    assert diagnostic.redraw_reason == "degenerate_anchors"


# --------------------------------------------------------------------------- #
# 3. DR-06 redraw triggers
# --------------------------------------------------------------------------- #
def test_dr06_trigger_set_is_declared_without_a_silent_cadence():
    assert RANGE_REDRAW_TRIGGERS[:3] == ("new_swing_extreme", "bos", "choch")
    assert "session_open" in RANGE_REDRAW_TRIGGERS

    for trigger in ("new_swing_extreme", "bos", "choch"):
        assert range_redraw_reason(trigger) == trigger
    # The session-open cadence is a *variant*: it is not active by default.
    assert DEFAULT_RANGE_REDRAW_CADENCE == "on_trigger"
    assert set(RANGE_REDRAW_CADENCES) == {"on_trigger", "session_open_and_bos"}
    assert range_redraw_reason("session_open") == ""
    assert range_redraw_reason("session_open",
                               cadence="session_open_and_bos") == "session_open"
    assert range_redraw_reason("bos",
                               cadence="session_open_and_bos") == "bos"
    assert range_redraw_reason("new_swing_extreme",
                               cadence="session_open_and_bos") == ""
    with pytest.raises(ValueError):
        range_redraw_reason("gap")
    with pytest.raises(ValueError):
        range_redraw_reason("bos", cadence="hourly")


def test_a_trigger_marks_the_current_state_for_redraw_without_mutating_it():
    state = build_range_state(120.0, 100.0, prior_highs=[119.0],
                              prior_lows=[101.0])
    marked = state.after_trigger("bos")
    assert marked is not state
    assert marked.valid is True          # DR-02 validity is independent
    assert marked.redraw_reason == "bos"  # DR-06 currentness
    assert marked.needs_redraw is True
    assert state.needs_redraw is False and state.redraw_reason == ""
    # An inactive trigger is a declaration error, not a silent no-op.
    with pytest.raises(ValueError):
        state.after_trigger("session_open")


def test_redraw_replaces_both_anchors_and_keeps_the_previous_state():
    old = build_range_state(120.0, 100.0, prior_highs=[119.0],
                            prior_lows=[101.0])
    new = redraw_range_state(
        old, "bos", high=130.0, low=95.0,
        prior_highs=[120.0, 125.0], prior_lows=[100.0, 96.0])
    assert (new.high, new.low) == (130.0, 95.0)
    assert new.valid is True and new.redraw_reason == ""
    assert new.redraw_trigger == "bos"
    assert new.previous is old
    # Replacement, not partial repair: the old state is untouched.
    assert (old.high, old.low) == (120.0, 100.0)
    assert old.redraw_trigger == ""

    # Both anchors are required, so a one-sided "repair" cannot be expressed.
    with pytest.raises(TypeError):
        redraw_range_state(old, "bos", high=130.0,
                           prior_highs=[120.0], prior_lows=[100.0])
    # A redraw must be caused by an active trigger.
    with pytest.raises(ValueError):
        redraw_range_state(old, "session_open", high=130.0, low=95.0,
                           prior_highs=[120.0], prior_lows=[100.0])


def test_redraw_of_an_invalid_pair_is_a_fresh_validation():
    old = build_range_state(102.0, 95.0, prior_highs=[105.0],
                            prior_lows=[90.0])
    assert old.valid is False
    new = redraw_range_state(old, "choch", high=120.0, low=85.0,
                             prior_highs=[110.0], prior_lows=[95.0])
    assert new.valid is True and new.previous is old
    assert old.valid is False  # the failed pair is not patched in place

    still_bad = redraw_range_state(old, "new_swing_extreme", high=103.0,
                                   low=96.0, prior_highs=[105.0],
                                   prior_lows=[90.0])
    assert still_bad.valid is False
    assert still_bad.redraw_reason == "anchor_no_prior_liquidity"
    assert still_bad.code == NT_RANGE_INVALID


def test_range_state_refuses_undeclared_classes_and_reasons():
    with pytest.raises(ValueError):
        RangeState(120.0, 100.0, True, anchor_class="vibes")
    with pytest.raises(ValueError):
        RangeState(120.0, 100.0, True, redraw_reason="unknown_reason")
    assert set(RANGE_ANCHOR_CLASSES) >= {"swing_pair", "swing_pair_took_liquidity"}
    assert DEFAULT_RANGE_ANCHOR_CLASS in RANGE_ANCHOR_CLASSES
    assert "anchor_no_prior_liquidity" in RANGE_REDRAW_REASONS


def test_measurement_basis_is_a_declared_dr08_fork():
    assert DEFAULT_RANGE_MEASUREMENT_BASIS == "wick"
    assert set(RANGE_MEASUREMENT_BASES) == {"wick", "body", "both"}
    assert validate_range_measurement_basis("wick") == "wick"
    with pytest.raises(ValueError):
        validate_range_measurement_basis("midpoint")


# --------------------------------------------------------------------------- #
# 4. No fixed-count range
# --------------------------------------------------------------------------- #
def test_no_fixed_count_can_anchor_a_structural_dealing_range():
    parameters = inspect.signature(detect_dealing_range_state).parameters
    assert "fallback_bars" not in parameters
    assert "window" not in parameters
    assert "lookback_bars" not in parameters

    value, diagnostic = detect_dealing_range_state(_tent())
    assert value is None
    assert diagnostic is not None
    assert "fallback" in diagnostic.reason.lower()
    arrays = build_ict_arrays(_tent(), "TESTUSDT", "15m")
    assert not [a for a in arrays if a.array_type == "DEALING_RANGE"]


# --------------------------------------------------------------------------- #
# 5. IPDA window as a separate type (IP-01..IP-07)
# --------------------------------------------------------------------------- #
def test_ipda_window_is_its_own_type_with_its_own_arithmetic():
    window = IPDAWindow(window=40, high=200.0, low=100.0, known_at=59,
                        right=59)
    assert window.ce == 150.0                      # EQ_N (IP-01)
    assert window.position_pct(175.0) == 75.0      # IP-02
    assert window.position_pct(125.0) == 25.0
    assert window.half(175.0) == "premium"
    assert window.half(125.0) == "discount"
    # IP-02: price == EQ is undefined by every source -- no half is invented.
    assert window.half(150.0) is None
    assert not isinstance(window, ConceptDealingRange)
    assert not isinstance(window, ContextDealingRange)
    assert not isinstance(window, RangeState)
    with pytest.raises(ValueError):
        IPDAWindow(window=30, high=200.0, low=100.0, known_at=59, right=59)
    assert IPDA_WINDOWS == (20, 40, 60)


def test_ipda_window_reads_only_closed_candles():
    daily = pd.DataFrame({
        "high": np.arange(100.0, 121.0),   # bars 0..20, bar 20 is the live one
        "low": np.arange(90.0, 111.0),
    })
    last_closed = detect_ipda_window(daily, 20, last_closed=19)
    assert last_closed is not None
    assert last_closed.high == 119.0 and last_closed.low == 90.0
    live = detect_ipda_window(daily, 20, last_closed=20)
    assert live is not None and live.high == 120.0
    assert live.known_at == 20
    # A window that does not fit in the supplied frame is absent, not clipped.
    assert detect_ipda_window(daily.iloc[:5], 20) is None
    with pytest.raises(ValueError):
        detect_ipda_window(daily, 30)


def test_ipda_set_exposes_six_extremes_three_midpoints_and_escalation():
    frame = pd.DataFrame({
        "high": np.arange(100.0, 200.0),
        "low": np.arange(90.0, 190.0),
    })
    windows = detect_ipda_windows(frame, last_closed=99)
    assert isinstance(windows, IPDAWindowSet)
    assert windows.numbers == (20, 40, 60)
    assert len(windows.extremes) == 6   # two per window (IP-05)
    assert len(windows.midpoints) == 3  # one per window
    levels = windows.levels()
    assert len(levels) == 9             # up to nine levels (IP-05)
    assert all(isinstance(level, IPDALevel) for level in levels)
    assert {level.kind for level in levels} == set(IPDA_LEVEL_KINDS)
    assert len({(level.kind, level.window, level.price)
                for level in levels}) == 9
    # Overlap between windows is expected, not an error (IP-05): every window
    # shares the same 100..199 frame maximum.
    assert [level.price for level in windows.extremes[:3]][0] == 199.0
    assert len({level.price for level in levels
                if level.kind == "extreme" and level.label.startswith("H")}) == 1

    assert windows.by_n(40).high == 199.0
    assert windows.by_n(40).low == 150.0
    assert windows.position_pct(174.5, 40) == 50.0
    assert windows.half(174.5, 40) is None

    # IP-03: 20 -> 40 -> 60 only when no usable objective remains nearer.
    assert windows.escalate(20, usable_objective_remains=True) == 20
    assert windows.escalate(20, usable_objective_remains=False) == 40
    assert windows.escalate(40, usable_objective_remains=False) == 60
    assert windows.escalate(60, usable_objective_remains=False) == 60
    with pytest.raises(ValueError):
        windows.by_n(30)
    with pytest.raises(ValueError):
        windows.escalate(30, usable_objective_remains=False)


def test_ipda_windows_need_every_declared_window():
    short = pd.DataFrame({
        "high": np.arange(100.0, 130.0),
        "low": np.arange(90.0, 120.0),
    })
    assert detect_ipda_windows(short, last_closed=29) is None
    with pytest.raises(ValueError):
        detect_ipda_windows(short, windows=(30,))
    two = detect_ipda_windows(short, windows=(20,), last_closed=29)
    assert isinstance(two, IPDAWindowSet) and two.numbers == (20,)


# --------------------------------------------------------------------------- #
# 6. Integration: analyze() and the array lifecycle carry the state
# --------------------------------------------------------------------------- #
def test_analyze_result_carries_the_range_state_and_never_an_ipda_window():
    result = analyze(_walk(), ICTConfig())
    assert result.dealing_range is not None
    assert isinstance(result.dealing_range, ConceptDealingRange)
    assert result.range_state is not None
    assert isinstance(result.range_state, RangeState)
    assert result.dealing_range.range_state is result.range_state
    assert result.range_state.valid in (True, False)
    assert not isinstance(result.dealing_range, IPDAWindow)


def test_dealing_range_array_carries_the_range_state():
    arrays = build_ict_arrays(_walk(), "TESTUSDT", "15m")
    range_array = next(a for a in arrays if a.array_type == "DEALING_RANGE")
    assert isinstance(range_array.range_state, RangeState)
    assert range_array.range_state.high == pytest.approx(range_array.price_upper)
    assert range_array.range_state.low == pytest.approx(range_array.price_lower)
    public = range_array.to_public()["range_state"]
    assert public["valid"] is range_array.range_state.valid
    assert public["redraw_reason"] == range_array.range_state.redraw_reason
    assert public["code"] == range_array.range_state.code
    assert not [a for a in arrays if "IPDA" in a.array_type.upper()]


def test_nt_range_invalid_is_the_declared_refusal_code():
    assert NT_RANGE_INVALID == "NT_RANGE_INVALID"
    assert RefusalCode.NT_RANGE_INVALID.value == NT_RANGE_INVALID
    assert "NT_RANGE_INVALID" in {code.value for code in RefusalCode}
