"""P1-07 - CHoCH, displacement (V25), MSS family (V07) and the closed-bar guard.

Pins five objects from ``CODE_MAPPING.md`` P1-07 and ``SYSTEM_SPEC.md``:

* **ST-08 / MS-R25** -- ``CHoCH`` is a third structure label, never a synonym
  for MSS or BOS, and it is *unconfirmed* until the first BOS in the new
  direction.  An unconfirmed CHoCH is a refusal (``NT_SHIFT_UNCONFIRMED``), not
  a missing MSS.
* **V07 / SN:C12** -- six non-equivalent MSS families whose MSS/CHoCH roles are
  inverted between authorities.  The family is a BLOCK fork: no family is
  selected by default, and a declared family stamps its ST-09 level
  requirement on every event.
* **V25 / PP:C17 / DP-04** -- one declared displacement detector.  The corpus
  supplies no magnitude, so a form that gates on one refuses with
  ``NT_NO_DISPLACEMENT`` until the run declares the value; the labelled repo
  detector grades and never bounds.
* **CB-01 / CB-10** -- the engine refuses to evaluate a forming last bar.
* **§8.1** -- the emitted evidence carries the MSS and the declared
  displacement verdict as separate keys.

Fixtures are closed synthetic bars; there is no network or vendor input.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from ict_trader.cisd import CISD
from ict_trader.concepts import (
    CHoCH_LABEL,
    ICTConfig,
    ICTResult,
    MSS_FAMILIES,
    MSS_FAMILY_SPECS,
    MSS_LEVEL_IMPLEMENTATION,
    MSS_LEVEL_SELECTIONS,
    STRUCTURE_LABELS,
    StructureEvent,
    Swing,
    analyze,
    detect_structure,
    structure_family_spec,
    structure_label,
)
from ict_trader.realtime.analysis import AnalysisContext
from ict_trader.realtime.analysis.engines.ict import (
    NT_NO_DISPLACEMENT,
    NT_NO_SHIFT,
    NT_SHIFT_UNCONFIRMED,
    ICTEngine,
)
from ict_trader.realtime.analysis.evidence import (
    DISPLACEMENT_FORMS,
    REPO_DISPLACEMENT_FORM,
    DisplacementSpec,
    NT_DATA_MISSING,
    NT_VARIANT_UNDECLARED,
    default_displacement_spec,
    displacement_spec_from_variants,
    evaluate_displacement,
    last_bar_closed,
    structure_evidence,
    unconfirmed_shift_evidence,
)
from ict_trader.realtime.analysis.structure import pivot_bos_bias
from ict_trader.setup import SetupConfig
from ict_trader.variants import RefusalCode, VariantConfig, VariantUndeclared


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #
def _closes(values: list[float]) -> pd.DataFrame:
    """A close-only frame; ``detect_structure`` reads nothing else."""
    n = len(values)
    return pd.DataFrame({
        "date": pd.date_range("2026-01-01", periods=n, freq="15min"),
        "open": values, "high": [v + 0.5 for v in values],
        "low": [v - 0.5 for v in values], "close": values, "volume": 1.0,
    })


#: One bear MSS, then a bullish counter-trend break (index 7) and the first
#: BOS in the new direction (index 9) that confirms it.  Trend is -1 at 7 and
#: +1 at 9.
_SHIFT_CLOSES = [96, 96, 96, 96, 96, 94.0, 96, 100.5, 96, 102.0, 102, 102]
_SHIFT_SWINGS = [
    Swing(1, 100.0, "H", 2),
    Swing(3, 95.0, "L", 4),
    Swing(6, 98.0, "H", 7),
    Swing(8, 101.0, "H", 9),
]


def _shift_frame(*, truncate: bool = False) -> pd.DataFrame:
    closes = _SHIFT_CLOSES[:8] if truncate else list(_SHIFT_CLOSES)
    return _closes(closes)


def _trend_frame() -> pd.DataFrame:
    """Realtime 1/1-pivot frame: a down leg, a bullish CHoCH, then a BOS."""
    closes = [101, 102, 103, 104, 105, 104, 103, 102, 101, 102, 103, 104, 105,
              104, 103, 101, 100, 99, 101, 102, 104, 106, 108, 106, 105, 107,
              109, 108, 111, 112]
    n = len(closes)
    return pd.DataFrame({
        "date": pd.date_range("2026-01-01", periods=n, freq="15min", tz="UTC"),
        "open": [c - 0.2 for c in closes],
        "high": [c + 0.5 for c in closes],
        "low": [c - 0.5 for c in closes],
        "close": closes, "volume": 100.0,
    })


def _ohlc(n: int = 80, *, step: float = 0.05) -> pd.DataFrame:
    close = np.linspace(100.0, 100.0 + step * (n - 1), n)
    return pd.DataFrame({
        "date": pd.date_range("2026-01-01", periods=n, freq="15min"),
        "open": close - 0.1, "high": close + 1.0,
        "low": close - 1.0, "close": close, "volume": 100.0,
    })


def _engine_frame(n: int = 1500) -> pd.DataFrame:
    dates = pd.date_range("2026-01-01", periods=n, freq="15min", tz="UTC")
    close = np.linspace(100, 102, n)
    return pd.DataFrame({
        "date": dates,
        "open": close - 0.1,
        "high": close + 1,
        "low": close - 1,
        "close": close,
        "volume": 100.0,
    })


def _engine_cisd(end: int = 1499, quality_parts: dict | None = None) -> CISD:
    return CISD(
        direction="bull", level=100.5, extreme=99.0, body_extreme=100.0,
        start=end - 5, end=end, sl=98.0, target=106.0, grade=4,
        checks={"sweep": True, "pda": True, "volume": True},
        quality=0.72, quality_parts=quality_parts,
    )


def _displaced(**overrides) -> dict:
    values = {"disp_body_ratio": 0.70, "disp_body_atr": 1.20,
              "disp_body_multiple": 1.40, "disp_volume_ratio": 1.10}
    values.update(overrides)
    return values


def _engine(monkeypatch, c: CISD, events: list[StructureEvent] | None = None,
            *, spec: DisplacementSpec | None = None) -> ICTEngine:
    import ict_trader.realtime.analysis.engines.ict as ict_module
    monkeypatch.setattr(
        ict_module, "analyze",
        lambda df, cfg: ICTResult(bars=df, cisds=[c],
                                  events=list(events or [])),
    )
    monkeypatch.setattr(ict_module, "build_setup", lambda df, res, cfg: [c])
    kwargs = {} if spec is None else {"displacement_spec": spec}
    return ICTEngine(SetupConfig(market="crypto"), ICTConfig(cisd_all=True),
                     **kwargs)


def _mss(index: int, direction: str = "bull") -> StructureEvent:
    return StructureEvent(index=index, price=1.0, kind="MSS",
                          direction=direction)


def _unconfirmed_choch(index: int, direction: str = "bull") -> StructureEvent:
    return StructureEvent(index=index, price=1.0, kind="CHoCH",
                          direction=direction, family="F5",
                          level_selection="most_recent_confirmed",
                          level_requirement="protected_swing",
                          level_status="missing", confirmed=False)


# --------------------------------------------------------------------------- #
# 1. CHoCH vs MSS (ST-08 / MS-R25)
# --------------------------------------------------------------------------- #
def test_structure_labels_are_declared():
    assert STRUCTURE_LABELS == ("MSS", "BOS", "CHoCH")
    assert CHoCH_LABEL == "CHoCH"


def test_a_break_with_no_established_trend_is_mss_not_choch():
    # ST-06: trend_state == 0 is not a reversal.  Even the CHoCH-first family
    # must label the first break MSS.
    events, _ = detect_structure(_shift_frame(), _SHIFT_SWINGS, mss_family="F5")
    assert events[0].kind == "MSS"
    assert structure_label(0, "bull", family="F5") == "MSS"
    assert structure_label(0, "bear", family="F5") == "MSS"


def test_f5_labels_the_counter_trend_break_choch_while_f1_labels_mss():
    events, _ = detect_structure(_shift_frame(), _SHIFT_SWINGS, mss_family="F5")
    assert [e.kind for e in events] == ["MSS", "CHoCH", "BOS"]

    for family in (None, "F1", "F2", "F3", "F4", "F6"):
        events, _ = detect_structure(_shift_frame(), _SHIFT_SWINGS,
                                     mss_family=family)
        assert [e.kind for e in events] == ["MSS", "MSS", "BOS"], family


def test_unconfirmed_choch_is_confirmed_by_the_first_bos_in_the_new_direction():
    short, _ = detect_structure(_shift_frame(truncate=True), _SHIFT_SWINGS,
                                mss_family="F5")
    choch = short[-1]
    assert choch.kind == "CHoCH"
    assert choch.confirmed is False
    assert choch.confirmed_at is None
    assert choch.is_confirmed() is False
    assert choch.is_confirmed(choch.index) is False

    full, _ = detect_structure(_shift_frame(), _SHIFT_SWINGS, mss_family="F5")
    choch = full[1]
    assert choch.kind == "CHoCH"
    assert choch.confirmed is True
    assert choch.confirmed_at == 9
    assert full[2].kind == "BOS"
    assert choch.is_confirmed(8) is False
    assert choch.is_confirmed(9) is True


def test_an_opposite_break_fails_the_choch_and_never_confirms_it():
    # MS-R50: a BOS back in the old direction is a failed CHoCH.  Price closes
    # back below the broken low after the CHoCH and never prints a new BOS.
    closes = list(_SHIFT_CLOSES[:8]) + [95.0, 94.0, 94.0, 94.0]
    events, _ = detect_structure(_closes(closes), _SHIFT_SWINGS,
                                 mss_family="F5")
    choch = [e for e in events if e.kind == "CHoCH"][0]
    assert choch.confirmed is False
    assert choch.confirmed_at is None


def test_structure_evidence_never_returns_an_unconfirmed_choch():
    times = [1_000 * i for i in range(100)]
    choch = _unconfirmed_choch(7)
    assert structure_evidence([choch], 20, times, direction="long") is None
    unconfirmed = unconfirmed_shift_evidence(
        [choch], 20, times, direction="long")
    assert unconfirmed is not None
    assert unconfirmed["event"] == "CHoCH"
    assert unconfirmed["confirmed"] is False
    # A later confirmed BOS supersedes it: the state is no longer "unconfirmed".
    assert unconfirmed_shift_evidence(
        [choch, StructureEvent(9, 1.0, "BOS", "bull")], 20, times,
        direction="long") is None


def test_realtime_pivot_read_uses_the_same_choch_vocabulary():
    frame = _trend_frame()
    unconfirmed = pivot_bos_bias(frame.iloc[:22].reset_index(drop=True),
                                 left=1, right=1, mss_family="F5")
    assert unconfirmed["label"] == "CHoCH"
    assert unconfirmed["confirmed"] is False
    assert unconfirmed["choch"]["index"] == 21

    confirmed = pivot_bos_bias(frame, left=1, right=1, mss_family="F5")
    assert confirmed["choch"]["confirmed"] is True
    assert confirmed["choch"]["confirmed_at"] == 28

    # V07 is a BLOCK fork: with no family, the ST-06 label only is produced.
    legacy = pivot_bos_bias(frame, left=1, right=1)
    assert legacy["choch"] is None
    assert "CHoCH" not in {legacy["label"]}


# --------------------------------------------------------------------------- #
# 2. V07 family + ST-09 level selection
# --------------------------------------------------------------------------- #
def test_v07_family_register_is_declared_and_blocks_by_default():
    assert MSS_FAMILIES == ("F1", "F2", "F3", "F4", "F5", "F6")
    assert tuple(MSS_FAMILY_SPECS) == MSS_FAMILIES
    for family, spec in MSS_FAMILY_SPECS.items():
        assert spec["level"] in MSS_LEVEL_SELECTIONS
        assert spec["level_status"] in {"corpus", "missing"}
        assert isinstance(spec["choch_first"], bool)
        assert spec["confirmation"] in {"mss", "choch_then_bos",
                                       "mss_then_choch"}
        # Only F5 puts CHoCH before MSS; every other family labels the first
        # counter-trend break MSS.
        assert spec["choch_first"] is (family == "F5")
        assert spec["source"].startswith("[SN:C12]")
    # The three unimplemented ST-09 mains are recorded as gaps, never silently
    # equated with the repo's "most recent confirmed" reading.
    assert MSS_LEVEL_IMPLEMENTATION["most_recent_secondary_swing"] == (
        "most_recent_confirmed")
    assert MSS_LEVEL_IMPLEMENTATION["launching_swing"] is None
    assert MSS_LEVEL_IMPLEMENTATION["protected_swing"] is None
    assert MSS_LEVEL_IMPLEMENTATION["significant_swing"] is None
    assert MSS_FAMILY_SPECS["F5"]["level"] == "protected_swing"
    assert MSS_FAMILY_SPECS["F5"]["level_status"] == "missing"
    # The later confirmation step is recorded, not fabricated: only the
    # first-break partition is computable from OHLC.
    assert MSS_FAMILY_SPECS["F5"]["confirmation"] == "choch_then_bos"
    assert MSS_FAMILY_SPECS["F6"]["confirmation"] == "mss_then_choch"
    # BLOCK: no family is selected for the run.
    assert ICTConfig().structure_family is None


def test_unknown_mss_family_is_refused():
    with pytest.raises(ValueError, match="V07"):
        structure_family_spec("F7")
    with pytest.raises(ValueError, match="MSS family"):
        detect_structure(_shift_frame(), _SHIFT_SWINGS, mss_family="F7")


def test_each_event_records_its_family_and_st09_level_requirement():
    events, _ = detect_structure(_shift_frame(), _SHIFT_SWINGS,
                                 mss_family="F1",
                                 level_selection="all_confirmed")
    assert events
    for event in events:
        assert event.family == "F1"
        assert event.level_requirement == "launching_swing"
        assert event.level_status == "missing"
        assert event.level_selection == "all_confirmed"
    assert MSS_FAMILY_SPECS["F6"]["level"] == "most_recent_secondary_swing"
    assert MSS_FAMILY_SPECS["F6"]["level_status"] == "corpus"


def test_analyze_forwards_only_a_declared_family(monkeypatch):
    import ict_trader.concepts as concepts_module

    seen: list[tuple[str, str | None]] = []
    real = concepts_module.detect_structure

    def spy(df, swings, *, level_selection="most_recent_confirmed",
            mss_family=None):
        seen.append((level_selection, mss_family))
        return real(df, swings, level_selection=level_selection,
                    mss_family=mss_family)

    monkeypatch.setattr(concepts_module, "detect_structure", spy)
    off = dict(show_cisd=False, show_htf_cisd=False, show_sessions=False,
               show_smt=False)

    analyze(_ohlc(), ICTConfig(**off))
    analyze(_ohlc(), ICTConfig(structure_family="F5", **off))

    assert seen == [("most_recent_confirmed", None),
                    ("most_recent_confirmed", "F5")]


# --------------------------------------------------------------------------- #
# 3. V25 displacement detector (DP-01..DP-07, PP:C17)
# --------------------------------------------------------------------------- #
def test_v25_forms_and_the_repo_label_are_declared():
    assert len(DISPLACEMENT_FORMS) == 4  # PP:C17's four detectors
    assert REPO_DISPLACEMENT_FORM not in DISPLACEMENT_FORMS
    spec = default_displacement_spec()
    assert spec.form == REPO_DISPLACEMENT_FORM
    assert spec.variant_option == "labelled repo detector"
    assert spec.label == "repo"


def test_undeclared_v25_blocks_the_dp1_gate():
    verdict = evaluate_displacement(_displaced(), None)
    assert verdict["ok"] is False
    assert verdict["code"] == NT_VARIANT_UNDECLARED
    assert verdict["code"] == RefusalCode.NT_VARIANT_UNDECLARED.value


def test_a_form_without_a_declared_magnitude_refuses_nt_no_displacement():
    spec = DisplacementSpec(form="body_ratio")
    assert spec.missing_thresholds() == ("min_body_ratio",)
    verdict = evaluate_displacement(_displaced(), spec)
    assert verdict["ok"] is False
    assert verdict["code"] == NT_NO_DISPLACEMENT
    assert "DP-04" in verdict["reason"]
    assert verdict["missing_thresholds"] == ["min_body_ratio"]


def test_the_repo_detector_grades_and_never_invents_a_bound():
    # DP-04: magnitude is missing and is graded, never bounded.  A small body
    # ratio therefore does not fail the repo detector.
    verdict = evaluate_displacement({"disp_body_ratio": 0.10},
                                    default_displacement_spec())
    assert verdict["ok"] is True
    assert verdict["thresholds"] == {}
    assert verdict["checks"] == {}


def test_a_declared_magnitude_is_enforced():
    spec = DisplacementSpec(form="body_ratio", min_body_ratio=0.60)
    assert evaluate_displacement({"disp_body_ratio": 0.70}, spec)["ok"] is True
    failed = evaluate_displacement({"disp_body_ratio": 0.50}, spec)
    assert failed["ok"] is False
    assert failed["code"] == NT_NO_DISPLACEMENT
    assert failed["checks"] == {"min_body_ratio": False}


def test_v25_declaration_selects_the_permitted_form():
    repo = displacement_spec_from_variants(
        VariantConfig({"V25": "labelled repo detector"}))
    assert repo.form == REPO_DISPLACEMENT_FORM
    assert repo.variant_option == "labelled repo detector"

    with pytest.raises(VariantUndeclared):
        displacement_spec_from_variants(VariantConfig())

    # The register records only "one of the four"; the form must be named.
    with pytest.raises(VariantUndeclared):
        displacement_spec_from_variants(
            VariantConfig({"V25": "one of the four declared forms"}))

    named = displacement_spec_from_variants(
        VariantConfig({"V25": "one of the four declared forms"}),
        form="body_ratio", min_body_ratio=0.60)
    assert named.form == "body_ratio"
    assert named.min_body_ratio == pytest.approx(0.60)

    # The repo form is not one of the four and must not be smuggled into that
    # option.
    with pytest.raises(ValueError):
        displacement_spec_from_variants(
            VariantConfig({"V25": "one of the four declared forms"}),
            form=REPO_DISPLACEMENT_FORM)


def test_engine_refuses_a_bounded_form_without_its_magnitude(monkeypatch):
    engine = _engine(monkeypatch, _engine_cisd(quality_parts=_displaced()),
                     events=[_mss(1499)],
                     spec=DisplacementSpec(form="body_ratio"))
    signals = engine.analyze(_engine_frame(), AnalysisContext(
        "btcusdt", "15m", 1_700_000_000_000))
    assert signals == []
    assert [r["code"] for r in engine.refusals] == [NT_NO_DISPLACEMENT]
    assert "displacement" in engine.refusals[0]["data_gaps"]
    assert "DP-04" in engine.refusals[0]["detail"]


def test_engine_admits_with_a_declared_magnitude(monkeypatch):
    spec = DisplacementSpec(form="body_ratio", min_body_ratio=0.60)
    engine = _engine(monkeypatch, _engine_cisd(quality_parts=_displaced()),
                     events=[_mss(1499)], spec=spec)
    signals = engine.analyze(_engine_frame(), AnalysisContext(
        "btcusdt", "15m", 1_700_000_000_000))
    assert len(signals) == 1
    evidence = signals[0].evidence_plain()
    assert evidence["displacement_gate"]["ok"] is True
    assert evidence["displacement_gate"]["thresholds"] == {
        "min_body_ratio": pytest.approx(0.60)}


# --------------------------------------------------------------------------- #
# 4. closed-bar guard (CB-01/CB-10, RE:EX-01 window_closed_bar)
# --------------------------------------------------------------------------- #
def _last_open_ms(frame: pd.DataFrame) -> int:
    return int(pd.Timestamp(frame["date"].iloc[-1]).timestamp() * 1000)


def test_last_bar_closed_helper():
    frame = _engine_frame(10)
    opened = _last_open_ms(frame)
    step = 15 * 60_000
    assert last_bar_closed(frame, as_of_ms=opened, interval="15m") is False
    assert last_bar_closed(frame, as_of_ms=opened + step - 1,
                           interval="15m") is False
    assert last_bar_closed(frame, as_of_ms=opened + step,
                           interval="15m") is True
    # A historical/replay window is closed for the guard's purpose.
    assert last_bar_closed(frame, as_of_ms=opened - step,
                           interval="15m") is True
    assert last_bar_closed(frame, as_of_ms=None, interval="15m") is True


def test_engine_refuses_a_forming_last_bar(monkeypatch):
    frame = _engine_frame()
    engine = _engine(monkeypatch, _engine_cisd(quality_parts=_displaced()),
                     events=[_mss(1499)])
    signals = engine.analyze(frame, AnalysisContext(
        "btcusdt", "15m", _last_open_ms(frame) + 1))

    assert signals == []
    assert engine.last_bar_closed is False
    assert [r["code"] for r in engine.refusals] == [NT_DATA_MISSING]
    assert engine.refusals[0]["data_gaps"] == ["closed_bar"]
    assert "CB-01" in engine.refusals[0]["detail"]


def test_engine_evaluates_once_the_bar_has_closed(monkeypatch):
    frame = _engine_frame()
    engine = _engine(monkeypatch, _engine_cisd(quality_parts=_displaced()),
                     events=[_mss(1499)])
    signals = engine.analyze(frame, AnalysisContext(
        "btcusdt", "15m", _last_open_ms(frame) + 15 * 60_000))
    assert engine.last_bar_closed is True
    assert len(signals) == 1


# --------------------------------------------------------------------------- #
# 5. MSS and displacement in the emitted evidence (§8.1 conjunction)
# --------------------------------------------------------------------------- #
def test_emitted_evidence_carries_mss_and_displacement(monkeypatch):
    engine = _engine(monkeypatch, _engine_cisd(quality_parts=_displaced()),
                     events=[_mss(1499)])
    signals = engine.analyze(_engine_frame(), AnalysisContext(
        "btcusdt", "15m", 1_700_000_000_000))
    assert len(signals) == 1
    evidence = signals[0].evidence_plain()
    assert evidence["mss"] is not None
    assert evidence["mss_confirmed"] is True
    assert evidence["displacement"]["disp_body_atr"] == pytest.approx(1.20)
    assert evidence["displacement_gate"]["ok"] is True
    assert "mss" not in evidence["data_gaps"]
    assert "displacement" not in evidence["data_gaps"]


def test_a_bos_only_history_is_refused_as_an_mss_gap(monkeypatch):
    engine = _engine(monkeypatch, _engine_cisd(quality_parts=_displaced()),
                     events=[StructureEvent(1499, 1.0, "BOS", "bull")])
    signals = engine.analyze(_engine_frame(), AnalysisContext(
        "btcusdt", "15m", 1_700_000_000_000))
    assert signals == []
    assert [r["code"] for r in engine.refusals] == [NT_NO_SHIFT]


def test_unconfirmed_shift_is_refused_with_nt_shift_unconfirmed(monkeypatch):
    engine = _engine(monkeypatch, _engine_cisd(quality_parts=_displaced()),
                     events=[_unconfirmed_choch(1499)])
    signals = engine.analyze(_engine_frame(), AnalysisContext(
        "btcusdt", "15m", 1_700_000_000_000))

    assert signals == []
    assert NT_SHIFT_UNCONFIRMED == RefusalCode.NT_SHIFT_UNCONFIRMED.value
    refusal = engine.refusals[0]
    assert refusal["code"] == NT_SHIFT_UNCONFIRMED
    assert "mss" in refusal["data_gaps"]
    assert "displacement" not in refusal["data_gaps"]
