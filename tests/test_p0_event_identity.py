"""P0-20 - event identity and declared constants.

The audit (``CODE_MAPPING.md`` P0-20) found eight undeclared/duplicated
decisions in the structure, sweep, engine, location and stop layers:

* MSS/BOS were de-duplicated by **price**, so a re-break of the same price by a
  newer swing was swallowed (C12/ST-04);
* the break was tested only against the latest confirmed swing, while ST-09
  attests four level-selection mains and chooses none of them;
* the pivot width ``5/1`` and its non-strict ``>=`` ties were re-typed at every
  call site, and ``build_sweep_events(left, right)`` silently ignored both
  arguments;
* the engine carried a private RR floor ``1.0`` that is in no table (V28);
* at-EQ silently returned ``"premium"`` although PD-02 attests four readings
  and PD-05 forbids a trade there without an array;
* a breaker was any mitigated OB, with no sweep precondition and no declared
  tier (PP:C9/R4/R6);
* ``min_stop_atr`` named both an admission filter and a stop-widening floor.

These tests assert the declarations exist, that the code reads them, and that
the behaviour changes only where the declaration says it does.  No ``repo``
value is promoted to corpus doctrine (SS14.1-6).
"""
from __future__ import annotations

import inspect

import numpy as np
import pandas as pd
import pytest

import ict_trader.concepts as concepts_module
from ict_trader.cisd import CISD
from ict_trader.concepts import (DEFAULT_STRUCTURE_LEVEL_SELECTION,
                                 PIVOT_LEFT, PIVOT_RIGHT, PIVOT_TIE_RULE,
                                 PIVOT_WIDTH, PIVOT_WIDTH_SOURCE, ICTConfig,
                                 ICTResult, STRUCTURE_LEVEL_SELECTIONS,
                                 STRUCTURE_LEVEL_SELECTION_SOURCES, Swing,
                                 build_zigzag, detect_structure, find_pivots)
from ict_trader.pd_arrays import (BREAKER_TIER_SOURCES, BREAKER_TIERS,
                                  DEFAULT_BREAKER_TIER, detect_breakers)
from ict_trader.realtime.analysis import AnalysisContext
from ict_trader.realtime.analysis.dealing_range import (AT_EQ_MODES,
                                                        AT_EQ_ZONE,
                                                        DEFAULT_AT_EQ_MODE,
                                                        DealingRange)
from ict_trader.realtime.analysis.engines.ict import (DEFAULT_ENGINE_RR_MODE,
                                                      ENGINE_RR_FLOORS,
                                                      ENGINE_RR_MODES,
                                                      ENGINE_RR_MODE_SOURCES,
                                                      ICTEngine, engine_rr_floor)
from ict_trader.realtime.analysis.risk import STOP_POLICY, structural_stop
from ict_trader.setup import SetupConfig


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _closes(values: list[float]) -> pd.DataFrame:
    """A close-only frame; ``detect_structure`` reads nothing else."""
    n = len(values)
    return pd.DataFrame({
        "date": pd.date_range("2026-01-01", periods=n, freq="15min"),
        "open": values, "high": [v + 0.5 for v in values],
        "low": [v - 0.5 for v in values], "close": values, "volume": 1.0,
    })


def _ohlc(n: int = 80, *, step: float = 0.05) -> pd.DataFrame:
    close = np.linspace(100.0, 100.0 + step * (n - 1), n)
    return pd.DataFrame({
        "date": pd.date_range("2026-01-01", periods=n, freq="15min"),
        "open": close - 0.1, "high": close + 1.0,
        "low": close - 1.0, "close": close, "volume": 100.0,
    })


def _indicator_off(**overrides) -> ICTConfig:
    """A config whose slow detectors are off; structure/breaker wiring is live."""
    values = dict(show_cisd=False, show_htf_cisd=False, show_sessions=False,
                  show_smt=False)
    values.update(overrides)
    return ICTConfig(**values)


# --------------------------------------------------------------------------- #
# 1. MSS/BOS identity is the swing, not the price
# --------------------------------------------------------------------------- #
def test_equal_highs_rebroken_by_a_newer_swing_are_two_events():
    # Two equal highs confirm at different times; each is broken in its own
    # window.  Price-only de-dupe swallowed the second event; identity does not.
    closes = [100.0] * 12
    closes[4] = 106.0   # breaks the H at index 2 (105.0)
    closes[8] = 106.0   # breaks the newer, equal H at index 6 (105.0)
    swings = [Swing(2, 105.0, "H", 3), Swing(6, 105.0, "H", 7)]

    events, _ = detect_structure(_closes(closes), swings)

    assert [(e.index, e.price) for e in events] == [(4, 105.0), (8, 105.0)]
    assert [e.swing_index for e in events] == [2, 6]


def test_breaking_the_same_swing_twice_is_one_event():
    closes = [100.0] * 8
    closes[4] = 106.0
    closes[5] = 107.0
    swings = [Swing(2, 105.0, "H", 3)]
    events, _ = detect_structure(_closes(closes), swings)

    assert len(events) == 1
    assert (events[0].swing_index, events[0].price) == (2, 105.0)
    assert events[0].level_selection == DEFAULT_STRUCTURE_LEVEL_SELECTION


# --------------------------------------------------------------------------- #
# 2. the break reads a declared level-selection mode (ST-09)
# --------------------------------------------------------------------------- #
def test_level_selection_is_declared_and_defaults_to_the_repo_reading():
    assert DEFAULT_STRUCTURE_LEVEL_SELECTION == "most_recent_confirmed"
    assert STRUCTURE_LEVEL_SELECTIONS == ("most_recent_confirmed",
                                          "all_confirmed")
    assert STRUCTURE_LEVEL_SELECTION_SOURCES[
        "most_recent_confirmed"]["label"] == "repo"
    assert STRUCTURE_LEVEL_SELECTION_SOURCES["all_confirmed"]["label"] == "corpus"
    assert ICTConfig().structure_level_selection == (
        DEFAULT_STRUCTURE_LEVEL_SELECTION)

    with pytest.raises(ValueError):
        detect_structure(_closes([100.0] * 5), [],
                         level_selection="significant")


def test_all_confirmed_selection_tests_every_confirmed_swing():
    # The latest confirmed high (110) is not broken at 106; an older confirmed
    # high (105) is.  "Most recent" sees nothing, ST-04 sees the break.
    closes = [100.0] * 10
    closes[6] = 106.0
    swings = [Swing(1, 105.0, "H", 2), Swing(4, 110.0, "H", 5)]

    events, _ = detect_structure(_closes(closes), swings)
    assert events == []

    events, _ = detect_structure(_closes(closes), swings,
                                 level_selection="all_confirmed")
    assert len(events) == 1
    assert (events[0].swing_index, events[0].price) == (1, 105.0)
    assert events[0].level_selection == "all_confirmed"


def test_analyze_forwards_the_declared_level_selection(monkeypatch):
    seen: list[str] = []
    real = concepts_module.detect_structure

    def spy(df, swings, *, level_selection=DEFAULT_STRUCTURE_LEVEL_SELECTION):
        seen.append(level_selection)
        return real(df, swings, level_selection=level_selection)

    monkeypatch.setattr(concepts_module, "detect_structure", spy)
    concepts_module.analyze(
        _ohlc(), _indicator_off(structure_level_selection="all_confirmed"))

    assert seen == ["all_confirmed"]


# --------------------------------------------------------------------------- #
# 3. pivot width/ties and build_sweep_events arguments are declared/honoured
# --------------------------------------------------------------------------- #
def test_pivot_width_and_tie_rule_are_declared():
    assert PIVOT_WIDTH == (5, 1) == (PIVOT_LEFT, PIVOT_RIGHT)
    assert PIVOT_WIDTH_SOURCE == "repo"
    assert PIVOT_TIE_RULE == "non_strict"
    assert ICTConfig().swing_len == PIVOT_LEFT
    # The pivot constructor states the declared width by default.
    params = inspect.signature(find_pivots).parameters
    assert params["left"].default == PIVOT_LEFT
    assert params["right"].default == PIVOT_RIGHT


def test_non_strict_ties_make_equal_highs_pivots_and_keep_the_later_extreme():
    df = pd.DataFrame({
        "open": [100.0] * 4,
        "high": [100.0, 105.0, 105.0, 100.0],
        "low": [98.0, 99.0, 99.0, 98.0],
        "close": [100.0] * 4,
    })
    highs = [s for s in find_pivots(df, 1, 1) if s.kind == "H"]
    # Equal highs are two pivots (LQ-03 needs that for EQH) ...
    assert [s.index for s in highs] == [1, 2]
    # ... and the zigzag keeps the later equal extreme (the recent swing).
    zz = build_zigzag([Swing(1, 105.0, "H", 2), Swing(2, 105.0, "H", 3)])
    assert len(zz) == 1 and zz[0].index == 2


def test_build_sweep_events_honours_its_pivot_arguments(monkeypatch):
    import ict_trader.realtime.analysis.sweeps as sweeps_module

    calls: list[tuple[int, int]] = []

    def fake_find_pivots(df, left, right=PIVOT_RIGHT):
        calls.append((int(left), int(right)))
        return []

    monkeypatch.setattr(sweeps_module, "find_pivots", fake_find_pivots)
    df = _closes([100.0] * 20)

    sweeps_module.build_sweep_events(df, left=2, right=3)
    sweeps_module.build_sweep_events(df)

    # A caller-declared width is used, not the old hardcoded 5/1.
    assert calls == [(2, 3), (PIVOT_LEFT, PIVOT_RIGHT)]
    params = inspect.signature(sweeps_module.build_sweep_events).parameters
    assert params["left"].default == PIVOT_LEFT
    assert params["right"].default == PIVOT_RIGHT


# --------------------------------------------------------------------------- #
# 4a. the engine RR floor is declared, not a hidden 1.0
# --------------------------------------------------------------------------- #
def _engine_frame(n: int = 150) -> pd.DataFrame:
    dates = pd.date_range("2026-01-01", periods=n, freq="15min", tz="UTC")
    close = np.linspace(100.0, 102.0, n)
    return pd.DataFrame({
        "date": dates,
        "open": close - 0.1,
        "high": close + 1.0,
        "low": close - 1.0,
        "close": close,
        "volume": 100.0,
    })


def _rr_cisd(rr: float, end: int = 149) -> CISD:
    entry = 102.0
    risk = 2.0
    return CISD(
        direction="bull", level=entry - 0.5, extreme=entry - risk,
        body_extreme=entry - risk, start=end - 5, end=end,
        sl=entry - risk, target=entry + rr * risk, grade=4,
        checks={"sweep": True, "pda": True, "volume": True},
        quality=0.7, quality_parts={"disp_body_atr": 1.0},
    )


def _patch_engine(monkeypatch, c: CISD) -> None:
    import ict_trader.realtime.analysis.engines.ict as ict_module
    monkeypatch.setattr(
        ict_module, "analyze",
        lambda df, cfg: ICTResult(bars=df, cisds=[c]))
    monkeypatch.setattr(ict_module, "build_setup", lambda df, res, cfg: [c])


def test_engine_rr_floor_is_a_declared_repo_mode():
    assert DEFAULT_ENGINE_RR_MODE == "floor_1_0"
    assert ENGINE_RR_MODES == ("floor_1_0", "none")
    assert ENGINE_RR_FLOORS == {"floor_1_0": 1.0, "none": 0.0}
    assert engine_rr_floor() == 1.0
    for mode in ENGINE_RR_MODES:
        assert ENGINE_RR_MODE_SOURCES[mode]["label"] == "repo"
    with pytest.raises(ValueError):
        engine_rr_floor("filter_2_1")
    with pytest.raises(ValueError):
        ICTEngine(SetupConfig(), ICTConfig(), rr_mode="1.0")


def test_engine_reads_the_declared_rr_mode(monkeypatch):
    c = _rr_cisd(0.5)
    _patch_engine(monkeypatch, c)
    df, ctx = _engine_frame(), AnalysisContext(
        "BTCUSDT", "15m", 1_700_000_000_000)

    # 0.5R is below the declared 1.0 floor -> no signal.
    assert ICTEngine(SetupConfig(market="crypto"),
                     ICTConfig(cisd_all=True)).analyze(df, ctx) == []
    # Declaring the absence of the floor admits it instead of hiding the gate.
    signals = ICTEngine(SetupConfig(market="crypto"), ICTConfig(cisd_all=True),
                        rr_mode="none").analyze(df, ctx)
    assert len(signals) == 1
    assert signals[0].evidence_plain()["rr"] == pytest.approx(0.5)


# --------------------------------------------------------------------------- #
# 4b. at-EQ is a declared predicate, not "premium"
# --------------------------------------------------------------------------- #
def test_at_eq_is_a_declared_predicate_not_premium():
    r = DealingRange(high=100.0, low=0.0)
    assert DEFAULT_AT_EQ_MODE == "buy_side_inclusive"
    assert AT_EQ_MODES == ("buy_side_inclusive", "strict", "no_entry")
    assert AT_EQ_ZONE == "at_eq"
    assert r.is_at_eq(50.0)
    assert not r.is_at_eq(50.000001)

    # The historical reading is now an explicit declaration ...
    assert r.zone(50.0) == "premium"
    # ... and the other attested at-EQ readings are their own answer.
    assert r.zone(50.0, at_eq="strict") == AT_EQ_ZONE
    assert r.zone(50.0, at_eq="no_entry") == AT_EQ_ZONE
    # Off-EQ bands are unchanged.
    assert r.zone(60.0) == "premium"
    assert r.zone(40.0) == "discount"

    assert not r.at_eq_blocks_entry(50.0)
    assert r.at_eq_blocks_entry(50.0, at_eq="no_entry")
    with pytest.raises(ValueError):
        r.zone(50.0, at_eq="premium")


# --------------------------------------------------------------------------- #
# 4c. a breaker needs a declared tier, and the sequenced tier needs a sweep
# --------------------------------------------------------------------------- #
class _OB:
    def __init__(self, direction="bull", top=105.0, bottom=100.0, *,
                 mitigated=True, right=6, created=2):
        self.direction = direction
        self.top = top
        self.bottom = bottom
        self.mitigated = mitigated
        self.right = right
        self.created = created


class _Sweep:
    def __init__(self, direction="bull", *, reclaim_bar=5):
        self.direction = direction
        self.reclaim_bar = reclaim_bar


def test_breaker_tier_is_declared():
    assert BREAKER_TIERS == ("minimal", "sequenced")
    assert DEFAULT_BREAKER_TIER == "sequenced"
    assert BREAKER_TIER_SOURCES["minimal"]["label"] == "repo"
    assert BREAKER_TIER_SOURCES["sequenced"]["label"] == "corpus"
    assert ICTConfig().breaker_tier == DEFAULT_BREAKER_TIER
    with pytest.raises(ValueError):
        detect_breakers([_OB()], tier="mitigated")


def test_sequenced_breaker_requires_a_sweep_before_the_ob_flip():
    ob = _OB()  # bullish OB -> a *bearish* breaker when it fails
    # A bare mitigated OB is not a breaker at the corpus tier.
    assert detect_breakers([ob]) == []
    # The sweep must be on the parent OB's side: a bearish breaker follows a
    # buy-side (BSL) raid, labelled direction="bear" by detect_sweeps.
    assert detect_breakers([ob], [_Sweep("bull", reclaim_bar=5)]) == []
    # ... and known before the flip bar (R6 sequence: sweep -> OB fails).
    assert detect_breakers([ob], [_Sweep("bear", reclaim_bar=7)]) == []

    arrays = detect_breakers([ob], [_Sweep("bear", reclaim_bar=5)])
    assert len(arrays) == 1
    a = arrays[0]
    assert a.kind == "breaker" and a.direction == "bear"
    assert (a.top, a.bottom) == (105.0, 100.0)   # parent bounds, not a new zone
    assert a.tier == "sequenced"
    assert a.source == "sequenced_mitigated_ob"


def test_minimal_breaker_tier_is_labelled_when_declared():
    arrays = detect_breakers([_OB()], tier="minimal")
    assert len(arrays) == 1
    assert arrays[0].tier == "minimal"
    assert arrays[0].source == "minimal_mitigated_ob"
    # A non-mitigated OB never becomes a breaker in either tier.
    assert detect_breakers([_OB(mitigated=False)], tier="minimal") == []


def test_analyze_forwards_the_declared_breaker_tier(monkeypatch):
    seen: dict[str, object] = {}

    def spy(order_blocks, sweeps=(), *, tier=DEFAULT_BREAKER_TIER):
        seen["tier"] = tier
        seen["sweeps"] = list(sweeps)
        return []

    monkeypatch.setattr(concepts_module, "detect_breakers", spy)
    concepts_module.analyze(_ohlc(), _indicator_off(breaker_tier="minimal"))

    assert seen["tier"] == "minimal"
    assert isinstance(seen["sweeps"], list)


# --------------------------------------------------------------------------- #
# 4d. the two ``min_stop_atr`` objects now have distinct names
# --------------------------------------------------------------------------- #
def test_stop_floor_and_admission_filter_have_distinct_names():
    params = inspect.signature(structural_stop).parameters
    assert "floor_atr" in params
    assert "min_stop_atr" not in params
    # SetupConfig keeps the admission *filter*, which skips rather than widens
    # a stop (setup.py) -- it is a different object from the risk floor.
    assert "min_stop_atr" in SetupConfig.__dataclass_fields__

    sweep = {"level": 99.8, "extreme": 99.5}
    widened = structural_stop("long", entry=100.0, atr=1.0,
                              sweep=sweep, structure=None)
    assert widened == pytest.approx(99.0)  # STOP_POLICY.floor_atr = 1.0 ATR
    undeclared = structural_stop("long", entry=100.0, atr=1.0,
                                 sweep=sweep, structure=None, floor_atr=0.0)
    assert undeclared == pytest.approx(
        99.5 - STOP_POLICY.buffer_atr * 1.0)
