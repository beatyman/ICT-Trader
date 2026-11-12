"""P1-05 -- weekly state: PWH/PWL, CE_wk, HH/HL, V06, sequence, scheduler.

The audit (``CODE_MAPPING.md`` P1-05, ``time_bias.md`` T15) found that the repo
has no weekly-bias object at all: ``grep -i weekly src/`` found only the
``detect_session_levels`` bucket, and every signal path passes
``show_sessions=False``, so PWH/PWL is not even computed where setups are
produced.  The corpus's weekly rows are exact where they exist:

* WB-01: ``CE_wk = (PWH + PWL) / 2``; below CE is bullish, above is bearish,
  and ``price == CE_wk`` has **no branch** (no tolerance band);
* WB-02: weekly HH/HL vs LH/LL;
* WB-03: ``monthly_structure == weekly_structure == direction`` and the
  discount side of ``CE_wk``; weekly alone is "lower probability" and has no
  number;
* WB-04: the V06 anchor fork -- weekly CE (range-anchored) vs weekly open
  (time-anchored) -- two prices that disagree on the same week;
* WB-05: the ordered weekly sequence (weekly open -> wrong side -> Tue/Wed
  extreme -> confirmation -> expansion);
* WB-06: the Tuesday/Wednesday fallback chain ending in reassignment.

The corpus states **no** weekly-bias probability (``weekly_bias`` R20), so this
file also locks the absence of a probability field.

The tests are red on the pre-P1-05 checkout: ``ict_trader.context`` has no
``WeeklyState``/``WeeklyIndex``/``WeeklySequence``/``WeekOpen`` and no CE_wk,
V06 or scheduler read path.
"""
from __future__ import annotations

import json
from dataclasses import replace
from datetime import time as dtime, timedelta
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from ict_trader.cisd import CISD
from ict_trader.concepts import build_zigzag, find_pivots
from ict_trader.context import (
    TUESDAY_CONDITIONS,
    WEEKDAYS,
    WEEKLY_ANCHOR_OPTIONS,
    WEEKLY_ANCHOR_SOURCES,
    WEEKLY_EXTREME_DAYS,
    WEEKLY_PROBABILITY_STATUS,
    WEEK_OPEN_CLOCKS,
    WEEK_OPEN_OPTIONS,
    BiasDirection,
    MidnightOpen,
    WeeklyGateResult,
    WeeklyIndex,
    WeeklyPhase,
    WeeklyRange,
    WeeklyScheduleState,
    WeeklySequence,
    WeeklyState,
    advance_weekly_sequence,
    daily_open_polarity,
    declare_week_open,
    monthly_bars,
    resolve_weekly_anchor,
    start_weekly_sequence,
    structure_from_bars,
    tuesday_profile_ok,
    week_schedule,
    weekly_anchor_bias,
    weekly_anchor_mode,
    weekly_bias_gate,
    weekly_ce,
    weekly_state_at,
)
from ict_trader.setup import SetupConfig, build_setup
from ict_trader.variants import (
    RefusalCode,
    VariantConfig,
    VariantUndeclared,
    read_variant_spec,
)

NY = "America/New_York"
#: The fixture starts Monday 2026-01-05 14:30 UTC == 09:30 America/New_York.
WEEK_ANCHOR = pd.Timestamp("2026-01-05 09:30", tz=NY)


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #
def _weekly_frame(weeks: tuple[tuple[float, float], ...] = (
        (110.0, 100.0), (120.0, 105.0), (130.0, 115.0), (140.0, 125.0)),
) -> pd.DataFrame:
    """Five daily bars per week, each week's first bar exactly at the anchor.

    The weekly clock is ``monday_0930_ny`` and every week's first bar starts at
    Monday 09:30 New York, so the week open is observable.  Highs/lows rise week
    over week, making weekly HH/HL bullish and every prior range known.
    """
    rows = []
    first = WEEK_ANCHOR.date()
    for week_index, (high, low) in enumerate(weeks):
        for day in range(5):
            date = first + timedelta(weeks=week_index, days=day)
            # Localise the wall clock, not the January UTC instant: after the
            # March DST change 09:30 New York is 13:30 UTC, not 14:30.
            stamp = pd.Timestamp.combine(date, dtime(9, 30)).tz_localize(
                NY, ambiguous=False, nonexistent="shift_forward")
            rows.append((stamp.tz_convert("UTC"), low, high, low, high, 1.0))
    return pd.DataFrame(rows, columns=["date", "open", "high", "low", "close",
                                       "volume"])


def _long_weekly_frame() -> pd.DataFrame:
    """Fourteen weeks from January into April, highs/lows rising every week.

    The first month (January) is partial, but February and March are whole and
    their monthly HH/HL is therefore computable -- the WB-03 monthly leg is a
    real computed value, not only a hand-set field.
    """
    weeks = tuple((110.0 + 10.0 * index, 100.0 + 10.0 * index)
                  for index in range(14))
    return _weekly_frame(weeks)


def _monthly_frame() -> pd.DataFrame:
    """Three calendar months of daily bars, each starting at its month anchor."""
    rows = []
    months = (("2026-02-01", 100.0, 90.0), ("2026-03-01", 110.0, 95.0),
              ("2026-04-01", 120.0, 100.0))
    for start, high, low in months:
        first = pd.Timestamp(start, tz=NY).date()
        for day in range(28):
            date = first + timedelta(days=day)
            stamp = pd.Timestamp.combine(date, dtime(0, 0)).tz_localize(
                NY, ambiguous=False, nonexistent="shift_forward")
            rows.append((stamp.tz_convert("UTC"), low, high, low, high, 1.0))
    return pd.DataFrame(rows, columns=["date", "open", "high", "low", "close",
                                       "volume"])


def _state(frame: pd.DataFrame | None = None, *, bar: int = 18,
           variant_ids: tuple[str, ...] = ()) -> WeeklyState:
    frame = _weekly_frame() if frame is None else frame
    state, refusal = WeeklyIndex(
        frame, declare_week_open("monday_0930_ny"), market="other").state(
            bar, variant_ids=variant_ids)
    assert refusal is None, refusal
    assert state is not None
    return state


def _result(df: pd.DataFrame, cisds: list[CISD]) -> SimpleNamespace:
    return SimpleNamespace(
        swings=build_zigzag(find_pivots(df, 5, 1)),
        cisds=cisds,
        fvgs=[],
        order_blocks=[],
        pd_arrays=[],
        sweeps=[],
        key_levels=[],
    )


def _permissive_cfg(**overrides) -> SetupConfig:
    values = dict(
        market="other", stack="1d_15m", allow_short=True, min_grade=0,
        min_confluence=0, require_sweep=False, origin_filter="any",
        htf_fvg=False, target_mode="fixed", rr=2.0, min_target_rr=0.0,
        max_inducements=-1, regime_features=False, min_disp_body_ratio=0.0,
        min_disp_atr=0.0, min_disp_multiple=0.0, min_setup_score=0.0,
        require_pda_array=False, require_first_fvg=False,
        require_fresh_fvg=False, pd_gate=False, ote_gate=False,
        session_gate="none", quality_profile="standard",
        bias_primitive="none",
    )
    values.update(overrides)
    return SetupConfig(**values)


def _cisd(direction: str, level: float, t: int) -> CISD:
    sl = level - 1.0 if direction == "bull" else level + 1.0
    return CISD(
        direction=direction, level=level, extreme=level, body_extreme=level,
        start=max(0, t - 5), end=t, sl=sl, target=None, grade=3,
        checks={"sweep": False, "pda": False},
    )


# --------------------------------------------------------------------------- #
# 1. CE_wk arithmetic and polarity (WB-01), no tolerance, no daily port
# --------------------------------------------------------------------------- #
def test_weekly_ce_is_the_range_midpoint():
    assert weekly_ce(110.0, 100.0) == 105.0
    assert weekly_ce(1.0900, 1.0800) == pytest.approx(1.0850)

    weekly = WeeklyRange("2026-01-19T09:30:00-05:00", 130.0, 115.0)
    assert weekly.ce == 122.5
    public = weekly.to_public()
    assert public["pwh"] == 130.0 and public["pwl"] == 115.0
    assert public["ce"] == 122.5
    assert "WB-01" in public["source"]

    with pytest.raises(ValueError):
        weekly_ce(100.0, 100.0)  # degenerate week has no CE
    with pytest.raises(ValueError):
        weekly_ce(float("nan"), 100.0)


def test_weekly_ce_polarity_and_the_no_branch_at_ce():
    state = _state()
    assert state.range.pwh == 130.0
    assert state.range.pwl == 115.0
    assert state.range.ce == 122.5

    # WB-01/WB-02: below CE is bullish, above is bearish.
    below, refusal = weekly_anchor_bias(replace(state, price=120.0), "weekly CE")
    assert refusal is None and below.bias is BiasDirection.BULL
    above, refusal = weekly_anchor_bias(replace(state, price=130.0), "weekly CE")
    assert refusal is None and above.bias is BiasDirection.BEAR
    # No tolerance band: 122.5001 is above the CE, not "approximately at" it.
    above_tiny, refusal = weekly_anchor_bias(
        replace(state, price=122.5001), "weekly CE")
    assert refusal is None and above_tiny.bias is BiasDirection.BEAR

    # price == CE_wk has no branch in the corpus: coded refusal, never a side.
    at_ce, refusal = weekly_anchor_bias(replace(state, price=122.5), "weekly CE")
    assert at_ce is None
    assert refusal.code is RefusalCode.NT_NO_BIAS
    assert "no tolerance" in refusal.detail


def test_weekly_polarity_does_not_inherit_the_daily_v05_reading():
    # WB-01's polarity is its own: below CE = bullish.  The daily DB-02
    # inverted reading is the opposite boolean on the same *kind* of
    # below-the-anchor price, so porting it here would flip the weekly read.
    state = replace(_state(), price=120.0)
    anchor, refusal = weekly_anchor_bias(state, "weekly CE")
    assert refusal is None and anchor.bias is BiasDirection.BULL

    opening = MidnightOpen(
        price=125.0, at=pd.Timestamp("2026-01-26 00:00", tz=NY),
        day="2026-01-26T00:00:00-05:00", bar=15, kind="NY midnight",
        zone=NY, anchor_time=dtime(0, 0),
        source="daily_bias R5 (00:00 New York); SYSTEM_SPEC GW-03",
        variant_ids=("V04",))
    daily_inverted, refusal = daily_open_polarity(
        opening, 120.0, polarity="inverted")
    assert refusal is None and daily_inverted is BiasDirection.BEAR
    assert anchor.bias is not daily_inverted
    # V05 is not read anywhere in the weekly path: a config declaring the
    # inverted daily reading does not change the V06 anchor mode or its source.
    cfg = _permissive_cfg(
        variant_config=VariantConfig({"V05": "inverted", "V06": "weekly CE"}),
        week_open_clock="monday_0930_ny")
    assert cfg.variant_config.read("V05") == "inverted"
    assert cfg.variant_config.read("V06") == "weekly CE"


# --------------------------------------------------------------------------- #
# 2. V06 anchor fork: two prices, one declared; undeclared refuses
# --------------------------------------------------------------------------- #
def test_v06_is_the_register_fork_and_names_both_anchors():
    spec = read_variant_spec("V06")
    assert spec.name == "weekly P/D anchor"
    assert spec.options == WEEKLY_ANCHOR_OPTIONS == ("weekly CE", "weekly open")
    assert spec.default_policy.value == "BLOCK"
    assert set(WEEKLY_ANCHOR_SOURCES) == set(WEEKLY_ANCHOR_OPTIONS)
    assert "WB-01" in WEEKLY_ANCHOR_SOURCES["weekly CE"]
    assert "WB-04" in WEEKLY_ANCHOR_SOURCES["weekly open"]


def test_unset_v06_refuses_to_pick_a_weekly_anchor():
    with pytest.raises(VariantUndeclared) as raised:
        resolve_weekly_anchor(VariantConfig())
    assert raised.value.variant_id == "V06"
    assert raised.value.refusal.code is RefusalCode.NT_VARIANT_UNDECLARED

    mode, refusal = weekly_anchor_mode(VariantConfig())
    assert mode is None
    assert refusal.variant_id == "V06"
    assert refusal.code is RefusalCode.NT_VARIANT_UNDECLARED


def test_the_anchor_fork_disagrees_on_the_same_week():
    # The corpus's own conflict (premium_discount C2): price above the weekly
    # open but below the range midpoint is premium under WB-04 and discount
    # under WB-01.  Both readings exist; neither is averaged into one.
    state = replace(_state(), price=124.0)
    assert state.week_open == 125.0
    assert state.range.ce == 122.5

    ce_anchor, refusal = weekly_anchor_bias(state, "weekly CE")
    assert refusal is None
    assert ce_anchor.anchor_price == 122.5
    assert ce_anchor.location == "premium"
    assert ce_anchor.bias is BiasDirection.BEAR

    open_anchor, refusal = weekly_anchor_bias(state, "weekly open")
    assert refusal is None
    assert open_anchor.anchor_price == 125.0
    assert open_anchor.location == "discount"
    assert open_anchor.bias is BiasDirection.BULL

    # Above both anchors: premium under both, bearish under both.
    high = replace(state, price=130.0)
    assert weekly_anchor_bias(high, "weekly CE")[0].bias is BiasDirection.BEAR
    assert weekly_anchor_bias(high, "weekly open")[0].bias is BiasDirection.BEAR

    # The fork really differs where the corpus says it does: price above the
    # weekly open but below the range midpoint (C2's disagreeing band).
    above_open = replace(state, price=124.0, week_open=122.0)
    assert weekly_anchor_bias(above_open, "weekly CE")[0].bias is (
        BiasDirection.BEAR)  # 124 > 122.5
    assert weekly_anchor_bias(above_open, "weekly open")[0].bias is (
        BiasDirection.BEAR)  # 124 > 122

    # 123 is above the CE (122.5) but below the open (125): premium on CE,
    # discount on open -- the two booleans disagree on the same week.
    disagreeing = replace(state, price=123.0)
    assert weekly_anchor_bias(
        disagreeing, "weekly CE")[0].bias is BiasDirection.BEAR
    assert weekly_anchor_bias(
        disagreeing, "weekly open")[0].bias is BiasDirection.BULL


def test_weekly_open_equality_also_has_no_branch():
    state = replace(_state(), price=125.0)
    anchor, refusal = weekly_anchor_bias(state, "weekly open")
    assert anchor is None
    assert refusal.code is RefusalCode.NT_NO_BIAS
    assert "weekly open" in refusal.detail


def test_an_unknown_anchor_and_a_naive_input_refuse():
    with pytest.raises(ValueError, match="V06"):
        weekly_anchor_bias(_state(), "daily CE")
    with pytest.raises(ValueError, match="unknown week-open"):
        declare_week_open("sunday_1800_ny")
    with pytest.raises(TypeError):
        weekly_anchor_bias(pd.Timestamp("2026-01-01"), "weekly CE")


# --------------------------------------------------------------------------- #
# 3. weekly state: prior complete week, HH/HL, no lookahead, missing named
# --------------------------------------------------------------------------- #
def test_weekly_state_reads_the_prior_complete_week():
    state = _state()
    assert state.scope == "1W"
    assert state.as_of == 18
    assert state.week_key == "2026-01-26T09:30:00-05:00"
    assert state.week_open_bar == 15
    assert state.week_open == 125.0
    assert state.week_key == state.week_start.isoformat()
    assert state.range.week_key == "2026-01-19T09:30:00-05:00"
    assert state.range.pwh == 130.0
    assert state.range.pwl == 115.0
    assert state.range.ce == 122.5
    # WB-02: the last two complete weeks are 120/105 then 130/115 -> HH/HL.
    assert state.weekly_structure is BiasDirection.BULL
    # Only January exists in the fixture and it is partial, so the monthly leg
    # is honestly missing -- no value is fabricated for WB-03.
    assert state.monthly_structure is None
    assert state.missing_fields == ("monthly_structure", "variant_ids")

    public = state.to_public()
    assert json.dumps(public)
    assert public["weekly_structure"] == "bullish"
    assert public["monthly_structure"] == ""
    assert public["weekly_range"]["ce"] == 122.5
    # No invented weekly probability (weekly_bias R20).
    assert WEEKLY_PROBABILITY_STATUS == "missing"
    assert public["probability_status"] == "missing"
    assert "probability" not in {
        key for key in public if key != "probability_status"
        and key != "probability_source"}


def test_current_week_high_cannot_repaint_the_weekly_state():
    frame = _weekly_frame()
    before = _state(frame, bar=18).to_public()

    mutated = frame.copy()
    # Blow up only the live week's bars (indices 15..19); the prior complete
    # week and the weekly HH/HL must not move.
    mutated.loc[15:, "high"] = 999.0
    mutated.loc[15:, "low"] = 1.0
    after = _state(mutated, bar=18).to_public()

    assert after["weekly_range"] == before["weekly_range"]
    assert after["weekly_structure"] == before["weekly_structure"]
    assert after["weekly_range"]["pwh"] == 130.0


def test_weekly_state_refuses_when_no_prior_week_is_complete():
    frame = _weekly_frame(weeks=((110.0, 100.0),))
    state, refusal = WeeklyIndex(
        frame, declare_week_open("monday_0930_ny"), market="other").state(4)
    assert state is None
    assert refusal.code is RefusalCode.NT_DATA_MISSING
    assert "no complete prior week" in refusal.detail


def test_weekly_state_refuses_a_frame_that_starts_mid_week():
    frame = _weekly_frame().iloc[2:].reset_index(drop=True)  # Wednesday start
    state, refusal = WeeklyIndex(
        frame, declare_week_open("monday_0930_ny"), market="other").state(0)
    assert state is None
    assert refusal.code is RefusalCode.NT_DATA_MISSING
    assert "week anchor" in refusal.detail


def test_weekly_state_at_helper_uses_the_declared_clock():
    state, refusal = weekly_state_at(
        _weekly_frame(), 18, week_open="sunday_1700_ny", market="other")
    # The fixture's first bar is Monday 09:30, not Sunday 17:00, so the Sunday
    # clock refuses rather than inventing a week open.
    assert state is None
    assert refusal.code is RefusalCode.NT_DATA_MISSING
    assert set(WEEK_OPEN_OPTIONS) == set(WEEK_OPEN_CLOCKS)


# --------------------------------------------------------------------------- #
# 4. WB-03 monthly ∧ weekly gate: no average, no invented probability
# --------------------------------------------------------------------------- #
def test_wb03_requires_monthly_and_weekly_to_agree():
    state = replace(_state(), price=120.0, monthly_structure=BiasDirection.BULL)

    passed = weekly_bias_gate(state, "bullish")
    assert passed.bias_ok is True
    assert passed.refusal is None
    assert passed.price_side_ok is True
    assert passed.downgraded is False
    assert passed.to_public()["rule"] == "WB-03"

    # Monthly missing: "weekly bias without monthly = lower probability" is a
    # downgrade, not a number and not a pass.
    missing = weekly_bias_gate(replace(state, monthly_structure=None), "bullish")
    assert missing.bias_ok is False
    assert missing.downgraded is True
    assert missing.refusal.code is RefusalCode.NT_NO_BIAS
    assert "lower probability" in missing.refusal.detail
    assert "%" not in missing.refusal.detail
    assert "confidence" not in missing.refusal.detail

    # Monthly and weekly disagree: a conflict, never an average.
    conflict = weekly_bias_gate(
        replace(state, monthly_structure=BiasDirection.BEAR), "bullish")
    assert conflict.bias_ok is False
    assert conflict.downgraded is True
    assert conflict.refusal.code is RefusalCode.NT_BIAS_CONFLICT


def test_wb03_checks_direction_and_the_discount_side_of_ce():
    state = replace(_state(), price=130.0,
                    monthly_structure=BiasDirection.BULL)

    wrong_side = weekly_bias_gate(state, "bullish")
    assert wrong_side.refusal.code is RefusalCode.NT_WRONG_HALF
    assert wrong_side.price_side_ok is False

    wrong_direction = weekly_bias_gate(
        replace(state, price=120.0), "bearish")
    assert wrong_direction.refusal.code is RefusalCode.NT_BIAS_GATE

    missing_weekly = weekly_bias_gate(
        replace(state, weekly_structure=None,
                monthly_structure=BiasDirection.BULL), "bullish")
    assert missing_weekly.refusal.code is RefusalCode.NT_NO_BIAS


def test_monthly_structure_is_hh_hl_on_complete_months():
    frame = _monthly_frame()
    bars = monthly_bars(frame, NY, market="other")
    complete = bars[(bars["complete"]) & (bars["aligned"])]
    # February and March are complete; April is still forming.
    assert len(complete) == 2
    assert structure_from_bars(complete) is BiasDirection.BULL
    assert structure_from_bars(complete.iloc[:1]) is None


def test_weekly_state_computes_the_monthly_wb03_leg_from_complete_months():
    frame = _long_weekly_frame()
    # Friday of week 13 (2026-04-03): February and March are complete months.
    state, refusal = weekly_index_state(frame, bar=len(frame) - 1)
    assert refusal is None
    assert state.weekly_structure is BiasDirection.BULL
    assert state.monthly_structure is BiasDirection.BULL
    assert "monthly_structure" not in state.missing_fields


def weekly_index_state(frame: pd.DataFrame, *, bar: int):
    return WeeklyIndex(
        frame, declare_week_open("monday_0930_ny"), market="other").state(bar)


# --------------------------------------------------------------------------- #
# 5. WB-05 weekly sequence: order enforced, mirror, reassignment
# --------------------------------------------------------------------------- #
def test_bearish_weekly_sequence_in_order():
    sequence = start_weekly_sequence("bearish", 100.0, as_of=0)
    assert sequence.phase is WeeklyPhase.OPEN_MARKED

    sequence = advance_weekly_sequence(
        sequence, "price_wrong_side", bar=1, price=101.0)
    assert sequence.phase is WeeklyPhase.MANIPULATION

    sequence = advance_weekly_sequence(
        sequence, "extreme_formed", bar=2, day="Tuesday", price=103.0)
    assert sequence.phase is WeeklyPhase.EXTREME
    assert sequence.extreme_day == "Tuesday"
    assert sequence.extreme_price == 103.0

    sequence = advance_weekly_sequence(
        sequence, "confirmation", bar=3, price=99.0)
    assert sequence.phase is WeeklyPhase.CONFIRMATION
    sequence = advance_weekly_sequence(sequence, "expansion", bar=4)
    assert sequence.phase is WeeklyPhase.EXPANSION
    assert json.dumps(sequence.to_public())


def test_bullish_weekly_sequence_mirror_puts_the_judas_below_the_open():
    sequence = start_weekly_sequence("bullish", 100.0, as_of=0)
    sequence = advance_weekly_sequence(
        sequence, "price_wrong_side", bar=1, price=99.0)
    sequence = advance_weekly_sequence(
        sequence, "extreme_formed", bar=2, day="Wednesday", price=98.0)
    assert sequence.phase is WeeklyPhase.EXTREME
    assert sequence.extreme_day == "Wednesday"
    sequence = advance_weekly_sequence(
        sequence, "confirmation", bar=3, price=101.0)
    assert sequence.phase is WeeklyPhase.CONFIRMATION


def test_weekly_sequence_refuses_out_of_order_and_forced_monday():
    sequence = start_weekly_sequence("bullish", 100.0, as_of=0)
    with pytest.raises(ValueError, match="follows the weekly extreme"):
        advance_weekly_sequence(sequence, "confirmation", bar=1, price=101.0)
    with pytest.raises(ValueError, match="manipulates"):
        advance_weekly_sequence(
            sequence, "price_wrong_side", bar=1, price=101.0)  # bullish
    sequence = advance_weekly_sequence(
        sequence, "price_wrong_side", bar=1, price=99.0)
    with pytest.raises(ValueError, match="Tuesday or Wednesday"):
        advance_weekly_sequence(
            sequence, "extreme_formed", bar=2, day="Monday", price=98.0)


def test_weekly_sequence_reassigns_instead_of_forcing_a_late_extreme():
    sequence = start_weekly_sequence("bearish", 100.0, as_of=0)
    sequence = advance_weekly_sequence(
        sequence, "price_wrong_side", bar=1, price=101.0)
    sequence = advance_weekly_sequence(
        sequence, "wednesday_close", bar=3, day="Wednesday")
    assert sequence.phase is WeeklyPhase.REASSIGNED
    assert sequence.reassigned is True

    # Once an extreme has formed, the Wednesday-close reassignment does not
    # apply (R23 reassigns the profile, it does not delete an event).
    formed = advance_weekly_sequence(
        start_weekly_sequence("bearish", 100.0, as_of=0),
        "price_wrong_side", bar=1, price=101.0)
    formed = advance_weekly_sequence(
        formed, "extreme_formed", bar=2, day="Tuesday", price=103.0)
    with pytest.raises(ValueError, match="already formed"):
        advance_weekly_sequence(
            formed, "wednesday_close", bar=3, day="Wednesday")


# --------------------------------------------------------------------------- #
# 6. WB-06 Tue/Wed scheduler and R19's six-gate Tuesday set
# --------------------------------------------------------------------------- #
def test_week_schedule_fallback_chain_and_reassignment():
    assert WEEKDAYS == ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday")
    assert WEEKLY_EXTREME_DAYS == ("Tuesday", "Wednesday")

    monday_failed = week_schedule(
        day="Monday", monday_reached=False, tuesday_reached=False,
        extreme_formed=False)
    assert monday_failed.state is WeeklyScheduleState.PENDING
    assert monday_failed.expected_day == "Tuesday"
    assert "Monday failed" in monday_failed.reasons[0]

    both_failed = week_schedule(
        day="Tuesday", monday_reached=True, tuesday_reached=False,
        extreme_formed=False)
    assert both_failed.state is WeeklyScheduleState.PENDING
    assert both_failed.expected_day == "Wednesday"

    wednesday_close = week_schedule(
        day="Wednesday", monday_reached=True, tuesday_reached=True,
        extreme_formed=False, wednesday_close=True)
    assert wednesday_close.state is WeeklyScheduleState.REASSIGNED
    assert wednesday_close.expected_day is None
    assert "do not force" in wednesday_close.reasons[0]

    thursday = week_schedule(
        day="Thursday", monday_reached=True, tuesday_reached=True,
        extreme_formed=False)
    assert thursday.state is WeeklyScheduleState.REASSIGNED

    complete = week_schedule(
        day="Wednesday", monday_reached=False, tuesday_reached=False,
        extreme_formed=True, extreme_day="Tuesday")
    assert complete.state is WeeklyScheduleState.COMPLETE
    assert complete.expected_day == "Tuesday"

    # A Monday extreme is not one of the corpus's profiles.
    with pytest.raises(ValueError, match="Tuesday or Wednesday"):
        week_schedule(
            day="Monday", monday_reached=False, tuesday_reached=False,
            extreme_formed=True, extreme_day="Monday")
    with pytest.raises(ValueError, match="weekly schedule day"):
        week_schedule(
            day="Sunday", monday_reached=False, tuesday_reached=False,
            extreme_formed=False)


def test_tuesday_profile_is_the_six_gate_and_list():
    all_met = {name: True for name in TUESDAY_CONDITIONS}
    ok, missing = tuesday_profile_ok(**all_met)
    assert ok is True and missing == ()

    one_missing = dict(all_met)
    one_missing["sell_side_liquidity_taken"] = False
    ok, missing = tuesday_profile_ok(**one_missing)
    assert ok is False
    assert missing == ("sell_side_liquidity_taken",)

    # Omitted gates count as unmet; no profile is forced.
    ok, missing = tuesday_profile_ok(**{"higher_timeframe_bias_bullish": True})
    assert ok is False
    assert len(missing) == len(TUESDAY_CONDITIONS) - 1

    with pytest.raises(ValueError, match="unknown Tuesday condition"):
        tuesday_profile_ok(numeric_threshold=3)


# --------------------------------------------------------------------------- #
# 7. build_setup wiring: evidence, V06 refusal, wrong-side gate
# --------------------------------------------------------------------------- #
def test_build_setup_emits_the_weekly_state_and_v06_anchor():
    frame = _weekly_frame()
    t = 18
    result = _result(frame, [_cisd("bull", float(frame["close"].iloc[t]), t)])
    cfg = _permissive_cfg(
        week_open_clock="monday_0930_ny",
        variant_config=VariantConfig({"V06": "weekly CE"}))

    signals = build_setup(frame, result, cfg)

    assert len(signals) == 1
    parts = signals[0].quality_parts
    assert parts["weekly_state_declared"] is True
    assert parts["week_open_clock"] == "monday_0930_ny"
    assert parts["week_open_clock_source"].strip()
    assert parts["weekly_state"]["weekly_range"]["pwh"] == 130.0
    assert parts["weekly_state"]["weekly_range"]["pwl"] == 115.0
    assert parts["weekly_state"]["weekly_range"]["ce"] == 122.5
    assert parts["weekly_anchor_mode"] == "weekly CE"
    assert parts["weekly_anchor"]["anchor_price"] == 122.5
    assert parts["weekly_anchor"]["bias"] == "bearish"  # close 140 > CE
    assert parts["weekly_gate_enabled"] is False
    assert parts["weekly_gate_result"]["rule"] == "WB-03"
    assert parts["weekly_probability_status"] == "missing"


def test_build_setup_weekly_gate_refuses_an_undeclared_v06_or_clock():
    frame = _weekly_frame()
    t = 18
    result = _result(frame, [_cisd("bull", float(frame["close"].iloc[t]), t)])

    with pytest.raises(VariantUndeclared) as raised:
        build_setup(
            frame, result,
            _permissive_cfg(week_open_clock="monday_0930_ny", weekly_gate=True))
    assert raised.value.variant_id == "V06"

    with pytest.raises(ValueError, match="week-open clock"):
        build_setup(
            frame, result,
            _permissive_cfg(
                variant_config=VariantConfig({"V06": "weekly CE"}),
                weekly_gate=True))


def test_build_setup_weekly_gate_refuses_the_wrong_v06_side():
    frame = _weekly_frame()
    t = 18
    result = _result(frame, [_cisd("bull", float(frame["close"].iloc[t]), t)])
    cfg = _permissive_cfg(
        week_open_clock="monday_0930_ny", weekly_gate=True,
        variant_config=VariantConfig({"V06": "weekly open"}))
    refusals: list[dict] = []

    signals = build_setup(frame, result, cfg, refusals=refusals)

    assert signals == []
    assert any(
        refusal["code"] == RefusalCode.NT_BIAS_GATE.value
        and refusal["gate"] == "weekly_anchor"
        and refusal["weekly_anchor_bias"] == "bearish"
        for refusal in refusals), refusals


def test_an_undeclared_weekly_run_stays_absent_and_says_so():
    frame = _weekly_frame()
    t = 18
    result = _result(frame, [_cisd("bull", float(frame["close"].iloc[t]), t)])
    # V06 declared but no week clock: the weekly state is simply absent.
    signals = build_setup(
        frame, result,
        _permissive_cfg(variant_config=VariantConfig({"V06": "weekly CE"})))
    assert len(signals) == 1
    parts = signals[0].quality_parts
    assert parts["weekly_state_declared"] is False
    assert parts["weekly_state"] is None
    assert parts["weekly_anchor"] is None
    assert parts["weekly_anchor_mode"] == "weekly CE"
    assert parts["weekly_probability_status"] == "missing"

    # No week clock: there is nothing to read and no assumed Monday/Sunday week.
    cfg = _permissive_cfg()
    assert cfg.week_open_clock is None
    with pytest.raises(ValueError, match="week-open clock"):
        _ = cfg.week_open
