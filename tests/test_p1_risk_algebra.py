"""P1-13 - risk algebra objects: sizing, declarations, RR/expectancy, targets.

``CODE_MAPPING.md`` P1-13 asks for six objects that were implicit or absent:

* ``value_per_unit`` (I3) and RK-14 lot-step rounding;
* declared ``RR_MODE`` (V28), ``STOP_POLICY`` (V29) and a risk band (V30) with
  its ``source_id``;
* weighted RR and expectancy arithmetic;
* a ``(price, known_at)`` target payload (TG-03/CB-07);
* a declared minimum-distance floor (TG-11).

These tests pin the declarations and the fail-closed behaviour.  They are red
on the pre-P1-13 checkout because the objects do not exist there.
"""
from __future__ import annotations

import pytest

from ict_trader.cisd import STOP_POLICY as CISD_STOP_POLICY
from ict_trader.realtime.analysis.risk import (
    DEFAULT_MIN_DISTANCE_FLOOR,
    DEFAULT_RISK_BAND,
    DEFAULT_VALUE_PER_UNIT,
    NET_RR_BLOCKER,
    NT_DATA_MISSING,
    NT_RR_BELOW_FLOOR,
    NT_SIZE_UNSOLVABLE,
    NT_TARGET_TOO_NEAR,
    NT_VARIANT_UNDECLARED,
    RISK_BAND_ID,
    RR_MODE,
    RR_MODE_ID,
    STOP_POLICY,
    STOP_POLICY_DECLARATION,
    STOP_POLICY_ID,
    TargetLevel,
    breakeven_win_rate,
    build_risk_plan,
    expectancy,
    position_size,
    round_to_lot_step,
    weighted_rr,
    weighted_rr_check,
    weighted_rr_check_from_legs,
)
from ict_trader.realtime.config import Settings
from ict_trader.strategy import (
    DEFAULT_LOT_STEP,
    DEFAULT_RISK_PCT,
    DEFAULT_RR_MODE,
    MIN_DISTANCE_FORMS,
    RISK_BANDS,
    RR_MODE_FLOORS,
    StrategyConfig,
    rr_floor,
    validate_risk_pct,
)
from ict_trader.sweeps import KeyLevel


def _plan(pools, **overrides):
    values = dict(
        direction="long",
        entry=100.0,
        atr=2.0,
        sweep={"level": 98.0, "extreme": 98.0},
        structure=None,
    )
    values.update(overrides)
    return build_risk_plan(**values, pools=pools)


# --------------------------------------------------------------------------- #
# 1. value_per_unit (I3) and lot-step rounding (RK-14)
# --------------------------------------------------------------------------- #
def test_value_per_unit_solves_the_corpus_sizing_formula():
    # 100_000 * 1% / (2.5 * 1.0) = 400 units
    assert position_size(100_000.0, 0.01, 100.0, 97.5) == pytest.approx(400.0)
    # A value per unit of 2 doubles the notional per unit, halving the units.
    assert position_size(100_000.0, 0.01, 100.0, 97.5,
                         value_per_unit=2.0) == pytest.approx(200.0)
    with pytest.raises(ValueError):
        position_size(100_000.0, 0.01, 100.0, 97.5, value_per_unit=0.0)


def test_lot_step_rounds_down_and_never_exceeds_the_risk_budget():
    assert round_to_lot_step(400.0, 3.0) == pytest.approx(399.0)
    assert round_to_lot_step(400.0, 0.0) == pytest.approx(400.0)
    with pytest.raises(ValueError):
        round_to_lot_step(400.0, -1.0)
    with pytest.raises(ValueError):
        round_to_lot_step(400.0, 3.0, policy="nearest")

    plan = _plan([108.0], equity=100_000.0, risk_pct=0.01,
                 value_per_unit=2.0, lot_step=3.0)
    assert plan.blockers == ()
    # 100_000 * 1% / (2.5 * 2) = 200 -> floored to 198 by the 3-unit step.
    assert plan.quantity == pytest.approx(198.0)
    budget = 100_000.0 * 0.01
    assert plan.quantity * abs(100.0 - plan.stop) * plan.value_per_unit <= budget


def test_size_unsolvable_below_the_lot_step_refuses():
    plan = _plan([108.0], equity=100.0, risk_pct=0.005,
                 value_per_unit=1.0, lot_step=10.0)
    assert plan.blockers == (NT_SIZE_UNSOLVABLE,)
    assert plan.quantity == 0.0
    assert plan.targets == ()


# --------------------------------------------------------------------------- #
# 2. declared RR_MODE (V28), STOP_POLICY (V29) and risk band (V30)
# --------------------------------------------------------------------------- #
def test_rr_mode_is_the_declared_v28_register_entry():
    assert RR_MODE_ID == "V28"
    assert RR_MODE == DEFAULT_RR_MODE == "filter_2_1"
    assert tuple(RR_MODE_FLOORS) == (
        "floor_3_1", "floor_session_1_5", "filter_2_1", "none")
    assert rr_floor("floor_3_1") == pytest.approx(3.0)
    assert rr_floor("filter_2_1") == pytest.approx(2.0)
    with pytest.raises(ValueError):
        rr_floor("floor_4_1")
    with pytest.raises(ValueError):
        _plan([108.0], rr_mode="floor_4_1")


def test_rr_mode_gates_admission_and_none_makes_the_ratio_an_output():
    # 2.32 gross R passes the declared repo net floor (2.0) but not floor_3_1.
    blocked = _plan([106.0], rr_mode="floor_3_1")
    assert blocked.blockers == (NT_RR_BELOW_FLOOR,)
    assert blocked.rr_mode == "floor_3_1"
    assert blocked.quantity == 0.0 and blocked.targets == ()

    # ``none`` is an output mode: even a 0.32 R target is sized, not gated.
    output_only = _plan([101.0], rr_mode="none")
    assert output_only.blockers == ()
    assert output_only.rr_mode == "none"
    assert output_only.quantity > 0
    assert output_only.gross_rr < 1.0


def test_risk_band_is_declared_v30_with_its_source_id():
    assert RISK_BAND_ID == "V30"
    assert DEFAULT_RISK_BAND.band_id in RISK_BANDS
    assert DEFAULT_RISK_BAND.source_id
    assert DEFAULT_RISK_BAND.contains(DEFAULT_RISK_PCT)
    assert validate_risk_pct(DEFAULT_RISK_PCT) == DEFAULT_RISK_PCT
    with pytest.raises(ValueError):
        validate_risk_pct(0.02)  # outside the declared 0.5-1.0% band

    # A sizing surface outside its declared band refuses before any price.
    out = _plan([108.0], risk_pct=0.02)
    assert out.blockers == (NT_VARIANT_UNDECLARED,)
    assert out.stop is None and out.quantity == 0.0


def test_stop_policy_is_the_declared_v29_object():
    assert STOP_POLICY_ID == "V29"
    assert STOP_POLICY is CISD_STOP_POLICY
    assert STOP_POLICY_DECLARATION.variant_id == "V29"
    assert STOP_POLICY_DECLARATION.source_id
    assert STOP_POLICY_DECLARATION.construction
    assert STOP_POLICY_DECLARATION.policy is STOP_POLICY
    assert STOP_POLICY.anchors[0] == "sweep_extreme"


def test_settings_declares_the_same_instrument_and_risk_algebra(tmp_path):
    settings = Settings(symbols=["BTCUSDT"], intervals=["15m"], mode="demo",
                        data_dir=tmp_path)
    assert settings.value_per_unit == DEFAULT_VALUE_PER_UNIT
    assert settings.lot_step == DEFAULT_LOT_STEP
    assert settings.risk_band == DEFAULT_RISK_BAND.band_id
    assert settings.min_distance_floor == DEFAULT_MIN_DISTANCE_FLOOR.mode
    public = settings.public_dict()
    assert public["risk_band"] == DEFAULT_RISK_BAND.band_id
    assert public["value_per_unit"] == DEFAULT_VALUE_PER_UNIT
    assert public["lot_step"] == DEFAULT_LOT_STEP

    with pytest.raises(ValueError):
        Settings(symbols=["BTCUSDT"], intervals=["15m"], mode="demo",
                 data_dir=tmp_path, risk_pct=0.02)
    with pytest.raises(ValueError):
        Settings(symbols=["BTCUSDT"], intervals=["15m"], mode="demo",
                 data_dir=tmp_path, risk_band="some_other_band")
    with pytest.raises(ValueError):
        Settings(symbols=["BTCUSDT"], intervals=["15m"], mode="demo",
                 data_dir=tmp_path, min_distance_floor="invented")

    # V30 also blocks an out-of-band risk percentage at the backtest surface.
    with pytest.raises(ValueError):
        StrategyConfig(risk_pct=0.02)


# --------------------------------------------------------------------------- #
# 3. weighted RR / expectancy
# --------------------------------------------------------------------------- #
def test_weighted_rr_and_expectancy_follow_the_corpus_arithmetic():
    assert weighted_rr([0.5, 0.5], [3.0, 1.0]) == pytest.approx(2.0)
    assert expectancy(0.4, 1.5) == pytest.approx(0.0, abs=1e-12)
    assert expectancy(0.5, 1.0) == pytest.approx(0.0)
    assert breakeven_win_rate(1.5) == pytest.approx(0.4)
    with pytest.raises(ValueError):
        expectancy(0.5, 1.0, loss_rate=0.6)
    with pytest.raises(ValueError):
        weighted_rr([0.5], [1.0, 2.0])


def test_weighted_rr_gate_refuses_without_a_hit_rate_assumption():
    missing = weighted_rr_check(1.5)
    assert missing.blocker == NT_DATA_MISSING
    assert missing.admitted is False
    assert missing.expectancy is None

    # The one attested 1.5:1 floor is negative expectancy below its 40%
    # break-even rate; the gate uses the derived rate, not an invented 40%.
    losing = weighted_rr_check(1.5, win_rate=0.35)
    assert losing.blocker == NT_RR_BELOW_FLOOR
    assert losing.expectancy < 0

    winning = weighted_rr_check_from_legs([0.5, 0.5], [3.0, 1.0],
                                          win_rate=0.5)
    assert winning.weighted_rr == pytest.approx(2.0)
    assert winning.blocker is None and winning.admitted is True


# --------------------------------------------------------------------------- #
# 4. (price, known_at) target payload (TG-03 / CB-07)
# --------------------------------------------------------------------------- #
def test_target_payload_preserves_price_and_known_at():
    level = TargetLevel(108.0, known_at=5, tf="15m")
    plan = _plan([level], signal_bar=5)
    assert plan.targets == (108.0,)
    assert plan.target_levels == (level,)
    assert plan.target_level is level
    assert plan.target_level.as_pair() == (108.0, 5)
    assert plan.target_level.to_public() == {
        "price": 108.0, "known_at": 5, "state": "intact", "tf": "15m"}


def test_target_known_after_the_signal_bar_is_not_admissible():
    future = TargetLevel(108.0, known_at=5)
    plan = _plan([future], signal_bar=4)
    assert plan.blockers == ("no_liquidity_target",)
    assert plan.targets == () and plan.target_levels == ()
    assert plan.quantity == 0.0
    assert future.is_known_at(4) is False
    assert future.is_admissible(5) is True


def test_swept_target_and_missing_known_at_refuse():
    swept = TargetLevel(108.0, known_at=1, state="swept")
    plan = _plan([swept], signal_bar=5)
    assert plan.blockers == ("no_liquidity_target",)

    # A level with no knowledge time cannot be proved admissible (CB-07).
    unknown = TargetLevel(108.0)
    plan = _plan([unknown], signal_bar=5)
    assert plan.blockers == (NT_DATA_MISSING,)
    assert plan.targets == ()


def test_key_level_is_coerced_into_the_payload():
    level = KeyLevel("EQH", 108.0, "H", known_at=5)
    plan = _plan([level], signal_bar=5)
    assert plan.target_levels[0].as_pair() == (108.0, 5)
    assert plan.target_levels[0].state == "intact"


# --------------------------------------------------------------------------- #
# 5. declared minimum-distance floor (TG-11)
# --------------------------------------------------------------------------- #
def test_minimum_distance_floor_is_declared_and_refuses_a_near_target():
    assert DEFAULT_MIN_DISTANCE_FLOOR.mode in MIN_DISTANCE_FORMS
    assert DEFAULT_MIN_DISTANCE_FLOOR.value == pytest.approx(3.0)
    assert DEFAULT_MIN_DISTANCE_FLOOR.source_id
    # 106.0 -> 105.8 with the 0.1 ATR pre-level offset: 2.32R gross.  It passes
    # the repo net floor (2.0) but is below the declared 3:1 distance floor.
    plan = _plan([106.0])
    assert plan.blockers == (NT_TARGET_TOO_NEAR,)
    assert plan.quantity == 0.0 and plan.targets == ()
    assert plan.target_levels == ()


def test_minimum_distance_floor_accepts_the_nearest_target_at_the_floor():
    plan = _plan([108.0])
    assert plan.blockers == ()
    assert plan.targets == (108.0,)
    # 107.8 reward / 2.5 risk = 3.12R: above the declared 3:1 floor.
    assert plan.target_levels[0].price == 108.0
    assert plan.quantity > 0


def test_minimum_distance_floor_uses_only_the_nearest_target():
    # A far pool would pass 3:1, but the nearest pool is below the floor; the
    # far pool may not rescue the plan (TG-07).
    plan = _plan([106.0, 130.0])
    assert plan.blockers == (NT_TARGET_TOO_NEAR,)
    assert plan.targets == ()


# --------------------------------------------------------------------------- #
# legacy compatibility: the net blocker keeps precedence
# --------------------------------------------------------------------------- #
def test_net_rr_blocker_keeps_precedence_over_the_distance_floor():
    plan = _plan([100.9])
    assert plan.blockers == (NET_RR_BLOCKER,)
    assert plan.gross_rr == 0.0 and plan.net_rr == 0.0
