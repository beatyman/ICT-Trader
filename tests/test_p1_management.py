"""P1-15 -- management objects: holding class, ladder, flat, BE enum, expiry.

``CODE_MAPPING.md`` P1-15 asks for the post-entry layer the corpus specifies but
the checkout lacked:

* ``holding_class`` (V33/MG-11) as a **required** declaration;
* the MG-01 first partial and the MG-02 per-level permission schedule;
* the MG-05 session flat (05:00 London / 11:00 NY AM) in the backtest and the
  paper broker;
* plan immutability (MG-09) and target expiry (TG-08);
* the MG-08 ``BE_MODE`` vocabulary, five mutually exclusive forms including
  ``never``.

These tests pin the declarations and the fail-closed behaviour.  They are red on
the pre-P1-15 checkout because the objects do not exist there.  Every number
asserted below is quoted from the spec's §9.6 / §13.6 table; no test invents a
threshold, and no test claims a ``repo`` value is corpus.
"""
from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError, replace
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from ict_trader.cisd import CISD
from ict_trader.concepts import ICTConfig, ICTResult
from ict_trader.realtime.analysis.risk import (
    MANAGEMENT_RULE_IDS,
    NT_DATA_MISSING,
    NT_VARIANT_UNDECLARED,
    TargetLevel,
    build_risk_plan,
    management_plan_for,
)
from ict_trader.realtime.broker import PaperBroker
from ict_trader.realtime.config import Settings
from ict_trader.realtime.metrics import Metrics
from ict_trader.realtime.models import utc_ms
from ict_trader.realtime.strategy_runner import TradeSignal
from ict_trader.strategy import (
    BE_MODE_FORMS,
    BE_MODE_IMPLEMENTED_FORMS,
    BE_MODE_SOURCES,
    DEFAULT_MANAGEMENT_LADDER,
    HOLDING_CLASSES,
    HOLDING_CLASS_ID,
    HOLDING_CLASS_POLICIES,
    LADDER_PERMISSIONS,
    MG01_LADDER,
    MG02_LEVELS,
    REPO_ONLY_BE_MODES,
    SESSION_FLAT_CLAUSES,
    SESSION_FLAT_ZONE,
    TARGET_EXPIRY_ACTIONS,
    TARGET_EXPIRY_DEADLINES,
    BEMode,
    HoldingClass,
    ManagementLadder,
    PlanImmutabilityError,
    SessionFlat,
    StrategyConfig,
    TargetExpiry,
    build_management_plan,
    mg08_be_mode,
    read_be_mode,
    read_holding_class,
    resolve_session_flat,
    session_flat_deadline,
    session_flat_due,
    simulate_cisd,
)
from ict_trader.variants import VARIANT_REGISTER, DefaultPolicy

NY = ZoneInfo("America/New_York")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def plan(**overrides):
    values = dict(
        holding_class="intraday",
        ladder=MG01_LADDER,
        be_mode="never",
        session_clause="london",
        target_expiry=TargetExpiry("session_flat", "flatten"),
        entry=100.0,
        stop=95.0,
        terminal_target=110.0,
        level_prices=[105.0],
    )
    values.update(overrides)
    return build_management_plan(**values)


def flat_frame(prices: list[float] | None = None) -> pd.DataFrame:
    """A 15m NY frame from 04:00 whose 05:00 bar is the London flat."""
    values = list(prices or [90, 95, 100, 100, 101, 102, 105, 110, 112])
    n = len(values)
    return pd.DataFrame({
        "date": pd.date_range("2026-07-15 04:00", periods=n, freq="15min",
                              tz="America/New_York"),
        "open": values,
        "high": [value + 0.1 for value in values],
        "low": [value - 0.1 for value in values],
        "close": values,
        "volume": [1000] * n,
    })


def run_flat(frame: pd.DataFrame, **cfg) -> object:
    signal = CISD("bull", level=100, extreme=95, body_extreme=95, start=1, end=2,
                  sl=95, target=110, grade=4, checks={"sweep": True})
    result = ICTResult(bars=frame, cisds=[signal], config=ICTConfig())
    base = dict(min_grade=0, min_confluence=0, entry_delay=1, max_bars=20,
                min_rr=0.0, commission_bps=0.0, slippage_bps=0.0)
    return simulate_cisd(frame, result, StrategyConfig(**{**base, **cfg}), "test")


def broker(tmp_path: Path, **overrides) -> PaperBroker:
    settings = Settings(symbols=["BTCUSDT"], intervals=["15m"], mode="demo",
                        data_dir=tmp_path)
    return PaperBroker(settings, Metrics(), **overrides)


def long_signal() -> TradeSignal:
    return TradeSignal(
        id="SIG-P1-15", symbol="BTCUSDT", interval="15m", bar_time=utc_ms(),
        created_at=utc_ms(), direction="long", entry=100.0, stop=98.0,
        target=110.0, rr=6.0, grade=4, quality=0.8, status="ARMED",
        actionable=True, executed=False, reason="unit test",
        checks={"sweep": True}, setup_level=100.0)


# --------------------------------------------------------------------------- #
# 1. holding_class (V33 / MG-11)
# --------------------------------------------------------------------------- #
def test_v33_register_declares_the_three_holding_classes():
    spec = VARIANT_REGISTER[HOLDING_CLASS_ID]
    assert spec.id == HOLDING_CLASS_ID == "V33"
    assert spec.options == HOLDING_CLASSES == ("intraday", "swing", "position")
    assert spec.default_policy is DefaultPolicy.MODE
    assert read_holding_class(HoldingClass.SWING) == "swing"


def test_holding_class_is_required_and_never_defaulted():
    refused = plan(holding_class=None)
    assert refused.blockers == (NT_VARIANT_UNDECLARED,)
    assert refused.is_usable is False
    assert refused.ladder is None and refused.be_mode is None
    with pytest.raises(ValueError):
        read_holding_class("scalp")
    with pytest.raises(ValueError):
        plan(holding_class="scalp")


def test_holding_class_policies_select_flat_and_ladder_length():
    intraday = HOLDING_CLASS_POLICIES["intraday"]
    swing = HOLDING_CLASS_POLICIES["swing"]
    position = HOLDING_CLASS_POLICIES["position"]
    # MG-11: the class selects whether the session flat applies (TM-67).
    assert intraday.session_flat is True
    assert swing.session_flat is False
    assert position.session_flat is False
    # MG-11: the class selects the ladder length; MG-02 is five, TM-14 three,
    # and no position-trade ladder is attested (missing, so not invented).
    assert (intraday.max_levels, swing.max_levels, position.max_levels) == (5, 3, None)


def test_position_class_admits_only_the_never_be_form():
    policy = HOLDING_CLASS_POLICIES["position"]
    assert policy.be_modes == ("never",)
    assert policy.admits_be("never") is True
    assert policy.admits_be("at_first_internal_pool") is False
    with pytest.raises(ValueError, match="TM-33"):
        plan(holding_class="position", session_clause=None,
             be_mode="at_first_internal_pool", ladder=None)
    usable = plan(holding_class="position", session_clause=None,
                  be_mode="never", ladder=None)
    assert usable.is_usable is True and usable.session_flat is None


def test_position_stop_lookback_is_mg11_forty_then_twenty():
    position = HOLDING_CLASS_POLICIES["position"]
    assert position.stop_lookback_days == (40, 20)
    assert position.trailing_family == "position_lookback"
    assert HOLDING_CLASS_POLICIES["intraday"].stop_lookback_days is None
    assert HOLDING_CLASS_POLICIES["swing"].stop_lookback_days is None


# --------------------------------------------------------------------------- #
# 2. BE_MODE (MG-08)
# --------------------------------------------------------------------------- #
def test_mg08_has_exactly_five_mutually_exclusive_forms():
    assert len(BE_MODE_FORMS) == 5
    assert len(set(BE_MODE_FORMS)) == 5
    assert BE_MODE_FORMS == (
        "at_first_internal_pool",
        "after_daily_range_40_50",
        "after_first_profit_third_of_move",
        "after_swing_mitigation",
        "never",
    )
    assert tuple(item.value for item in BEMode) == BE_MODE_FORMS
    assert read_be_mode("never") == "never"
    with pytest.raises(ValueError):
        read_be_mode("at_target")


def test_every_be_form_carries_corpus_provenance():
    for form in BE_MODE_FORMS:
        entry = BE_MODE_SOURCES[form]
        assert entry["label"].startswith("corpus")
        assert "MG-10" in entry["source"]
        assert "doc_" in entry["source"] or "proj_" in entry["source"]


def test_implemented_be_triggers_map_onto_the_mg08_enum():
    assert mg08_be_mode("never") == "never"
    assert mg08_be_mode("after_first_partial") == "at_first_internal_pool"
    # TM-66: no source endorses a bare R-trigger, so it has no MG-08 form and
    # stays a labelled repo trigger instead of being promoted to corpus.
    assert mg08_be_mode("at_r") is None
    assert REPO_ONLY_BE_MODES == ("at_r",)
    assert BE_MODE_IMPLEMENTED_FORMS["at_r"] is None
    with pytest.raises(ValueError):
        mg08_be_mode("at_target")


def test_undeclared_be_mode_refuses_the_plan():
    refused = plan(be_mode=None)
    assert refused.blockers == (NT_VARIANT_UNDECLARED,)
    assert refused.is_usable is False


def test_repo_only_at_r_trigger_cannot_satisfy_mg08():
    refused = plan(be_mode="at_r")
    assert refused.blockers == (NT_DATA_MISSING,)
    assert refused.is_usable is False


def test_unknown_be_mode_is_a_contradiction():
    with pytest.raises(ValueError):
        plan(be_mode="at_target")


# --------------------------------------------------------------------------- #
# 3. Level ladder (MG-01 / MG-02)
# --------------------------------------------------------------------------- #
def test_mg02_transcribes_the_permission_schedule():
    assert tuple(MG02_LEVELS) == ("L1", "L2", "L3", "L4", "L5")
    l1, l2, l3, l4, l5 = (MG02_LEVELS[key] for key in ("L1", "L2", "L3", "L4", "L5"))

    assert (l1.anchor, l1.fraction, l1.permission) == ("first_irl", 0.50, "mandatory")
    assert l1.pip_band == (5.0, 15.0) and l1.move_stop_to_be is True

    assert l2.fraction == 0.25 and l2.permission == "optional"
    assert l2.pip_band == (10.0, 25.0)
    assert l2.requires == ("33/33/33 split",)

    assert l3.anchor == "pdh" and l3.fraction == 0.50
    assert l3.pip_band == (20.0, 60.0)

    assert l4.fraction == 0.25 and l4.permission == "runner"
    assert l4.requires == ("4/4 alignment",) and l4.pip_band == (50.0, 150.0)

    # No fraction is stated for "swing only"; none is invented.
    assert l5.anchor == "pmh_ipda" and l5.fraction is None
    assert l5.permission == "swing_only"
    assert set(LADDER_PERMISSIONS) == {
        "mandatory", "optional", "remaining", "runner", "swing_only"}


def test_mg01_is_fifty_at_the_first_irl_and_fifty_to_the_erl():
    assert MG01_LADDER is DEFAULT_MANAGEMENT_LADDER
    assert MG01_LADDER.levels == (MG02_LEVELS["L1"],)
    assert MG01_LADDER.terminal_anchor == "erl"
    assert MG01_LADDER.fractions == (0.5, 0.5)
    assert MG01_LADDER.total_fraction == pytest.approx(1.0)


def test_mg01_ladder_is_executable_against_an_ordered_price_path():
    MG01_LADDER.validate(entry=100.0, direction="long",
                         terminal_target=110.0, level_prices=[105.0])
    MG01_LADDER.validate(entry=100.0, direction="short",
                         terminal_target=90.0, level_prices=[95.0])


def test_ladder_fractions_must_sum_to_one():
    # L1 50% + L3 50% + a 50% runner is 1.5: the MG-02 table is a permission
    # schedule, not one executable ladder, and it is not silently normalised.
    overfull = ManagementLadder(
        (MG02_LEVELS["L1"], MG02_LEVELS["L3"]), terminal_fraction=0.50)
    assert overfull.total_fraction == pytest.approx(1.5)
    with pytest.raises(ValueError, match="not 1"):
        overfull.validate()


def test_ladder_levels_must_be_ordered_and_before_the_target():
    with pytest.raises(ValueError):
        MG01_LADDER.validate(entry=100.0, direction="long",
                             terminal_target=110.0, level_prices=[90.0])
    with pytest.raises(ValueError):
        MG01_LADDER.validate(entry=100.0, direction="long",
                             terminal_target=110.0, level_prices=[111.0])


def test_a_ladder_longer_than_the_class_maximum_is_refused():
    long = ManagementLadder(
        tuple(MG02_LEVELS[key] for key in ("L1", "L2", "L3", "L4")),
        terminal_fraction=0.50)
    with pytest.raises(ValueError, match="more than"):
        plan(holding_class="swing", ladder=long, session_clause=None,
             level_prices=[101.0, 103.0, 106.0, 109.0])


def test_an_inadmissible_ladder_blocks_the_plan():
    overfull = ManagementLadder(
        (MG02_LEVELS["L1"], MG02_LEVELS["L3"]), terminal_fraction=0.50)
    refused = plan(ladder=overfull, level_prices=[101.0, 105.0])
    assert refused.blockers == (NT_DATA_MISSING,)
    assert refused.ladder is None and refused.is_usable is False


# --------------------------------------------------------------------------- #
# 4. Session flat (MG-05)
# --------------------------------------------------------------------------- #
def test_mg05_clauses_are_the_two_ny_closes():
    london = SESSION_FLAT_CLAUSES["london"]
    ny_am = SESSION_FLAT_CLAUSES["ny_am"]
    assert london.at.strftime("%H:%M") == "05:00"
    assert ny_am.at.strftime("%H:%M") == "11:00"
    assert london.zone == ny_am.zone == SESSION_FLAT_ZONE == "America/New_York"
    assert london.fraction == ny_am.fraction == 1.0  # MG-05 exits 100%
    assert london.overrides_score is True
    assert london.alert_at.strftime("%H:%M") == "04:55"
    assert ny_am.alert_at.strftime("%H:%M") == "10:55"
    with pytest.raises(ValueError):
        SessionFlat("london", datetime.min.time(), fraction=0.5)


def test_session_flat_fires_at_the_instant_not_before():
    london = SESSION_FLAT_CLAUSES["london"]
    assert session_flat_due(pd.Timestamp("2026-07-15 04:59", tz=NY), london) is False
    assert session_flat_due(pd.Timestamp("2026-07-15 05:00", tz=NY), london) is True
    assert session_flat_due(pd.Timestamp("2026-07-15 05:01", tz=NY), london) is True


def test_session_flat_is_dst_safe_on_both_transitions():
    london = SESSION_FLAT_CLAUSES["london"]
    ny_am = SESSION_FLAT_CLAUSES["ny_am"]
    # 05:00 NY is 09:00 UTC in summer (EDT) and 10:00 UTC in winter (EST).
    assert session_flat_due(pd.Timestamp("2026-07-15 09:00", tz="UTC"), london) is True
    assert session_flat_due(pd.Timestamp("2026-01-15 10:00", tz="UTC"), london) is True
    # 2026-11-01 is the fall-back day; 16:00 UTC is 11:00 EST.
    assert session_flat_due(pd.Timestamp("2026-11-01 16:00", tz="UTC"), ny_am) is True
    # The deadline is rebuilt on the calendar date, so it keeps the wall clock.
    summer = session_flat_deadline(
        pd.Timestamp("2026-07-15 04:45", tz=NY), london)
    winter = session_flat_deadline(
        pd.Timestamp("2026-01-15 04:45", tz=NY), london)
    assert summer.strftime("%H:%M") == winter.strftime("%H:%M") == "05:00"
    assert summer.utcoffset() != winter.utcoffset()


def test_intraday_plan_without_a_session_clause_refuses():
    refused = plan(session_clause=None)
    assert NT_DATA_MISSING in refused.blockers
    assert refused.session_flat is None and refused.is_usable is False


def test_session_flat_on_a_non_intraday_class_is_a_category_error():
    for holding_class in ("swing", "position"):
        with pytest.raises(ValueError, match="MG-11/TM-67|TM-67"):
            plan(holding_class=holding_class, session_clause="london")
        with pytest.raises(ValueError, match="MG-11/TM-67|TM-67"):
            resolve_session_flat(holding_class, "london")
    with pytest.raises(ValueError):
        resolve_session_flat(None, "london")


def test_unknown_session_clause_is_refused():
    with pytest.raises(ValueError):
        plan(session_clause="ny_pm")


def test_intraday_plan_carries_the_flat():
    usable = plan()
    assert usable.is_usable is True
    assert usable.session_flat.clause == "london"
    assert usable.to_public()["session_flat"]["at"] == "05:00"


# --------------------------------------------------------------------------- #
# 5. Session flat in the backtest and the paper broker
# --------------------------------------------------------------------------- #
def test_backtest_flattens_an_intraday_position_at_the_declared_clock():
    frame = flat_frame()
    trade = run_flat(frame, holding_class="intraday",
                     session_flat_clause="london").trades[0]
    # The 05:00 bar is index 4; the target at 110 is only reached at index 7.
    assert trade.reason == "session_flat"
    assert trade.exit == pytest.approx(float(frame["close"].iloc[4]))
    assert trade.entry_time.hour == 4 and trade.exit_time.hour == 5
    assert trade.t1_filled is False
    assert trade.holding_class == "intraday"


def test_backtest_flat_counters_are_reported():
    result = run_flat(flat_frame(), holding_class="intraday",
                      session_flat_clause="london")
    assert result.metrics["exits_session_flat"] == 1


def test_backtest_without_a_holding_class_keeps_the_target():
    trade = run_flat(flat_frame()).trades[0]
    assert trade.reason == "tp"
    assert trade.holding_class == ""


def test_backtest_swing_class_has_no_intraday_flat():
    trade = run_flat(flat_frame(), holding_class="swing").trades[0]
    assert trade.reason == "tp"
    assert trade.holding_class == "swing"


def test_broker_session_flat_closes_the_position(tmp_path):
    b = broker(tmp_path, holding_class="intraday",
               session_flat_clause="ny_am")
    assert asyncio.run(b.execute(long_signal(), 100.0)).accepted
    assert asyncio.run(b.enforce_session_flat(
        "BTCUSDT", datetime(2026, 7, 15, 10, 59, tzinfo=NY), 101.0)) is False
    assert "BTCUSDT" in b.positions
    assert asyncio.run(b.enforce_session_flat(
        "BTCUSDT", datetime(2026, 7, 15, 11, 0, tzinfo=NY), 101.0)) is True
    assert "BTCUSDT" not in b.positions
    assert b.trades[-1].exit_reason == "session_flat"


def test_broker_without_a_management_declaration_never_flattens(tmp_path):
    b = broker(tmp_path)
    assert asyncio.run(b.execute(long_signal(), 100.0)).accepted
    assert asyncio.run(b.enforce_session_flat(
        "BTCUSDT", datetime(2026, 7, 15, 23, 0, tzinfo=NY), 101.0)) is False
    assert "BTCUSDT" in b.positions
    assert b.snapshot()["management"]["session_flat"] is None


# --------------------------------------------------------------------------- #
# 6. Plan immutability (MG-09)
# --------------------------------------------------------------------------- #
def test_management_plan_is_a_frozen_value():
    usable = plan()
    with pytest.raises(FrozenInstanceError):
        usable.ladder = None  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        usable.entry = 101.0  # type: ignore[misc]


def test_redraw_is_allowed_before_entry_and_refused_after_entry():
    pre_entry = plan(entry=None, stop=None, terminal_target=None,
                     level_prices=None)
    revised = pre_entry.redraw(be_mode=BEMode.AT_FIRST_INTERNAL_POOL.value)
    assert revised.be_mode == "at_first_internal_pool"
    assert pre_entry.be_mode == "never"  # the original is unchanged

    post_entry = plan()
    with pytest.raises(PlanImmutabilityError):
        post_entry.redraw(be_mode=BEMode.NEVER.value)


def test_check_redraw_refuses_a_changed_ladder_after_entry():
    post_entry = plan()
    same = replace(post_entry)
    post_entry.check_redraw(same)  # an identical copy is not a redraw
    changed = replace(
        post_entry,
        ladder=ManagementLadder((MG02_LEVELS["L3"],), terminal_fraction=0.50))
    with pytest.raises(PlanImmutabilityError):
        post_entry.check_redraw(changed)


def test_stop_may_tighten_but_never_widen():
    long_plan = plan()
    long_plan.check_stop(96.0)  # inward for a long
    with pytest.raises(PlanImmutabilityError):
        long_plan.check_stop(94.0)  # widening

    short_plan = plan(entry=100.0, stop=105.0)
    short_plan.check_stop(104.0)  # inward for a short
    with pytest.raises(PlanImmutabilityError):
        short_plan.check_stop(106.0)


# --------------------------------------------------------------------------- #
# 7. Target expiry (TG-08)
# --------------------------------------------------------------------------- #
def test_tg08_declares_the_deadline_and_the_disposition():
    assert TARGET_EXPIRY_DEADLINES == ("session_flat", "next_window")
    assert TARGET_EXPIRY_ACTIONS == ("reduce", "flatten")
    flatten = TargetExpiry("session_flat", "flatten")
    assert flatten.action(reached=True) == "hold"
    assert flatten.action(reached=False) == "flatten"
    reduce = TargetExpiry("next_window", "reduce", reduce_fraction=0.5)
    assert reduce.action(reached=False) == "reduce"


def test_tg08_reduce_needs_the_missing_fraction():
    with pytest.raises(ValueError, match="reduce"):
        TargetExpiry("session_flat", "reduce")
    with pytest.raises(ValueError):
        TargetExpiry("session_flat", "flatten", reduce_fraction=0.5)
    with pytest.raises(ValueError):
        TargetExpiry("session_close", "flatten")
    with pytest.raises(ValueError):
        TargetExpiry("session_flat", "close")


def test_plan_without_target_expiry_refuses():
    refused = plan(target_expiry=None)
    assert refused.blockers == (NT_DATA_MISSING,)
    assert refused.target_expiry is None and refused.is_usable is False


def test_risk_plan_states_the_expiry_on_every_target():
    expiry = TargetExpiry("session_flat", "flatten")
    stated = build_risk_plan(
        "long", 100.0, 2.0, {"level": 98.0, "extreme": 98.0}, None, [108.0],
        target_expiry=expiry)
    assert stated.blockers == ()
    assert stated.target_expiry is expiry
    assert all(level.expires_at == "session_flat"
               for level in stated.target_levels)
    assert stated.target_levels[0].to_public()["expires_at"] == "session_flat"

    unstated = build_risk_plan(
        "long", 100.0, 2.0, {"level": 98.0, "extreme": 98.0}, None, [108.0])
    assert unstated.target_expiry is None
    assert all(level.expires_at is None for level in unstated.target_levels)
    assert "expires_at" not in TargetLevel(108.0).to_public()  # legacy shape


def test_management_plan_for_carries_the_risk_plan_without_authoring_prices():
    expiry = TargetExpiry("session_flat", "flatten")
    risk_plan = build_risk_plan(
        "long", 100.0, 2.0, {"level": 98.0, "extreme": 98.0}, None, [108.0],
        target_expiry=expiry)
    mgmt = management_plan_for(
        risk_plan, entry=100.0, holding_class="intraday", be_mode="never",
        session_clause="london", level_prices=[108.0])
    assert mgmt.is_usable is True
    assert mgmt.stop == risk_plan.stop
    assert mgmt.terminal_target == risk_plan.targets[-1]
    assert mgmt.entry == 100.0
    assert set(MANAGEMENT_RULE_IDS) >= {"MG-01", "MG-05", "MG-08", "TG-08"}


def test_management_plan_for_inherits_a_refused_risk_plan():
    refused = build_risk_plan(
        "long", 100.0, 2.0, {"level": 98.0, "extreme": 98.0}, None, [106.0],
        rr_mode="floor_3_1",
        target_expiry=TargetExpiry("session_flat", "flatten"))
    assert refused.blockers
    mgmt = management_plan_for(
        refused, entry=100.0, holding_class="intraday", be_mode="never",
        session_clause="london")
    assert mgmt.blockers == refused.blockers
    assert mgmt.ladder is None and mgmt.is_usable is False


# --------------------------------------------------------------------------- #
# 8. Config wiring (V33 + MG-05)
# --------------------------------------------------------------------------- #
def test_strategy_config_reads_v33_and_mg05():
    default = StrategyConfig()
    assert default.declared_holding_class() is None
    assert default.declared_session_flat() is None

    declared = StrategyConfig(holding_class="intraday",
                              session_flat_clause="ny_am")
    assert declared.declared_holding_class() == "intraday"
    assert declared.declared_session_flat().at.strftime("%H:%M") == "11:00"
    assert declared.config_hash != default.config_hash

    with pytest.raises(ValueError):
        StrategyConfig(holding_class="scalp")
    with pytest.raises(ValueError):
        StrategyConfig(holding_class="swing", session_flat_clause="london")
    with pytest.raises(ValueError):
        StrategyConfig(session_flat_clause="london")
