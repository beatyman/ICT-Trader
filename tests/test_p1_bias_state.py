"""P1-02 -- BiasState, the declared hold, the six revision events, the artefact.

The corpus makes bias a *stored, pre-session, held* object (BI-04/BI-05,
SYSTEM_SPEC §3.1/§3.5) and states that a per-call boolean is not a bias object.
These tests pin the object contract, the fail-closed hold/revision lifecycle,
and the written pre-session artefact.  They also assert the *absence* of a
fabricated probability/confidence field: the corpus never converts a bias into
one (``htf_bias`` R29).
"""
from __future__ import annotations

import dataclasses
import json
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from ict_trader.cisd import CISD
from ict_trader.concepts import build_zigzag, find_pivots
from ict_trader.context import (
    BIAS_HOLD_PERIODS,
    BIAS_VOCABULARIES,
    NO_CHANGE_REVISION_EVENTS,
    REVISION_EVENT_EFFECTS,
    REVISION_EVENTS,
    BiasDirection,
    BiasHold,
    BiasHoldPolicy,
    BiasInvalidation,
    BiasState,
    BiasStateStore,
    RevisionEvent,
    apply_bias_revision,
    commit_pre_session,
    serialize_bias,
)
from ict_trader.realtime.analysis.evidence import (htf_bias_evidence,
                                                    htf_bias_states)
from ict_trader.setup import SetupConfig, _SetupPrecompute, _stack_spec, build_setup

PIVOT_LEFT = 5


# --------------------------------------------------------------------------- #
# fixtures and the fixture state
# --------------------------------------------------------------------------- #
def _state(**overrides) -> BiasState:
    fields = dict(
        scope="1D",
        bias=BiasDirection.BULL,
        vocabulary="+neutral",
        as_of=10,
        invalidation=BiasInvalidation(
            "daily close below the prior day low", price=99.0, bar=8),
        revision_trigger=None,
        variant_ids=("V01", "V02", "V03"),
    )
    fields.update(overrides)
    return BiasState(**fields)


def _store(**overrides) -> BiasStateStore:
    return BiasStateStore(
        (_state(**overrides),),
        hold=BiasHoldPolicy(BiasHold.SESSION, horizon_bars=26))


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


def _base_bearish_frame() -> pd.DataFrame:
    return _frame(240, "2025-12-20", 15, 200.0, -0.08, 3.0, 7.0)


def _daily_bullish_frame() -> pd.DataFrame:
    return _frame(120, "2025-09-01", 1440, 100.0, 0.4, 8.0, 6.0)


def _result(df: pd.DataFrame, cisds: list[CISD]) -> SimpleNamespace:
    return SimpleNamespace(
        swings=build_zigzag(find_pivots(df, PIVOT_LEFT, 1)),
        cisds=cisds,
        fvgs=[],
        order_blocks=[],
        pd_arrays=[],
        sweeps=[],
        key_levels=[],
    )


def _cisd(direction: str, level: float, t: int) -> CISD:
    sl = level - 1.0 if direction == "bull" else level + 1.0
    return CISD(
        direction=direction, level=level, extreme=level, body_extreme=level,
        start=max(0, t - 5), end=t, sl=sl, target=None, grade=3,
        checks={"sweep": False, "pda": False},
    )


def _gate_cfg(**overrides) -> SetupConfig:
    cfg = dict(
        market="other",
        stack="1d_15m",
        allow_short=True,
        min_grade=0,
        min_confluence=0,
        require_1d_cloud=False,
        require_1d_macd=False,
        require_1d_tk=False,
        require_4h_cloud=False,
        require_4h_macd=False,
        require_4h_tk=False,
        require_15m_cloud=False,
        require_15m_macd=False,
        require_15m_hist_rising=False,
        use_score=False,
        require_sweep=False,
        origin_filter="any",
        htf_fvg=False,
        target_mode="fixed",
        rr=2.0,
        min_target_rr=0.0,
        max_inducements=-1,
        regime_features=False,
        htf_structure_gate="aligned",
        daily_bias_filter="none",
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
    cfg.update(overrides)
    return SetupConfig(**cfg)


# --------------------------------------------------------------------------- #
# 1. state construction: the declared contract, no fabricated fields
# --------------------------------------------------------------------------- #
def test_state_has_exactly_the_declared_contract_fields():
    names = [f.name for f in dataclasses.fields(BiasState)]
    assert names == ["scope", "bias", "vocabulary", "as_of", "invalidation",
                     "revision_trigger", "variant_ids"]


def test_state_construction_carries_every_declared_value():
    state = _state()
    assert state.scope == "1D"
    assert state.bias is BiasDirection.BULL
    assert state.vocabulary == "+neutral"
    assert state.as_of == 10
    assert state.invalidation is not None
    assert state.invalidation.condition.startswith("daily close")
    assert state.revision_trigger is None  # initial commitment, not missing
    assert state.variant_ids == ("V01", "V02", "V03")

    public = state.to_public()
    assert public["scope"] == "1D"
    assert public["bias"] == "bullish"
    assert public["as_of"] == 10
    assert public["revision_trigger"] is None
    assert public["variant_ids"] == ["V01", "V02", "V03"]
    assert public["missing_fields"] == []


def test_state_never_carries_a_fabricated_probability():
    public = _state().to_public()
    # The corpus does not convert a bias into a confidence or an expectancy
    # (htf_bias R29); the object must not expose such a field at all.
    assert "probability" not in public
    assert "confidence" not in public
    assert "score" not in public
    assert not any("prob" in key.lower() or "confidence" in key.lower()
                   for key in public)


def test_missing_fields_are_explicit_not_invented():
    state = _state(bias=None, invalidation=None, variant_ids=())
    assert state.bias is None
    assert serialize_bias(state.bias) == ""  # no reading, never "neutral"
    assert set(state.missing_fields) == {"bias", "invalidation", "variant_ids"}
    public = state.to_public()
    assert public["bias"] is None
    assert public["invalidation"] is None
    assert set(public["missing_fields"]) == {"bias", "invalidation",
                                             "variant_ids"}
    json.dumps(public)  # JSON-safe by construction


def test_vocabulary_controls_which_reading_can_be_stored():
    assert len(BIAS_VOCABULARIES) == 5
    # BI-03: a binary run cannot name neutral.
    with pytest.raises(ValueError):
        _state(bias="neutral", vocabulary="binary")
    # The vocabulary-specific readings are storable under their own fork.
    assert _state(bias="unclear", vocabulary="+unclear").bias is \
        BiasDirection.UNCLEAR
    assert _state(bias="ambiguous", vocabulary="+ambiguous").bias is \
        BiasDirection.AMBIGUOUS
    with pytest.raises(ValueError):
        _state(vocabulary="+sideways")


def test_only_registered_variant_ids_are_carried():
    with pytest.raises(KeyError):
        _state(variant_ids=("V99",))


def test_scope_must_be_one_object_per_scope():
    with pytest.raises(ValueError):
        _state(scope="  ")
    with pytest.raises(ValueError):
        commit_pre_session(
            (_state(scope="1D"), _state(scope="1D")),
            session_id="2026-11-12", anchor=10, session_open=20)


# --------------------------------------------------------------------------- #
# 2. the six revision events
# --------------------------------------------------------------------------- #
def test_exactly_the_six_declared_revision_events_exist():
    assert len(REVISION_EVENTS) == 6
    assert set(REVISION_EVENT_EFFECTS) == set(REVISION_EVENTS)
    assert NO_CHANGE_REVISION_EVENTS == (RevisionEvent.FAILED_SETUP,)
    assert REVISION_EVENT_EFFECTS[RevisionEvent.FAILED_SETUP] == \
        "no state change"


def test_confirmed_htf_choch_revises_the_state():
    store = _store()
    revised = store.revise(
        "1D", RevisionEvent.CONFIRMED_HTF_CHOCH, bar=12, bias="bear")
    assert revised.bias is BiasDirection.BEAR
    assert revised.as_of == 12
    assert revised.revision_trigger is RevisionEvent.CONFIRMED_HTF_CHOCH
    assert revised.variant_ids == ("V01", "V02", "V03")

    held, refusal = store.read("1D", 12)
    assert refusal is None
    assert held == revised
    # The revised state is stamped at its own bar: it is not knowable earlier.
    before, refusal = store.read("1D", 11)
    assert before is None
    assert refusal is not None and "not knowable" in refusal.detail


def test_failed_setup_does_not_change_the_state():
    store = _store()
    original, _ = store.read("1D", 10)
    assert original is not None
    unchanged = store.revise("1D", RevisionEvent.FAILED_SETUP, bar=11)
    assert unchanged is original
    with pytest.raises(ValueError):
        store.revise("1D", RevisionEvent.FAILED_SETUP, bar=12, bias="bear")


def test_a_revision_needs_the_re_derived_reading():
    store = _store()
    with pytest.raises(ValueError):
        store.revise("1D", RevisionEvent.BARE_HTF_CHOCH, bar=12, bias=None)


def test_a_revision_cannot_precede_the_knowledge_time():
    with pytest.raises(ValueError):
        apply_bias_revision(
            _state(), RevisionEvent.BOTH_HTF_BROKEN, bar=9, bias="bear")


def test_unknown_revision_event_is_refused():
    with pytest.raises(ValueError):
        apply_bias_revision(_state(), "five_minute_wick", bar=12, bias="bear")


# --------------------------------------------------------------------------- #
# 3. declared hold period and expiry
# --------------------------------------------------------------------------- #
def test_the_four_hold_periods_are_the_declared_ones():
    assert set(BIAS_HOLD_PERIODS) == {"session", "week", "multiweek", "open"}


def test_hold_expiry_is_the_declared_horizon():
    policy = BiasHoldPolicy(BiasHold.SESSION, horizon_bars=5)
    store = BiasStateStore((_state(),), hold=policy)
    assert policy.expiry_bar(_state()) == 15
    kept, refusal = store.read("1D", 14)
    assert refusal is None and kept is not None
    expired, refusal = store.read("1D", 15)
    assert expired is None
    assert refusal is not None
    assert refusal.code.value == "NT_NO_BIAS"
    assert "expired" in refusal.detail


def test_a_finite_hold_cannot_use_a_default_count():
    # The corpus supplies no count for session/week/multi-week, so the horizon
    # is a run declaration, never a default.
    with pytest.raises(ValueError):
        BiasHoldPolicy(BiasHold.SESSION)
    with pytest.raises(ValueError):
        BiasHoldPolicy(BiasHold.WEEK)
    with pytest.raises(ValueError):
        BiasHoldPolicy(BiasHold.MULTIWEEK)
    with pytest.raises(ValueError):
        BiasHoldPolicy(BiasHold.SESSION, horizon_bars=0)


def test_open_hold_never_expires_and_takes_no_horizon():
    policy = BiasHoldPolicy(BiasHold.OPEN)
    store = BiasStateStore((_state(),), hold=policy)
    assert policy.expiry_bar(_state()) is None
    kept, refusal = store.read("1D", 10 ** 6)
    assert refusal is None and kept is not None
    with pytest.raises(ValueError):
        BiasHoldPolicy(BiasHold.OPEN, horizon_bars=3)


def test_a_state_is_not_readable_before_its_as_of():
    store = _store()
    state, refusal = store.read("1D", 9)
    assert state is None
    assert refusal is not None and refusal.code.value == "NT_NO_BIAS"


def test_missing_scope_is_a_typed_refusal_not_a_neutral():
    store = _store()
    state, refusal = store.read("4h", 10)
    assert state is None
    assert refusal is not None
    assert refusal.code.value == "NT_NO_BIAS"


# --------------------------------------------------------------------------- #
# 4. the pre-session committed artefact
# --------------------------------------------------------------------------- #
def test_pre_session_artifact_is_written_before_the_session():
    states = (_state(scope="1D", as_of=8), _state(scope="4h", as_of=8,
                                                   bias="bear"))
    art = commit_pre_session(states, session_id="2026-11-12", anchor=9,
                             session_open=10, deadline=9)
    assert art.anchor < art.session_open
    assert art.state("1D") is states[0]
    assert art.state("4h") is states[1]
    assert art.state("weekly") is None
    assert art.deadline == 9
    assert art.missing_fields == ()
    assert art.variant_ids == ("V01", "V02", "V03")

    public = art.to_public()
    assert public["session_id"] == "2026-11-12"
    assert public["anchor"] == 9 and public["session_open"] == 10
    assert len(public["states"]) == 2
    json.dumps(public)  # the artefact is a written, JSON-safe record


def test_pre_session_commit_must_precede_the_open():
    with pytest.raises(ValueError):
        commit_pre_session((_state(),), session_id="s", anchor=10,
                           session_open=10)
    with pytest.raises(ValueError):
        commit_pre_session((_state(),), session_id="s", anchor=11,
                           session_open=10)


def test_pre_session_deadline_is_declared_or_explicitly_missing():
    state = _state(as_of=8)
    art = commit_pre_session((state,), session_id="s", anchor=9,
                             session_open=10)
    assert art.deadline is None
    assert art.missing_fields == ("deadline",)
    assert art.to_public()["missing_fields"] == ["deadline"]
    with pytest.raises(ValueError):
        commit_pre_session((state,), session_id="s", anchor=9,
                           session_open=10, deadline=8)
    with pytest.raises(ValueError):
        commit_pre_session((state,), session_id="s", anchor=9,
                           session_open=10, deadline=10)


def test_artifact_seeds_the_store_and_holds_the_commitment():
    art = commit_pre_session((_state(as_of=8),), session_id="s", anchor=9,
                             session_open=10)
    store = art.as_store(BiasHoldPolicy(BiasHold.SESSION, horizon_bars=26))
    held, refusal = store.read("1D", 10)
    assert refusal is None and held is not None
    assert held.as_of == 8


# --------------------------------------------------------------------------- #
# wiring: setup reads a stored state; evidence exposes state objects
# --------------------------------------------------------------------------- #
def test_setup_reads_one_cached_bias_state_per_bar():
    df, daily = _base_bearish_frame(), _daily_bullish_frame()
    cfg = _gate_cfg()
    spec = _stack_spec(cfg)
    t = len(df) - 1
    pre = _SetupPrecompute(df, _result(df, []), cfg, daily, spec)

    state = pre.bias_state(t)
    assert isinstance(state, BiasState)
    assert state.scope == spec.bias_tf == "1D"
    assert state.as_of == t
    assert state.vocabulary == "+neutral"
    assert state.bias is BiasDirection.BULL  # 1D HH/HL, not the bearish base TF
    assert state.revision_trigger is None
    assert "invalidation" in state.missing_fields
    assert pre.bias_state(t) is state  # computed once per bar, then stored


def test_emitted_setup_carries_the_state_fields():
    df, daily = _base_bearish_frame(), _daily_bullish_frame()
    cfg = _gate_cfg()
    t = len(df) - 1
    level = float(df["close"].iloc[t])
    signals = build_setup(df, _result(df, [_cisd("bull", level, t)]), cfg,
                          daily=daily)
    assert len(signals) == 1
    parts = signals[0].quality_parts
    assert parts["bias_state_scope"] == "1D"
    assert parts["bias_state_as_of"] == t
    assert parts["bias_state_vocabulary"] == "+neutral"
    assert parts["bias_state_revision_trigger"] is None
    assert parts["bias_state_variant_ids"] == []
    assert "invalidation" in parts["bias_state_missing_fields"]
    assert "probability" not in parts


def test_setup_reads_the_committed_pre_session_store():
    df, daily = _base_bearish_frame(), _daily_bullish_frame()
    cfg = _gate_cfg()
    t = len(df) - 1
    level = float(df["close"].iloc[t])
    result = _result(df, [_cisd("bull", level, t)])

    def _held(bias: BiasDirection, as_of: int,
              vocabulary: str = "+neutral") -> BiasState:
        return BiasState(
            scope="1D", bias=bias, vocabulary=vocabulary, as_of=as_of,
            invalidation=None, revision_trigger=None, variant_ids=())

    # The fresh per-bar reading is bullish, but the committed pre-session
    # state says bearish: the store wins, because the bias is held (BI-04).
    bearish = BiasStateStore(
        (_held(BiasDirection.BEAR, t),),
        hold=BiasHoldPolicy(BiasHold.SESSION, horizon_bars=5))
    assert build_setup(df, result, cfg, daily=daily, bias_store=bearish) == []

    bullish = BiasStateStore(
        (_held(BiasDirection.BULL, t),),
        hold=BiasHoldPolicy(BiasHold.SESSION, horizon_bars=5))
    signals = build_setup(df, result, cfg, daily=daily, bias_store=bullish)
    assert len(signals) == 1
    parts = signals[0].quality_parts
    assert parts["bias_state_source"] == "pre_session_store"
    assert parts["bias_state_as_of"] == t

    # BI-03's "both directions" is not a neutral veto: it permits either side.
    both = BiasStateStore(
        (_held(BiasDirection.BOTH, t, "both directions"),),
        hold=BiasHoldPolicy(BiasHold.SESSION, horizon_bars=5))
    assert len(build_setup(df, result, cfg, daily=daily,
                           bias_store=both)) == 1
    short = _result(df, [_cisd("bear", level, t)])
    assert len(build_setup(df, short, cfg, daily=daily,
                           bias_store=both)) == 1


def test_an_expired_hold_refuses_with_a_typed_code():
    df, daily = _base_bearish_frame(), _daily_bullish_frame()
    cfg = _gate_cfg()
    t = len(df) - 1
    level = float(df["close"].iloc[t])
    held = BiasState(
        scope="1D", bias=BiasDirection.BULL, vocabulary="+neutral",
        as_of=t - 1, invalidation=None, revision_trigger=None,
        variant_ids=())
    store = BiasStateStore(
        (held,), hold=BiasHoldPolicy(BiasHold.SESSION, horizon_bars=1))
    refusals: list[dict] = []
    assert build_setup(df, _result(df, [_cisd("bull", level, t)]), cfg,
                       daily=daily, refusals=refusals,
                       bias_store=store) == []
    assert any(r.get("code") == "NT_NO_BIAS"
               and r.get("bias_source") == "pre_session_store"
               for r in refusals)


def test_binary_vocabulary_records_neutral_as_no_reading():
    # BI-03: a binary run has no neutral token, so a neutral structure read is
    # stored as an explicit "no reading" -- never coerced into bull or bear.
    base = _base_bearish_frame()
    flat_daily = _frame(120, "2025-09-01", 1440, 100.0, 0.0, 0.0, 6.0)
    cfg = _gate_cfg(bias_vocabulary="binary", htf_structure_gate="none")
    pre = _SetupPrecompute(base, _result(base, []), cfg, flat_daily,
                           _stack_spec(cfg))
    state = pre.bias_state(len(base) - 1)
    assert state.bias is None
    assert "bias" in state.missing_fields
    assert state.vocabulary == "binary"


def test_htf_evidence_exposes_bias_state_objects():
    rows = []
    t0 = pd.Timestamp("2026-01-01")
    for i in range(25 * 96):
        close = 100.0 + i / 96.0
        rows.append({
            "date": t0 + pd.Timedelta(minutes=15 * i),
            "open": close - 0.10, "high": close + 0.20,
            "low": close - 0.20, "close": close, "volume": 1.0,
        })
    frame = pd.DataFrame(rows)
    states = htf_bias_states(frame, len(frame) - 1, vocabulary="+neutral",
                             variant_ids=("V02", "V03"))
    assert states is not None
    one_d = states["1D"]
    assert isinstance(one_d, BiasState)
    assert one_d.scope == "1D"
    assert one_d.as_of == len(frame) - 1
    assert one_d.bias is BiasDirection.NEUTRAL
    assert one_d.variant_ids == ("V02", "V03")
    # The legacy dict surface is a serializer over the same state objects.
    assert htf_bias_evidence(frame, len(frame) - 1)["1D"] == {
        "bias": "neutral"}
