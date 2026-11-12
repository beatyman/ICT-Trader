"""P0-17 - the admission chain is a declared conjunction, not a score.

Invariants (CODE_MAPPING P0-17; SYSTEM_SPEC §8.1/§8.2, RE:EX-02, PP:CISD-08,
DP-07):

* setup admission is the §8.1 conjunction under a declared gate strength -- one
  failing conjunct rejects, and the legacy 2-of-3 score only applies when it is
  explicitly declared;
* the sequence chain is declared (``sweep -> CISD -> MSS``) and carries no
  invented bar budgets;
* ``structure`` evidence means an **MSS**, never any BOS, and the missing
  ``mss``/``displacement`` evidence is recorded in ``data_gaps``;
* a setup with no displacement evidence is refused with
  ``NT_NO_DISPLACEMENT`` instead of being silently admitted.
"""
from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from ict_trader.cisd import CISD
from ict_trader.concepts import ICTConfig, ICTResult, StructureEvent
from ict_trader.concepts import build_zigzag, find_pivots
from ict_trader.realtime.analysis import AnalysisContext
from ict_trader.realtime.analysis.engines.ict import (
    NT_NO_DISPLACEMENT,
    NT_NO_SHIFT,
    ICTEngine,
)
from ict_trader.realtime.analysis.evidence import (
    displacement_evidence,
    structure_evidence,
)
from ict_trader.rules import SEQUENCE_CHAIN, _sequence_check
from ict_trader.setup import (
    GATE_STRENGTHS,
    SetupConfig,
    _SetupPrecompute,
    _admit,
    build_setup,
)

PIVOT_LEFT = 5


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #
def _frame(n: int, start: str, minutes: int, base: float, slope: float,
           amp: float, period: float) -> pd.DataFrame:
    rows = []
    t0 = pd.Timestamp(start)
    for i in range(n):
        close = base + slope * i + amp * np.sin(i / period)
        rows.append({
            "date": t0 + pd.Timedelta(minutes=minutes * i),
            "open": close - 0.1,
            "high": close + 0.3,
            "low": close - 0.3,
            "close": close,
            "volume": 1.0,
        })
    return pd.DataFrame(rows)


def _base_frame() -> pd.DataFrame:
    return _frame(240, "2025-12-20", 15, 200.0, -0.08, 3.0, 7.0)


def _daily_frame() -> pd.DataFrame:
    return _frame(120, "2025-09-01", 1440, 100.0, 0.4, 8.0, 6.0)


def _result(df: pd.DataFrame, cisds: list[CISD],
            events: list[StructureEvent] | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        swings=build_zigzag(find_pivots(df, PIVOT_LEFT, 1)),
        events=list(events or []),
        cisds=cisds,
        fvgs=[],
        order_blocks=[],
        pd_arrays=[],
        sweeps=[],
        key_levels=[],
    )


def _cisd(direction: str, level: float, t: int, *,
          quality_parts: dict | None = None) -> CISD:
    sl = level - 1.0 if direction == "bull" else level + 1.0
    return CISD(
        direction=direction, level=level, extreme=level, body_extreme=level,
        start=max(0, t - 5), end=t, sl=sl, target=None, grade=3,
        checks={"sweep": False, "pda": False},
        quality_parts={} if quality_parts is None else quality_parts,
    )


def _gate_cfg(**overrides) -> SetupConfig:
    """A permissive config; the HTF checks are the only live admission tier."""
    values = dict(
        market="other",
        stack="1d_15m",
        allow_short=True,
        min_grade=0,
        min_confluence=0,
        min_1d_score=2,
        require_sweep=False,
        origin_filter="any",
        htf_fvg=False,
        target_mode="fixed",
        rr=2.0,
        min_target_rr=0.0,
        max_inducements=-1,
        regime_features=False,
        daily_bias_filter="none",
        htf_structure_gate="none",
        entry_mode="next_open",
        min_disp_body_ratio=0.0,
        min_disp_atr=0.0,
        min_disp_multiple=0.0,
        min_stop_atr=0.0,
        min_setup_score=0.0,
        require_pda_array=False,
        require_first_fvg=False,
        require_fresh_fvg=False,
        pd_gate=False,
        ote_gate=False,
        session_gate="none",
        quality_profile="standard",
    )
    values.update(overrides)
    return SetupConfig(**values)


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


def _engine(monkeypatch, c: CISD,
            events: list[StructureEvent] | None = None) -> ICTEngine:
    import ict_trader.realtime.analysis.engines.ict as ict_module
    monkeypatch.setattr(
        ict_module, "analyze",
        lambda df, cfg: ICTResult(bars=df, cisds=[c], events=list(events or [])),
    )
    monkeypatch.setattr(ict_module, "build_setup", lambda df, res, cfg: [c])
    return ICTEngine(SetupConfig(market="crypto"), ICTConfig(cisd_all=True))


# --------------------------------------------------------------------------- #
# 1. admission is the §8.1 conjunction under a declared gate strength
# --------------------------------------------------------------------------- #
def test_default_gate_strength_is_the_declared_conjunction():
    assert GATE_STRENGTHS == ("hard_veto", "score")
    assert SetupConfig().gate_strength == "hard_veto"
    assert SetupConfig().use_score is None


def test_undeclared_gate_strength_refuses_instead_of_defaulting():
    with pytest.raises(ValueError, match="gate_strength"):
        SetupConfig(gate_strength="two_of_three")


def test_legacy_use_score_is_an_explicit_declaration_not_the_default():
    assert SetupConfig(use_score=True).gate_strength == "score"
    assert SetupConfig(use_score=False).gate_strength == "hard_veto"


@pytest.mark.parametrize("checks", [
    [False, True, True],
    [True, False, True],
    [True, True, False],
])
def test_one_failing_conjunct_rejects_under_hard_veto(checks):
    # hard_veto is the §8.1 conjunction: the failing conjunct rejects even
    # though a 2-of-3 score would pass.
    assert _admit(checks, "hard_veto", 2) is False
    assert _admit(checks, "score", 2) is True


def test_a_tier_with_no_enabled_check_does_not_block():
    assert _admit([], "hard_veto", 2) is True
    assert _admit([], "score", 2) is True


def test_two_of_three_htf_checks_are_rejected_by_default(monkeypatch):
    df, daily = _base_frame(), _daily_frame()
    t = len(df) - 1
    c = _cisd("bull", float(df["close"].iloc[t]), t)
    # One 1D conjunct fails; two of three pass.
    monkeypatch.setattr(
        _SetupPrecompute, "htf_checks",
        lambda self, t, direction, cfg: ([True, True, False], [], []),
    )

    assert _gate_cfg().gate_strength == "hard_veto"
    assert build_setup(df, _result(df, [c]), _gate_cfg(), daily=daily) == []

    # The same 2-of-3 survives only when the score strength is declared.
    scored = build_setup(df, _result(df, [c]),
                         _gate_cfg(gate_strength="score"), daily=daily)
    assert len(scored) == 1
    assert scored[0].quality_parts["gate_strength"] == "score"


# --------------------------------------------------------------------------- #
# 2. the declared CISD/MSS chain, with no invented bar budgets
# --------------------------------------------------------------------------- #
def _sweep(bar: int):
    return SimpleNamespace(sweep_bar=bar)


def _cisd_leg(end: int, direction: str = "bull"):
    return SimpleNamespace(end=end, direction=direction)


def _mss(index: int, direction: str = "bull") -> StructureEvent:
    return StructureEvent(index=index, price=1.0, kind="MSS",
                          direction=direction)


def _bos(index: int, direction: str = "bull") -> StructureEvent:
    return StructureEvent(index=index, price=1.0, kind="BOS",
                          direction=direction)


def test_chain_is_declared_sweep_cisd_mss():
    assert SEQUENCE_CHAIN == ("sweep", "cisd", "mss")
    seq = _sequence_check([_sweep(10)], [_mss(20)], [_cisd_leg(12)])
    assert seq["valid"] is True
    assert seq["chain"] == list(SEQUENCE_CHAIN)
    assert (seq["sweep_bar"], seq["cisd_bar"], seq["mss_bar"]) == (10, 12, 20)
    assert seq["reason"] == "sweep -> CISD -> MSS"


def test_no_invented_12_or_6_bar_budgets_are_applied():
    # CISD 300 bars and MSS 500 bars after the sweep.  The removed repo budgets
    # (MSS <= 12 bars after sweep, CISD <= 6 bars after MSS) rejected this.
    seq = _sequence_check([_sweep(0)], [_mss(500)], [_cisd_leg(300)])
    assert seq["valid"] is True
    assert seq["mss_bar"] - seq["cisd_bar"] == 200


def test_cisd_after_mss_is_not_the_declared_chain():
    seq = _sequence_check([_sweep(10)], [_mss(12)], [_cisd_leg(20)])
    assert seq["valid"] is False
    assert seq["reason"] == "no MSS after CISD"
    assert seq["mss_bar"] is None


def test_bos_cannot_satisfy_the_mss_leg():
    seq = _sequence_check([_sweep(10)], [_bos(20)], [_cisd_leg(12)])
    assert seq["valid"] is False
    assert seq["mss_bar"] is None


def test_cisd_before_the_sweep_is_rejected():
    seq = _sequence_check([_sweep(10)], [_mss(20)], [_cisd_leg(5)])
    assert seq["valid"] is False
    assert seq["reason"] == "no CISD after sweep"


def test_chain_is_directional():
    seq = _sequence_check([_sweep(10)], [_mss(20, "bear")], [_cisd_leg(12, "bull")])
    assert seq["valid"] is False


# --------------------------------------------------------------------------- #
# 3. structure evidence means MSS, never any BOS
# --------------------------------------------------------------------------- #
TIMES = [1_000 * i for i in range(1000)]


def test_structure_evidence_rejects_a_bos_only_history():
    assert structure_evidence([_bos(30)], 40, TIMES) is None


def test_structure_evidence_returns_the_mss_not_a_later_bos():
    out = structure_evidence([_mss(20), _bos(30)], 40, TIMES)
    assert out is not None
    assert out["event"] == "MSS"
    assert out["direction"] == "bullish"


def test_structure_evidence_requires_mss_after_the_sweep():
    assert structure_evidence(
        [_mss(5)], 40, TIMES, direction="long", after_index=10) is None
    out = structure_evidence(
        [_mss(15)], 40, TIMES, direction="long", after_index=10)
    assert out is not None and out["event"] == "MSS"


def test_structure_evidence_filters_by_entry_direction():
    assert structure_evidence(
        [_mss(20, "bear")], 40, TIMES, direction="long") is None
    assert structure_evidence(
        [_mss(20, "bull")], 40, TIMES, direction="long")["direction"] == "bullish"


def test_bos_only_history_is_refused_as_an_mss_gap(monkeypatch):
    c = _engine_cisd(quality_parts={"disp_body_atr": 1.2})
    bos = _bos(1300)
    engine = _engine(monkeypatch, c, events=[bos])
    signals = engine.analyze(_engine_frame(), AnalysisContext(
        "btcusdt", "15m", 1_700_000_000_000))
    # A BOS is not the MSS the conjunction requires: no signal is emitted and
    # the missing conjunct is named by code and by gap.
    assert signals == []
    assert len(engine.refusals) == 1
    refusal = engine.refusals[0]
    assert refusal["code"] == NT_NO_SHIFT == "NT_NO_SHIFT"
    assert "mss" in refusal["data_gaps"]
    assert "displacement" not in refusal["data_gaps"]


# --------------------------------------------------------------------------- #
# 4. missing displacement fails closed with NT_NO_DISPLACEMENT
# --------------------------------------------------------------------------- #
def test_displacement_evidence_requires_a_body_measure():
    assert displacement_evidence(None) is None
    assert displacement_evidence({}) is None
    assert displacement_evidence({"disp_volume_ratio": 2.0}) is None
    assert displacement_evidence({"disp_body_atr": 1.2}) == {"disp_body_atr": 1.2}


def test_missing_displacement_refuses_with_nt_no_displacement(monkeypatch):
    c = _engine_cisd(quality_parts={})
    engine = _engine(monkeypatch, c)
    signals = engine.analyze(_engine_frame(), AnalysisContext(
        "btcusdt", "15m", 1_700_000_000_000))

    assert signals == []
    assert NT_NO_DISPLACEMENT == "NT_NO_DISPLACEMENT"
    assert len(engine.refusals) == 1
    refusal = engine.refusals[0]
    assert refusal["code"] == NT_NO_DISPLACEMENT
    assert "displacement" in refusal["data_gaps"]
    assert refusal["direction"] == "long"


def test_present_displacement_still_emits_the_signal(monkeypatch):
    c = _engine_cisd(quality_parts={
        "disp_body_ratio": 0.70, "disp_body_atr": 1.20,
    })
    engine = _engine(monkeypatch, c)
    signals = engine.analyze(_engine_frame(), AnalysisContext(
        "btcusdt", "15m", 1_700_000_000_000))

    assert len(signals) == 1
    assert engine.refusals == []
    evidence = signals[0].evidence_plain()
    assert evidence["displacement"]["disp_body_atr"] == pytest.approx(1.20)
    assert "displacement" not in evidence["data_gaps"]
