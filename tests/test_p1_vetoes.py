"""P1-14 - vetoes: heat, correlation, events, kill switch and daily budget.

``CODE_MAPPING.md`` P1-14 asks for the four §9.5 vetoes that were absent:

* a portfolio heat cap (a §13.9 *blocked* parameter: no value is supplied, so
  a run must declare one and an undeclared cap refuses);
* the correlation veto ``RK-13`` with a declared threshold and pair data;
* the event veto ``RK-12`` driven by the economic-calendar input ``I7``, with
  the ``48 h`` window, the NFP/FOMC/CPI avoidance, the ``5-10 min``
  post-``08:30`` wait and the ``defer | cap_50`` response;
* the consecutive-loss kill switch and the daily/trade budget ``RK-11``,
  reset only at the declared ``V35`` boundary.

Every gate is fail-closed: a missing input is a coded ``NT_*`` refusal, never a
default.  These tests are red on the pre-P1-14 checkout because the objects do
not exist there.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from ict_trader.realtime.analysis.risk import (
    BUDGET_HORIZONS,
    BUDGET_RESET_ID,
    BUDGET_RESET_OPTIONS,
    BUDGET_UNITS,
    EVENT_VETO_RESPONSES,
    VETO_ORDER,
    BudgetBoundary,
    BudgetState,
    CalendarRelease,
    CorrelationVeto,
    EconomicCalendar,
    EventVetoPolicy,
    NT_BUDGET_SPENT,
    NT_CORRELATION,
    NT_DATA_MISSING,
    NT_EVENT_VETO,
    NT_SESSION_CLOSED,
    NT_VARIANT_UNDECLARED,
    OpenPositionRisk,
    PortfolioHeatCap,
    RiskVetoBoard,
    TradeBudget,
    VetoDecision,
    VetoRequest,
    apply_veto,
    build_risk_plan,
    correlation_veto,
    event_veto,
    plan_risk_money,
    portfolio_heat_check,
    risk_money,
    session_budget_check,
)
from ict_trader.realtime.config import Settings
from ict_trader.strategy import (
    CONSECUTIVE_LOSS_LIMIT,
    DEFAULT_MAX_DAILY_LOSS_PCT,
    EVENT_VETO_CAP,
    EVENT_VETO_POST_RELEASE_WAIT_MINUTES,
    EVENT_VETO_WINDOW_HOURS,
    MISSING_VETO_INPUTS,
    VETO_REGISTER,
)
from ict_trader.variants import SPEC_REFUSAL_CODES, read_variant_spec

NY = ZoneInfo("America/New_York")
AT = datetime(2026, 3, 10, 9, 0, tzinfo=NY)
BOUNDARY = BudgetBoundary("NY true day")


def _board(**overrides) -> RiskVetoBoard:
    values = dict(
        budget=TradeBudget(unit="fills", horizon="day"),
        boundary=BudgetBoundary("NY true day"),
        heat=PortfolioHeatCap(0.02, source_id="desk/risk"),
        correlation=CorrelationVeto(0.8, source_id="desk/risk"),
        events=EventVetoPolicy("defer"),
    )
    values.update(overrides)
    return RiskVetoBoard(**values)


def _request(**overrides) -> VetoRequest:
    values = dict(
        at=AT,
        equity=100_000.0,
        symbol="BTCUSDT",
        new_risk_money=500.0,
        open_positions=(),
        calendar=EconomicCalendar(()),
        correlations=None,
    )
    values.update(overrides)
    return VetoRequest(**values)


def _plan():
    return build_risk_plan(
        direction="long", entry=100.0, atr=2.0,
        sweep={"level": 98.0, "extreme": 98.0}, structure=None,
        pools=[108.0])


# --------------------------------------------------------------------------- #
# 1. RK-11 budget boundary: V35 is the only declaration
# --------------------------------------------------------------------------- #
def test_budget_boundary_is_the_declared_v35_row():
    assert BUDGET_RESET_ID == "V35"
    assert BUDGET_RESET_OPTIONS == tuple(read_variant_spec("V35").options)
    assert BUDGET_RESET_OPTIONS == ("NY true day", "UTC date", "broker day")

    with pytest.raises(ValueError):
        BudgetBoundary("UTC midnight")

    # The two calendar options disagree on the same instant: 2026-03-10 23:30
    # NY is already 2026-03-11 in UTC, so a UTC reset would start the day early.
    late = datetime(2026, 3, 10, 23, 30, tzinfo=NY)
    assert BudgetBoundary("NY true day").day_key(late) == "2026-03-10"
    assert BudgetBoundary("UTC date").day_key(late) == "2026-03-11"
    assert BudgetBoundary("NY true day").day_key(AT) == "2026-03-10"

    # A naive stamp is never assumed to be UTC (I2 / §1.5).
    with pytest.raises(ValueError):
        BudgetBoundary("NY true day").day_key(datetime(2026, 3, 10, 9, 0))


def test_broker_day_has_no_clock_without_a_declaration():
    with pytest.raises(ValueError):
        BudgetBoundary("broker day")
    broker = BudgetBoundary(
        "broker day", broker_roll="17:00", broker_zone="America/New_York")
    assert broker.day_key(datetime(2026, 3, 10, 16, 0, tzinfo=NY)) == "2026-03-09"
    assert broker.day_key(datetime(2026, 3, 10, 18, 0, tzinfo=NY)) == "2026-03-10"
    with pytest.raises(ValueError):
        BudgetBoundary("broker day", broker_roll="17:00",
                       broker_zone="Not/AZone")


# --------------------------------------------------------------------------- #
# 2. Portfolio heat cap (a §13.9 blocked parameter)
# --------------------------------------------------------------------------- #
def test_portfolio_heat_cap_refuses_above_the_declared_cap():
    cap = PortfolioHeatCap(0.02, source_id="desk/risk")
    within = portfolio_heat_check(
        cap, equity=100_000.0, open_risk_money=1_000.0,
        new_risk_money=900.0)
    assert within.admitted
    assert within.input_dict()["cap_pct"] == pytest.approx(0.02)

    over = portfolio_heat_check(
        cap, equity=100_000.0, open_risk_money=1_000.0,
        new_risk_money=1_100.0)
    assert over.blocker == NT_BUDGET_SPENT
    assert over.size_multiplier == 0.0
    # A3: the refusal carries the failing guard's inputs.
    assert over.input_dict()["new_risk_money"] == pytest.approx(1_100.0)
    assert over.detail


def test_heat_cap_has_no_default_and_no_assumed_zero():
    cap = PortfolioHeatCap(0.02, source_id="desk/risk")
    assert portfolio_heat_check(
        None, equity=100_000.0, open_risk_money=0.0,
        new_risk_money=100.0).blocker == NT_DATA_MISSING
    for equity in (None, 0.0, -1.0):
        assert portfolio_heat_check(
            cap, equity=equity, open_risk_money=0.0,
            new_risk_money=1.0).blocker == NT_DATA_MISSING
    assert portfolio_heat_check(
        cap, equity=100_000.0, open_risk_money=None,
        new_risk_money=1.0).blocker == NT_DATA_MISSING
    assert portfolio_heat_check(
        cap, equity=100_000.0, open_risk_money=0.0,
        new_risk_money=None).blocker == NT_DATA_MISSING

    with pytest.raises(ValueError):
        PortfolioHeatCap(0.02, source_id="")   # anonymous cap
    with pytest.raises(ValueError):
        PortfolioHeatCap(1.5, source_id="desk/risk")


# --------------------------------------------------------------------------- #
# 3. RK-13 correlation veto
# --------------------------------------------------------------------------- #
def test_correlation_veto_skips_a_simultaneous_correlated_position():
    declaration = CorrelationVeto(0.8, source_id="desk/risk")
    positions = (OpenPositionRisk("ETHUSDT", 400.0),)

    blocked = correlation_veto(
        declaration, symbol="BTCUSDT", open_positions=positions,
        correlations={("BTCUSDT", "ETHUSDT"): 0.9})
    assert blocked.blocker == NT_CORRELATION
    assert blocked.input_dict()["correlation"] == pytest.approx(0.9)

    # The pair key order is normalised.
    assert correlation_veto(
        declaration, symbol="BTCUSDT", open_positions=positions,
        correlations={("ETHUSDT", "BTCUSDT"): 0.9}).blocker == NT_CORRELATION

    # Below the declared threshold the entry is admitted.
    assert correlation_veto(
        declaration, symbol="BTCUSDT", open_positions=positions,
        correlations={("BTCUSDT", "ETHUSDT"): 0.79}).admitted


def test_correlation_veto_refuses_unknown_inputs():
    declaration = CorrelationVeto(0.8, source_id="desk/risk")
    positions = (OpenPositionRisk("ETHUSDT", 400.0),)

    assert correlation_veto(
        None, symbol="BTCUSDT", open_positions=positions,
        correlations={("BTCUSDT", "ETHUSDT"): 0.9}).blocker == NT_DATA_MISSING
    assert correlation_veto(
        declaration, symbol="BTCUSDT", open_positions=positions,
        correlations=None).blocker == NT_DATA_MISSING
    # An unknown pair is unknown, not uncorrelated.
    assert correlation_veto(
        declaration, symbol="BTCUSDT", open_positions=positions,
        correlations={("BTCUSDT", "SOLUSDT"): 0.9}).blocker == NT_DATA_MISSING
    assert correlation_veto(
        declaration, symbol="", open_positions=positions,
        correlations={("BTCUSDT", "ETHUSDT"): 0.9}).blocker == NT_DATA_MISSING

    # With no simultaneous position the gate evaluates false from the book
    # alone; it needs no correlation data.
    assert correlation_veto(None, symbol="BTCUSDT").admitted

    with pytest.raises(ValueError):
        CorrelationVeto(0.0, source_id="desk/risk")
    with pytest.raises(ValueError):
        CorrelationVeto(0.8, source_id="")


# --------------------------------------------------------------------------- #
# 4. RK-12 event veto and the I7 calendar
# --------------------------------------------------------------------------- #
def test_event_veto_requires_the_calendar_input_i7():
    policy = EventVetoPolicy("defer")
    assert event_veto(
        policy, calendar=None, at=AT).blocker == NT_DATA_MISSING
    # A naive entry stamp cannot be placed on the clock.
    assert event_veto(
        policy, calendar=EconomicCalendar(()),
        at=datetime(2026, 3, 10, 9, 0)).blocker == NT_DATA_MISSING
    # The response fork itself must be declared (V11).
    assert event_veto(
        None, calendar=EconomicCalendar(()),
        at=AT).blocker == NT_VARIANT_UNDECLARED


def test_event_veto_defers_a_high_impact_release_inside_the_window():
    calendar = EconomicCalendar.from_rows([{
        "at": AT + timedelta(hours=10), "name": "Retail Sales",
        "high_impact": True}])
    blocked = event_veto(EventVetoPolicy("defer"), calendar=calendar, at=AT)
    assert blocked.blocker == NT_EVENT_VETO
    assert blocked.input_dict()["release"]["name"] == "Retail Sales"

    # The same release 49 h out is outside the declared 48 h window.
    outside = EconomicCalendar.from_rows([{
        "at": AT + timedelta(hours=49), "name": "Retail Sales",
        "high_impact": True}])
    assert event_veto(
        EventVetoPolicy("defer"), calendar=outside, at=AT).admitted


def test_event_veto_caps_an_inside_window_entry_at_half_size():
    calendar = EconomicCalendar.from_rows([{
        "at": AT + timedelta(hours=10), "name": "Retail Sales",
        "high_impact": True}])
    decision = event_veto(EventVetoPolicy("cap_50"), calendar=calendar, at=AT)
    assert decision.admitted
    assert decision.size_multiplier == pytest.approx(0.5)
    assert decision.capped is True


def test_event_veto_avoids_nfp_fomc_cpi_by_name():
    # The names are avoided independent of the calendar's impact taxonomy, and
    # the 50% cap does not rescue them.
    same_day = EconomicCalendar.from_rows([{
        "at": datetime(2026, 3, 10, 14, 0, tzinfo=NY), "name": "NFP",
        "high_impact": False}])
    assert event_veto(
        EventVetoPolicy("defer"), calendar=same_day,
        at=AT).blocker == NT_EVENT_VETO
    assert event_veto(
        EventVetoPolicy("cap_50"), calendar=same_day,
        at=AT).blocker == NT_EVENT_VETO

    # Tomorrow's FOMC inside the window is also avoided.
    tomorrow = EconomicCalendar.from_rows([{
        "at": AT + timedelta(hours=24), "name": "FOMC",
        "high_impact": None}])
    assert event_veto(
        EventVetoPolicy("defer"), calendar=tomorrow,
        at=AT).blocker == NT_EVENT_VETO


def test_event_veto_waits_after_the_0830_release():
    release = datetime(2026, 3, 10, 8, 30, tzinfo=NY)
    calendar = EconomicCalendar.from_rows([{
        "at": release, "name": "Retail Sales", "high_impact": True}])
    policy = EventVetoPolicy("defer", post_release_wait_minutes=10)

    assert event_veto(
        policy, calendar=calendar,
        at=release + timedelta(minutes=3)).blocker == NT_EVENT_VETO
    # The wait is over at the declared boundary (10 minutes).
    assert event_veto(
        policy, calendar=calendar,
        at=release + timedelta(minutes=10)).admitted


def test_event_veto_wait_is_declared_inside_the_corpus_range():
    assert 5.0 <= EventVetoPolicy("defer").post_release_wait_minutes <= 10.0
    with pytest.raises(ValueError):
        EventVetoPolicy("defer", post_release_wait_minutes=4)
    with pytest.raises(ValueError):
        EventVetoPolicy("defer", post_release_wait_minutes=11)
    with pytest.raises(ValueError):
        EventVetoPolicy("ignore")            # undeclared V11 response
    with pytest.raises(ValueError):
        EventVetoPolicy("defer", window_hours=0)


def test_event_veto_refuses_an_unclassified_release_inside_the_window():
    unclassified = EconomicCalendar.from_rows([{
        "at": AT + timedelta(hours=20), "name": "PMI",
        "high_impact": None}])
    assert event_veto(
        EventVetoPolicy("cap_50"), calendar=unclassified,
        at=AT).blocker == NT_DATA_MISSING

    # Outside the window the missing taxonomy cannot change the verdict.
    far = EconomicCalendar.from_rows([{
        "at": AT + timedelta(hours=60), "name": "PMI",
        "high_impact": None}])
    assert event_veto(
        EventVetoPolicy("cap_50"), calendar=far, at=AT).admitted


def test_calendar_rejects_a_naive_release_timestamp():
    with pytest.raises(ValueError):
        CalendarRelease(datetime(2026, 3, 10, 8, 30), "NFP", True)
    with pytest.raises(ValueError):
        CalendarRelease(AT, "NFP", high_impact="yes")  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# 5. RK-11 kill switch, daily cap and trade-count cap
# --------------------------------------------------------------------------- #
def test_consecutive_killzone_losses_close_the_session():
    state = BudgetState()
    budget = TradeBudget(unit="fills", horizon="session",
                         max_daily_loss_pct=None)
    assert session_budget_check(
        budget, state, boundary=BOUNDARY, at=AT,
        equity=100_000.0).admitted

    state.record_outcome(BOUNDARY, AT, -100.0, in_killzone=True)
    assert session_budget_check(
        budget, state, boundary=BOUNDARY, at=AT,
        equity=100_000.0).admitted

    state.record_outcome(BOUNDARY, AT, -100.0, in_killzone=True)
    closed = session_budget_check(
        budget, state, boundary=BOUNDARY, at=AT, equity=100_000.0)
    assert closed.blocker == NT_SESSION_CLOSED
    assert closed.input_dict()["killzone_losses"] == 2

    # A latch is not dismissible mid-day (F11).
    assert session_budget_check(
        budget, state, boundary=BOUNDARY, at=AT,
        equity=100_000.0).blocker == NT_SESSION_CLOSED


def test_only_consecutive_killzone_losses_trigger_the_kill_switch():
    budget = TradeBudget(unit="fills", horizon="session",
                         max_daily_loss_pct=None)

    # A winning killzone trade resets the streak.
    reset = BudgetState()
    reset.record_outcome(BOUNDARY, AT, -100.0, in_killzone=True)
    reset.record_outcome(BOUNDARY, AT, 150.0, in_killzone=True)
    reset.record_outcome(BOUNDARY, AT, -100.0, in_killzone=True)
    assert session_budget_check(
        budget, reset, boundary=BOUNDARY, at=AT,
        equity=100_000.0).admitted

    # Losses outside the killzone are not the RK-11 object.
    outside = BudgetState()
    outside.record_outcome(BOUNDARY, AT, -100.0, in_killzone=False)
    outside.record_outcome(BOUNDARY, AT, -100.0, in_killzone=False)
    assert session_budget_check(
        budget, outside, boundary=BOUNDARY, at=AT,
        equity=100_000.0).admitted


def test_daily_loss_cap_latches_the_day():
    state = BudgetState()
    budget = TradeBudget(unit="fills", horizon="day", max_daily_loss_pct=0.01)
    state.record_outcome(BOUNDARY, AT, -1_001.0)
    blocked = session_budget_check(
        budget, state, boundary=BOUNDARY, at=AT, equity=100_000.0)
    assert blocked.blocker == NT_BUDGET_SPENT
    assert blocked.input_dict()["realized_pnl"] == pytest.approx(-1_001.0)

    # The next declared day rolls the latch, not just the counter.
    tomorrow = AT + timedelta(days=1)
    assert session_budget_check(
        budget, state, boundary=BOUNDARY, at=tomorrow,
        equity=100_000.0).admitted


def test_trade_cap_declares_its_unit_and_horizon():
    assert BUDGET_UNITS == ("fills", "attempts")
    assert BUDGET_HORIZONS == ("day", "session")

    filled = BudgetState()
    budget = TradeBudget(unit="fills", horizon="day", max_trades=2)
    filled.record_outcome(BOUNDARY, AT, 10.0)
    filled.record_outcome(BOUNDARY, AT, 10.0)
    assert session_budget_check(
        budget, filled, boundary=BOUNDARY, at=AT,
        equity=100_000.0).blocker == NT_BUDGET_SPENT

    attempted = BudgetState()
    attempts = TradeBudget(unit="attempts", horizon="day", max_trades=1)
    attempted.record_attempt(BOUNDARY, AT)
    assert session_budget_check(
        attempts, attempted, boundary=BOUNDARY, at=AT,
        equity=100_000.0).blocker == NT_BUDGET_SPENT

    with pytest.raises(ValueError):
        TradeBudget(unit="trades", horizon="day")
    with pytest.raises(ValueError):
        TradeBudget(unit="fills", horizon="week")
    # The switch may fire no later than RK-11's attested threshold.
    with pytest.raises(ValueError):
        TradeBudget(unit="fills", horizon="day", kill_switch_losses=3)
    assert TradeBudget(
        unit="fills", horizon="day",
        kill_switch_losses=1).kill_switch_losses == 1


def test_budget_gate_refuses_missing_declarations():
    state = BudgetState()
    budget = TradeBudget(unit="fills", horizon="day")
    assert session_budget_check(
        None, state, boundary=BOUNDARY, at=AT,
        equity=100_000.0).blocker == NT_DATA_MISSING
    # No history is not "zero losses".
    assert session_budget_check(
        budget, None, boundary=BOUNDARY, at=AT,
        equity=100_000.0).blocker == NT_DATA_MISSING
    # V35 undeclared: the budget may not reset at an assumed boundary.
    assert session_budget_check(
        budget, state, boundary=None, at=AT,
        equity=100_000.0).blocker == NT_VARIANT_UNDECLARED
    assert session_budget_check(
        budget, state, boundary=BOUNDARY,
        at=datetime(2026, 3, 10, 9, 0),
        equity=100_000.0).blocker == NT_DATA_MISSING

    capped = TradeBudget(unit="fills", horizon="day", max_daily_loss_pct=0.01)
    assert session_budget_check(
        capped, state, boundary=BOUNDARY, at=AT,
        equity=None).blocker == NT_DATA_MISSING


def test_budget_state_resets_at_the_declared_boundary():
    budget = TradeBudget(unit="fills", horizon="day", max_daily_loss_pct=0.01)
    late = datetime(2026, 3, 10, 23, 30, tzinfo=NY)
    state = BudgetState()
    state.record_outcome(BOUNDARY, late, -2_000.0)
    assert state.day == "2026-03-10"
    assert session_budget_check(
        budget, state, boundary=BOUNDARY, at=late,
        equity=100_000.0).blocker == NT_BUDGET_SPENT

    # 00:30 NY is a new NY budget day: the loss and the latch reset.
    next_morning = datetime(2026, 3, 11, 0, 30, tzinfo=NY)
    assert session_budget_check(
        budget, state, boundary=BOUNDARY, at=next_morning,
        equity=100_000.0).admitted
    assert state.day == "2026-03-11"


# --------------------------------------------------------------------------- #
# 6. The board: one declared order, one §12 code, no silent skip
# --------------------------------------------------------------------------- #
def test_veto_board_admits_a_clean_entry():
    decision = _board().check(_request(), BudgetState())
    assert decision.admitted
    assert decision.size_multiplier == pytest.approx(1.0)
    assert decision.blocker is None
    assert decision.input_dict().get("guards") == VETO_ORDER


def test_veto_board_refuses_undeclared_declarations():
    # The heat cap is a §13.9 blocked parameter: absent means refuse, not skip.
    assert _board(heat=None).check(
        _request(), BudgetState()).blocker == NT_DATA_MISSING
    # V35 undeclared.
    assert _board(boundary=None).check(
        _request(), BudgetState()).blocker == NT_VARIANT_UNDECLARED
    # I7 absent.
    assert _board().check(
        _request(calendar=None), BudgetState()).blocker == NT_DATA_MISSING
    # RK-12 response fork absent.
    assert _board(events=None).check(
        _request(calendar=EconomicCalendar(())),
        BudgetState()).blocker == NT_VARIANT_UNDECLARED
    # RK-13 threshold absent while a position is open.
    assert _board(correlation=None).check(
        _request(open_positions=(OpenPositionRisk("ETHUSDT", 100.0),)),
        BudgetState()).blocker == NT_DATA_MISSING


def test_veto_board_reports_one_code_in_the_declared_precedence():
    assert VETO_ORDER == ("budget", "event", "correlation", "heat")
    state = BudgetState()
    state.record_outcome(BOUNDARY, AT, -100.0, in_killzone=True)
    state.record_outcome(BOUNDARY, AT, -100.0, in_killzone=True)

    # The session latch and an over-heated candidate are both true; the
    # declared order reports exactly one code.
    decision = _board().check(
        _request(new_risk_money=90_000.0), state)
    assert decision.blocker == NT_SESSION_CLOSED
    assert isinstance(decision, VetoDecision)
    assert decision.admitted is False


def test_cap_fifty_carries_onto_the_plan_and_halves_its_size():
    plan = _plan()
    assert plan.blockers == ()
    calendar = EconomicCalendar.from_rows([{
        "at": AT + timedelta(hours=10), "name": "Retail Sales",
        "high_impact": True}])
    board = _board(events=EventVetoPolicy("cap_50"))
    decision = board.check(_request(calendar=calendar), BudgetState())
    assert decision.admitted
    assert decision.size_multiplier == pytest.approx(0.5)

    capped = apply_veto(plan, decision)
    assert capped.quantity == pytest.approx(plan.quantity * 0.5)
    assert capped.stop == plan.stop
    assert capped.targets == plan.targets
    assert capped.blockers == ()


def test_refused_veto_strips_the_plan_of_size_and_ladder():
    plan = _plan()
    assert plan.quantity > 0 and plan.targets
    hot = _board(heat=PortfolioHeatCap(0.001, source_id="desk/risk"))
    decision = hot.check(
        _request(new_risk_money=500.0), BudgetState())
    assert decision.blocker == NT_BUDGET_SPENT

    refused = apply_veto(plan, decision)
    assert refused.quantity == 0.0
    assert refused.targets == ()
    assert refused.target_levels == ()
    assert refused.blockers == (NT_BUDGET_SPENT,)


def test_plan_risk_money_matches_the_sizing_identity():
    plan = _plan()
    expected = plan.quantity * abs(100.0 - plan.stop) * plan.value_per_unit
    assert plan_risk_money(plan, 100.0) == pytest.approx(expected)
    assert risk_money(plan.quantity, 100.0, plan.stop,
                      plan.value_per_unit) == pytest.approx(expected)


# --------------------------------------------------------------------------- #
# 7. The run surface declares the veto inputs
# --------------------------------------------------------------------------- #
def test_settings_declares_the_veto_inputs(tmp_path):
    settings = Settings(symbols=["BTCUSDT"], intervals=["15m"], mode="demo",
                        data_dir=tmp_path)
    # The blocked/missing inputs stay unset: no default is shipped.
    assert settings.portfolio_heat_cap_pct is None
    assert settings.correlation_threshold is None
    assert settings.event_veto_response is None
    assert settings.budget_reset_boundary is None
    assert settings.budget_unit is None
    assert settings.budget_horizon is None

    assert settings.event_veto_window_hours == pytest.approx(48.0)
    assert 5.0 <= settings.event_veto_post_release_wait_minutes <= 10.0
    assert settings.max_daily_loss_pct == pytest.approx(
        DEFAULT_MAX_DAILY_LOSS_PCT)

    public = settings.public_dict()
    assert public["portfolio_heat_cap_pct"] is None
    assert public["event_veto_window_hours"] == pytest.approx(48.0)
    assert public["budget_reset_boundary"] is None

    with pytest.raises(ValueError):
        Settings(symbols=["BTCUSDT"], intervals=["15m"], mode="demo",
                 data_dir=tmp_path, portfolio_heat_cap_pct=1.5)
    with pytest.raises(ValueError):
        Settings(symbols=["BTCUSDT"], intervals=["15m"], mode="demo",
                 data_dir=tmp_path, correlation_threshold=0.0)
    with pytest.raises(ValueError):
        Settings(symbols=["BTCUSDT"], intervals=["15m"], mode="demo",
                 data_dir=tmp_path, event_veto_response="ignore")
    with pytest.raises(ValueError):
        Settings(symbols=["BTCUSDT"], intervals=["15m"], mode="demo",
                 data_dir=tmp_path, event_veto_post_release_wait_minutes=4)
    with pytest.raises(ValueError):
        Settings(symbols=["BTCUSDT"], intervals=["15m"], mode="demo",
                 data_dir=tmp_path, budget_reset_boundary="UTC midnight")
    with pytest.raises(ValueError):
        Settings(symbols=["BTCUSDT"], intervals=["15m"], mode="demo",
                 data_dir=tmp_path, budget_unit="trades")
    with pytest.raises(ValueError):
        Settings(symbols=["BTCUSDT"], intervals=["15m"], mode="demo",
                 data_dir=tmp_path, budget_horizon="week")


def test_settings_reads_the_veto_declarations_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("PORTFOLIO_HEAT_CAP_PCT", "0.025")
    monkeypatch.setenv("CORRELATION_THRESHOLD", "0.85")
    monkeypatch.setenv("EVENT_VETO_RESPONSE", "CAP_50")
    monkeypatch.setenv("EVENT_VETO_WINDOW_HOURS", "36")
    monkeypatch.setenv("EVENT_VETO_POST_RELEASE_WAIT_MINUTES", "7")
    monkeypatch.setenv("BUDGET_RESET_BOUNDARY", "UTC date")
    monkeypatch.setenv("BUDGET_UNIT", "attempts")
    monkeypatch.setenv("BUDGET_HORIZON", "session")

    settings = Settings.from_env(data_dir=tmp_path)
    assert settings.portfolio_heat_cap_pct == pytest.approx(0.025)
    assert settings.correlation_threshold == pytest.approx(0.85)
    assert settings.event_veto_response == "cap_50"
    assert settings.event_veto_window_hours == pytest.approx(36.0)
    assert settings.event_veto_post_release_wait_minutes == pytest.approx(7.0)
    assert settings.budget_reset_boundary == "UTC date"
    assert settings.budget_unit == "attempts"
    assert settings.budget_horizon == "session"


def test_veto_codes_are_section_12_refusal_codes():
    for code in (NT_BUDGET_SPENT, NT_CORRELATION, NT_DATA_MISSING,
                 NT_EVENT_VETO, NT_SESSION_CLOSED, NT_VARIANT_UNDECLARED):
        assert code in SPEC_REFUSAL_CODES


def test_veto_register_labels_every_value_and_names_the_missing_ones():
    assert CONSECUTIVE_LOSS_LIMIT == 2
    assert EVENT_VETO_WINDOW_HOURS == pytest.approx(48.0)
    assert EVENT_VETO_CAP == pytest.approx(0.5)
    assert 5.0 <= EVENT_VETO_POST_RELEASE_WAIT_MINUTES <= 10.0
    assert DEFAULT_MAX_DAILY_LOSS_PCT == pytest.approx(0.03)
    assert EVENT_VETO_RESPONSES == ("defer", "cap_50")

    assert VETO_REGISTER["max_daily_loss_pct"].label == "repo"
    assert VETO_REGISTER["consecutive_loss_limit"].label == "corpus"
    assert VETO_REGISTER["event_veto_window_hours"].label == "corpus (single)"
    for entry in VETO_REGISTER.values():
        assert entry.source.strip()

    # The two blocked parameters are named, not filled.
    assert set(MISSING_VETO_INPUTS) == {
        "portfolio_heat_cap", "correlation_threshold"}
    assert "portfolio_heat_cap" not in VETO_REGISTER
    assert "correlation_threshold" not in VETO_REGISTER
