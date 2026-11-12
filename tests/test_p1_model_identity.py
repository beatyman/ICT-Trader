"""P1-11 -- model and period identity.

``CODE_MAPPING`` P1-11 asks for three declarations the checkout did not hold:

* the **PO3 six-field period contract** -- instrument, candle length, session
  definition, timezone, opening price, ending time -- filled before any phase
  label (GW-02), with one opening convention and never a mix (GW-03);
* the **narrative order** ``bias_first`` / ``draw_first`` (SYSTEM_SPEC §3.4),
  where the two orderings may not be mixed and both still enter ``B1`` before
  ``D1``;
* one **execution model** out of the ten families of §8.4 (RE:EX-18).  The
  families are non-equivalent and are never averaged.

§14.2 has no ``V##`` row for any of the three, so each is a declared run input
with no default.  The tests therefore pin: an incomplete period refuses before a
label, an undeclared or invented model refuses with ``NT_VARIANT_UNDECLARED``,
and the legacy (undeclared) setup path claims no family at all instead of
silently presenting the CISD retrace as one of the ten.
"""
from __future__ import annotations

import inspect
from datetime import time as dtime
from types import SimpleNamespace

import pandas as pd
import pytest

from ict_trader.cisd import CISD
from ict_trader.concepts import build_zigzag, find_pivots
from ict_trader.context import (
    EXECUTION_MODEL_FAMILIES,
    EXECUTION_MODEL_FAMILY_SPECS,
    MODEL_IDENTITY_FIELDS,
    NARRATIVE_ORDER_OPTIONS,
    NARRATIVE_STATE_ENTRY_ORDER,
    PO3_DAILY_DEFAULT_OPEN,
    PO3_DAILY_DEFAULT_ZONE,
    PO3_PERIOD_FIELDS,
    PO3_PHASE_ORDER,
    ModelIdentity,
    ModelIdentityUndeclared,
    NarrativeOrder,
    PO3PeriodContract,
    PO3PeriodIncomplete,
    UnknownExecutionModel,
    declare_day_boundary,
    declare_execution_model,
    declare_model_identity,
    declare_narrative_order,
    declare_po3_period,
    execution_model_spec,
    label_po3_phase,
    parse_execution_model_id,
    resolve_model_identity,
)
from ict_trader.setup import SetupConfig, build_setup
from ict_trader.strategy import StrategyConfig, simulate_cisd
from ict_trader.variants import RefusalCode, VariantConfig

NY = "America/New_York"


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #
def _period_fields(**overrides: object) -> dict[str, object]:
    fields: dict[str, object] = {
        "instrument": "EURUSD",
        "candle_length": "15m",
        "session_definition": "London killzone 02:00-05:00 NY",
        "timezone": NY,
        "opening_price": 1.0850,
        "ending_time": "05:00",
    }
    fields.update(overrides)
    return fields


def _period(**overrides: object) -> PO3PeriodContract:
    return declare_po3_period(_period_fields(**overrides))


def _frame(path: tuple[tuple[float, float, float], ...]) -> pd.DataFrame:
    """One base bar per day at 00:00 New York; each tuple is (H, L, C)."""
    rows = []
    for index, (high, low, close) in enumerate(path):
        stamp = pd.Timestamp(
            f"2026-01-{index + 1:02d} 00:00", tz=NY).tz_convert("UTC")
        rows.append((stamp, close, high, low, close, 1.0))
    return pd.DataFrame(rows, columns=["date", "open", "high", "low", "close",
                                       "volume"])


def _three_days() -> pd.DataFrame:
    return _frame(((100.0, 90.0, 95.0),
                   (100.0, 90.0, 91.0),
                   (96.0, 90.0, 95.0)))


def _result(df: pd.DataFrame, t: int, direction: str = "bear") -> SimpleNamespace:
    level = float(df["close"].iloc[t])
    sl = level - 1.0 if direction == "bull" else level + 1.0
    return SimpleNamespace(
        swings=build_zigzag(find_pivots(df, 5, 1)),
        cisds=[CISD(
            direction=direction, level=level, extreme=level,
            body_extreme=level, start=max(0, t - 5), end=t, sl=sl, target=None,
            grade=3, checks={"sweep": False, "pda": False})],
        fvgs=[], order_blocks=[], pd_arrays=[], sweeps=[], key_levels=[],
    )


def _permissive_cfg(**overrides: object) -> SetupConfig:
    values = dict(
        market="other", stack="1d_15m", allow_short=True, min_grade=0,
        min_confluence=0, require_sweep=False, origin_filter="any",
        htf_fvg=False, target_mode="fixed", rr=2.0, min_target_rr=0.0,
        max_inducements=-1, regime_features=False, min_disp_body_ratio=0.0,
        min_disp_atr=0.0, min_disp_multiple=0.0, min_setup_score=0.0,
        require_pda_array=False, require_first_fvg=False,
        require_fresh_fvg=False, pd_gate=False, ote_gate=False,
        session_gate="none", quality_profile="standard", bias_primitive="none",
        variant_config=VariantConfig({"V04": "NY midnight"}),
    )
    values.update(overrides)
    return SetupConfig(**values)


def _identity_cfg(**overrides: object) -> SetupConfig:
    values = dict(
        po3_period=_period(),
        narrative_order="bias_first",
        execution_model_id="m2022",
    )
    values.update(overrides)
    return _permissive_cfg(**values)


# --------------------------------------------------------------------------- #
# 1. GW-02: the six-field period contract
# --------------------------------------------------------------------------- #
def test_the_period_contract_has_exactly_the_six_gw02_fields():
    assert PO3_PERIOD_FIELDS == (
        "instrument", "candle_length", "session_definition", "timezone",
        "opening_price", "ending_time")
    contract = _period()
    public = contract.to_public()
    assert set(public) == set(PO3_PERIOD_FIELDS)
    assert len(public) == 6
    assert public["ending_time"] == "05:00"
    assert public["opening_price"] == 1.0850
    assert "GW-02" in contract.source


@pytest.mark.parametrize("field", PO3_PERIOD_FIELDS)
def test_each_missing_period_field_refuses_before_any_label(field: str):
    fields = _period_fields()
    fields.pop(field)
    with pytest.raises(PO3PeriodIncomplete) as raised:
        declare_po3_period(fields)
    assert raised.value.missing_fields == (field,)
    assert raised.value.refusal.code is RefusalCode.NT_VARIANT_UNDECLARED
    assert raised.value.refusal.variant_id == "po3_period_contract"


def test_an_empty_period_contract_names_all_six_fields():
    with pytest.raises(PO3PeriodIncomplete) as raised:
        declare_po3_period()
    assert raised.value.missing_fields == PO3_PERIOD_FIELDS


@pytest.mark.parametrize("value", ["", "   ", None])
def test_a_blank_period_field_is_not_a_declaration(value: object):
    fields = _period_fields()
    fields["instrument"] = value
    with pytest.raises(PO3PeriodIncomplete) as raised:
        declare_po3_period(fields)
    assert "instrument" in raised.value.missing_fields


def test_a_seventh_period_field_refuses_rather_than_being_stored():
    fields = _period_fields()
    fields["bar_count"] = 20
    with pytest.raises(ValueError, match="no seventh field"):
        declare_po3_period(fields)


@pytest.mark.parametrize("zone", ["UTC+5", "GMT-4", "Mars/Olympus"])
def test_the_period_timezone_must_be_a_named_iana_zone(zone: str):
    # GW-03/R47: a fixed UTC offset breaks the midnight boundary under DST.
    with pytest.raises(ValueError, match="named IANA zone"):
        _period(timezone=zone)


@pytest.mark.parametrize("price", [0.0, -1.0, float("nan"), float("inf"),
                                   "not-a-price", True])
def test_the_opening_price_must_be_a_finite_positive_number(price: object):
    with pytest.raises(ValueError, match="opening_price"):
        _period(opening_price=price)


def test_the_ending_time_is_a_minute_resolution_clock():
    assert _period(ending_time="23:59").ending_time == dtime(23, 59)
    with pytest.raises(ValueError, match="ending_time"):
        _period(ending_time="25:00")
    with pytest.raises(ValueError, match="ending_time"):
        _period(ending_time="05:00:30")


def test_the_period_contract_has_no_defaulted_field():
    signature = inspect.signature(PO3PeriodContract)
    assert signature.parameters
    assert all(parameter.default is inspect.Parameter.empty
               for parameter in signature.parameters.values())


# --------------------------------------------------------------------------- #
# 2. GW-03: one opening convention, never mixed
# --------------------------------------------------------------------------- #
def test_the_period_and_the_day_boundary_share_one_opening_zone():
    boundary = declare_day_boundary("NY midnight")
    assert _period().assert_opening_convention(boundary) is not None
    with pytest.raises(ValueError, match="never mixed"):
        _period(timezone="Europe/London").assert_opening_convention(boundary)


def test_gw03_daily_default_is_a_labelled_constant_not_a_default():
    # The daily default is documented (00:00 America/New_York) but no object
    # applies it implicitly: the contract still needs all six fields supplied.
    assert PO3_DAILY_DEFAULT_ZONE == "America/New_York"
    assert PO3_DAILY_DEFAULT_OPEN == dtime(0, 0)
    boundary = declare_day_boundary("NY midnight")
    assert boundary.zone == PO3_DAILY_DEFAULT_ZONE
    assert boundary.anchor_time == PO3_DAILY_DEFAULT_OPEN


# --------------------------------------------------------------------------- #
# 3. GW-02/GW-05: no label before the contract is complete
# --------------------------------------------------------------------------- #
def test_a_phase_cannot_be_labelled_without_the_period_contract():
    with pytest.raises(PO3PeriodIncomplete) as raised:
        label_po3_phase("manipulation", None)
    assert raised.value.missing_fields == PO3_PERIOD_FIELDS

    fields = _period_fields()
    fields.pop("session_definition")
    with pytest.raises(PO3PeriodIncomplete) as partial:
        label_po3_phase("manipulation", fields)
    assert partial.value.missing_fields == ("session_definition",)


def test_the_phase_label_keeps_the_fixed_amd_order():
    contract = _period()
    labels = [label_po3_phase(phase, contract) for phase in PO3_PHASE_ORDER]
    assert [label.phase.value for label in labels] == list(PO3_PHASE_ORDER)
    assert [label.to_public()["phase_index"] for label in labels] == [1, 2, 3]
    with pytest.raises(ValueError, match="GW-05"):
        label_po3_phase("reversal", contract)


# --------------------------------------------------------------------------- #
# 4. §3.4: narrative order
# --------------------------------------------------------------------------- #
def test_narrative_order_is_exactly_two_complete_orderings():
    assert NARRATIVE_ORDER_OPTIONS == ("bias_first", "draw_first")
    assert tuple(order.value for order in NarrativeOrder) == \
        NARRATIVE_ORDER_OPTIONS


def test_bias_first_authors_the_bias_before_the_draw():
    spec = declare_narrative_order("bias_first")
    assert spec.order is NarrativeOrder.BIAS_FIRST
    assert (spec.authored_first, spec.authored_second) == ("bias", "draw")


def test_draw_first_authors_the_draw_before_the_bias():
    spec = declare_narrative_order("draw_first")
    assert spec.order is NarrativeOrder.DRAW_FIRST
    assert (spec.authored_first, spec.authored_second) == ("draw", "bias")


def test_both_orderings_enter_b1_before_d1():
    # §3.4: the ordering changes which field is authored first, not the graph.
    assert NARRATIVE_STATE_ENTRY_ORDER == ("B1", "D1")
    for value in NARRATIVE_ORDER_OPTIONS:
        spec = declare_narrative_order(value)
        assert spec.state_entry_order == ("B1", "D1")
        assert spec.to_public()["state_entry_order"] == ["B1", "D1"]


@pytest.mark.parametrize("value", [None, "", "   "])
def test_an_undeclared_narrative_order_refuses(value: object):
    with pytest.raises(ModelIdentityUndeclared) as raised:
        declare_narrative_order(value)
    assert raised.value.refusal.code is RefusalCode.NT_VARIANT_UNDECLARED
    assert raised.value.field == "narrative_order"


@pytest.mark.parametrize("value", ["mixed", "bias_first,draw_first",
                                   "draw_then_bias", "both"])
def test_the_two_orderings_may_not_be_mixed(value: str):
    with pytest.raises(ValueError, match="may not be mixed"):
        declare_narrative_order(value)


def test_narrative_order_has_no_default():
    signature = inspect.signature(declare_narrative_order)
    assert signature.parameters["value"].default is inspect.Parameter.empty


# --------------------------------------------------------------------------- #
# 5. RE:EX-18: one execution model out of >= 10 declared families
# --------------------------------------------------------------------------- #
def test_at_least_ten_non_equivalent_families_are_declared():
    assert len(EXECUTION_MODEL_FAMILIES) >= 10
    assert tuple(EXECUTION_MODEL_FAMILY_SPECS) == EXECUTION_MODEL_FAMILIES
    assert len(set(EXECUTION_MODEL_FAMILIES)) == len(EXECUTION_MODEL_FAMILIES)


def test_the_families_are_the_spec_8_4_rows():
    assert EXECUTION_MODEL_FAMILIES == (
        "m2022", "silver_bullet", "judas_london", "ny_am", "macro_reprice",
        "ote_confluence", "ifvg_inversion", "fvg_first_limit", "turtle_soup",
        "po3_amd")
    names = {spec.name for spec in EXECUTION_MODEL_FAMILY_SPECS.values()}
    assert names == {
        "M2022", "Silver Bullet", "Judas / London", "NY AM", "Macro reprice",
        "OTE confluence", "IFVG inversion", "FVG-first limit", "Turtle Soup",
        "PO3 / AMD"}


@pytest.mark.parametrize("model_id", EXECUTION_MODEL_FAMILIES)
def test_every_family_declares_window_chain_entry_stop_and_source(
        model_id: str):
    spec = execution_model_spec(model_id)
    public = spec.to_public()
    assert public["model_id"] == model_id
    for field in ("name", "window", "required_chain", "entry", "stop",
                  "source"):
        assert public[field].strip(), (model_id, field)


def test_the_model_id_selects_exactly_one_family():
    assert declare_execution_model("M2022").model_id == "m2022"
    assert declare_execution_model("silver-bullet").model_id == "silver_bullet"
    assert declare_execution_model("po3_amd").name == "PO3 / AMD"


@pytest.mark.parametrize("value", [None, "", "   "])
def test_an_undeclared_execution_model_refuses(value: object):
    with pytest.raises(ModelIdentityUndeclared) as raised:
        parse_execution_model_id(value)
    assert raised.value.refusal.code is RefusalCode.NT_VARIANT_UNDECLARED
    assert raised.value.field == "execution_model_id"


@pytest.mark.parametrize("value", ["cisd_retrace", "made_up_model",
                                   "m2022+silver_bullet"])
def test_an_invented_execution_model_is_never_resolved(value: str):
    with pytest.raises(UnknownExecutionModel) as raised:
        declare_execution_model(value)
    assert raised.value.refusal.code is RefusalCode.NT_VARIANT_UNDECLARED
    assert "not a declared execution-model family" in str(raised.value)


def test_the_execution_model_parser_has_no_default():
    signature = inspect.signature(parse_execution_model_id)
    assert signature.parameters["value"].default is inspect.Parameter.empty


# --------------------------------------------------------------------------- #
# 6. the identity triple
# --------------------------------------------------------------------------- #
def test_the_identity_requires_all_three_parts():
    empty = ModelIdentity()
    assert empty.missing_fields == MODEL_IDENTITY_FIELDS
    assert empty.complete is False
    with pytest.raises(ModelIdentityUndeclared) as raised:
        empty.require()
    assert raised.value.refusal.code is RefusalCode.NT_VARIANT_UNDECLARED
    assert empty.refusal() is not None


def test_a_piecemeal_identity_refuses_instead_of_filling_the_gaps():
    with pytest.raises(ModelIdentityUndeclared) as period_only:
        resolve_model_identity(period=_period())
    assert period_only.value.field == "narrative_order"
    with pytest.raises(ModelIdentityUndeclared):
        resolve_model_identity(narrative_order="bias_first")
    with pytest.raises(ModelIdentityUndeclared):
        resolve_model_identity(execution_model_id="m2022")


def test_declare_model_identity_without_any_part_refuses():
    with pytest.raises(ModelIdentityUndeclared) as raised:
        declare_model_identity()
    assert raised.value.field == "po3_period"
    assert raised.value.refusal.code is RefusalCode.NT_VARIANT_UNDECLARED


def test_a_complete_identity_is_one_object_with_three_parts():
    identity = declare_model_identity(
        period=_period(), narrative_order="draw_first",
        execution_model_id="po3_amd")
    assert identity.complete is True
    assert identity.missing_fields == ()
    public = identity.to_public()
    assert public["declared"] is True
    assert public["execution_model_id"] == "po3_amd"
    assert public["narrative_order"]["order"] == "draw_first"
    assert set(public["period_contract"]) == set(PO3_PERIOD_FIELDS)


def test_the_identity_refuses_two_opening_conventions():
    boundary = declare_day_boundary("NY midnight")
    with pytest.raises(ValueError, match="never mixed"):
        declare_model_identity(
            period=_period(timezone="Europe/London"),
            narrative_order="bias_first",
            execution_model_id="m2022",
            boundary=boundary)


# --------------------------------------------------------------------------- #
# 7. setup wiring: evidence, normalization, gate, hash
# --------------------------------------------------------------------------- #
def test_setup_config_normalizes_the_declared_identity():
    cfg = _identity_cfg(execution_model_id="M2022")
    assert isinstance(cfg.po3_period, PO3PeriodContract)
    assert cfg.narrative_order == "bias_first"
    assert cfg.execution_model_id == "m2022"


def test_setup_config_refuses_a_piecemeal_identity():
    with pytest.raises(ModelIdentityUndeclared):
        _permissive_cfg(po3_period=_period())
    with pytest.raises(ModelIdentityUndeclared):
        _permissive_cfg(narrative_order="draw_first")
    with pytest.raises(ModelIdentityUndeclared):
        _permissive_cfg(execution_model_id="m2022")


def test_setup_config_refuses_an_invented_execution_model():
    with pytest.raises(UnknownExecutionModel):
        _permissive_cfg(po3_period=_period(), narrative_order="bias_first",
                        execution_model_id="cisd_retrace")


def test_setup_config_gate_refuses_an_undeclared_identity():
    with pytest.raises(ModelIdentityUndeclared) as raised:
        _permissive_cfg(model_identity_gate=True)
    assert raised.value.refusal.code is RefusalCode.NT_VARIANT_UNDECLARED


def test_build_setup_emits_the_declared_identity():
    df = _three_days()
    result = _result(df, 2, "bear")
    signals = build_setup(df, result, _identity_cfg())
    assert len(signals) == 1
    parts = signals[0].quality_parts
    assert parts["model_identity_declared"] is True
    assert parts["model_identity_gate"] is False
    assert parts["model_identity_missing_fields"] == []
    assert parts["model_identity"]["declared"] is True
    assert parts["execution_model_id"] == "m2022"
    assert parts["execution_model_family"] == "M2022"
    assert parts["execution_model_window"] == \
        "per V13 (the declared window table)"
    assert parts["narrative_order"] == "bias_first"
    assert parts["narrative_order_authored_first"] == "bias"
    assert parts["narrative_order_state_entry"] == ["B1", "D1"]
    assert set(parts["po3_period_contract"]) == set(PO3_PERIOD_FIELDS)
    assert parts["po3_period_complete"] is True


def test_build_setup_legacy_path_claims_no_model():
    df = _three_days()
    result = _result(df, 2, "bear")
    signals = build_setup(df, result, _permissive_cfg())
    assert len(signals) == 1
    parts = signals[0].quality_parts
    # The legacy CISD path is *not* silently declared as M2022 or any family.
    assert parts["model_identity_declared"] is False
    assert parts["execution_model_id"] is None
    assert parts["narrative_order"] is None
    assert parts["po3_period_contract"] is None
    assert parts["model_identity_missing_fields"] == list(MODEL_IDENTITY_FIELDS)


def test_build_setup_with_the_identity_gate_admits_a_complete_identity():
    df = _three_days()
    result = _result(df, 2, "bear")
    signals = build_setup(df, result,
                          _identity_cfg(model_identity_gate=True))
    assert len(signals) == 1
    assert signals[0].quality_parts["model_identity_gate"] is True


def test_build_setup_refuses_a_mismatched_opening_convention():
    df = _three_days()
    result = _result(df, 2, "bear")
    cfg = _identity_cfg(po3_period=_period(timezone="Europe/London"))
    with pytest.raises(ValueError, match="never mixed"):
        build_setup(df, result, cfg)


def test_the_config_hash_covers_every_identity_part():
    base = _permissive_cfg()
    identity = _identity_cfg()
    assert base.config_hash != identity.config_hash
    assert identity.config_hash != _identity_cfg(
        execution_model_id="silver_bullet").config_hash
    assert identity.config_hash != _identity_cfg(
        narrative_order="draw_first").config_hash
    assert identity.config_hash != _identity_cfg(
        po3_period=_period(session_definition="London open 03:00 NY")
    ).config_hash


# --------------------------------------------------------------------------- #
# 8. strategy wiring: same identity, no inferred family
# --------------------------------------------------------------------------- #
def test_strategy_config_normalizes_and_hashes_the_identity():
    cfg = StrategyConfig(po3_period=_period(), narrative_order="draw_first",
                         execution_model_id="po3_amd")
    assert isinstance(cfg.po3_period, PO3PeriodContract)
    assert cfg.narrative_order == "draw_first"
    assert cfg.execution_model_id == "po3_amd"
    assert cfg.config_hash != StrategyConfig().config_hash


def test_strategy_config_refuses_incomplete_or_invented_identity():
    with pytest.raises(ModelIdentityUndeclared):
        StrategyConfig(po3_period=_period())
    with pytest.raises(UnknownExecutionModel):
        StrategyConfig(po3_period=_period(), narrative_order="bias_first",
                       execution_model_id="cisd_retrace")
    with pytest.raises(ModelIdentityUndeclared):
        StrategyConfig(model_identity_gate=True)


def _sim_frame() -> pd.DataFrame:
    rows = [
        ("00:00", 100.0, 101.0, 99.0, 100.0),
        ("00:15", 100.0, 102.0, 99.5, 101.0),
        ("00:30", 103.0, 104.0, 102.5, 103.5),
        ("00:45", 103.5, 105.0, 103.0, 104.0),
        ("01:00", 104.0, 106.0, 103.5, 105.0),
        ("01:15", 105.0, 107.0, 104.5, 106.0),
    ]
    data = []
    for clock, open_, high, low, close in rows:
        stamp = pd.Timestamp(f"2026-01-05 {clock}", tz=NY).tz_convert("UTC")
        data.append((stamp, open_, high, low, close, 1.0))
    return pd.DataFrame(data, columns=["date", "open", "high", "low", "close",
                                       "volume"])


def _sim_result(df: pd.DataFrame) -> SimpleNamespace:
    cisd = CISD(
        direction="bull", level=101.0, extreme=101.0, body_extreme=101.0,
        start=0, end=1, sl=99.0, target=120.0, grade=5,
        checks={"sweep": True, "pda": True, "volume": True, "session": True,
                "targets": True, "smt": True},
    )
    return SimpleNamespace(cisds=[cisd], htf_cisds=[])


def test_a_simulated_trade_records_the_declared_identity():
    df = _sim_frame()
    cfg = StrategyConfig(
        po3_period=_period(), narrative_order="draw_first",
        execution_model_id="po3_amd", min_grade=0, min_confluence=0,
        commission_bps=0.0, slippage_bps=0.0)
    result = simulate_cisd(df, _sim_result(df), cfg, "TEST")
    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.execution_model_id == "po3_amd"
    assert trade.narrative_order == "draw_first"


def test_a_simulated_trade_without_a_identity_claims_no_family():
    df = _sim_frame()
    cfg = StrategyConfig(min_grade=0, min_confluence=0,
                         commission_bps=0.0, slippage_bps=0.0)
    result = simulate_cisd(df, _sim_result(df), cfg, "TEST")
    assert len(result.trades) == 1
    assert result.trades[0].execution_model_id == ""
    assert result.trades[0].narrative_order == ""
