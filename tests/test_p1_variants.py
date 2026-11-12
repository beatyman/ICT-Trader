"""P1-01 — variant register, refusal vocabulary and fail-closed reads.

The audit (``CODE_MAPPING.md`` P1-01) requires the ``V01``–``V35`` register of
``SYSTEM_SPEC.md`` §14.2 with one read path, a config hash and the §12 refusal
vocabulary as types.  The three properties this file locks are:

* **completeness** — every row exists with its name, options, default policy,
  source citation and the state it blocks when undeclared;
* **fail-closed refusal** — an undeclared fork returns/raises
  ``NT_VARIANT_UNDECLARED`` and never selects an option;
* **no silent default** — neither a ``DEFAULT-REPO`` row nor a blank/invalid
  declaration may fall back to a value; the config hash is deterministic and
  sensitive to the declared set only.

These tests are red on the pre-P1-01 checkout because ``ict_trader.variants``
and ``ict_trader.config`` do not exist there.
"""
from __future__ import annotations

import re

import pytest

from ict_trader.config import CONFIG_HASH_SCHEMA, config_hash
from ict_trader.setup import SetupConfig
from ict_trader.strategy import StrategyConfig
from ict_trader.variants import (
    DEFAULT_POLICIES,
    SPEC_REFUSAL_CODES,
    VARIANT_REGISTER,
    DefaultPolicy,
    Refusal,
    RefusalCode,
    UnknownVariant,
    VariantConfig,
    VariantSpec,
    VariantUndeclared,
    read_variant_spec,
)

#: §14.2 rows, verbatim.  Duplicated here so a silent edit to the register
#: cannot pass as a test change.
EXPECTED_NAMES = {
    "V01": "bias primitive",
    "V02": "bias timeframe set",
    "V03": "state vocabulary",
    "V04": "day boundary",
    "V05": "daily polarity",
    "V06": "weekly P/D anchor",
    "V07": "MSS family",
    "V08": "range anchor",
    "V09": "matrix roster + order",
    "V10": "IPDA variant",
    "V11": "gate strength",
    "V12": "sweep test",
    "V13": "window table + boundary",
    "V14": "window authority",
    "V15": "sweep direction vs bias",
    "V16": "FVG boundary convention",
    "V17": "body-close consequence",
    "V18": "BPR polarity + existence",
    "V19": "OB zone + gates",
    "V20": "breaker failure + geometry",
    "V21": "mitigation ontology",
    "V22": "IFVG removal",
    "V23": "rejection cardinality + bounds",
    "V24": "CISD reference + unit",
    "V25": "displacement detector",
    "V26": "array priority",
    "V27": "OTE bounds + anchor",
    "V28": "RR mode",
    "V29": "stop policy",
    "V30": "risk band",
    "V31": "ladder + BE",
    "V32": "target ranking",
    "V33": "holding class",
    "V34": "seasonal gate",
    "V35": "daily budget reset",
}

EXPECTED_SOURCES = {
    "V01": "[SN:C1]", "V02": "[SN:C2]", "V03": "[SN:C4]",
    "V04": "[SN:C6]", "V05": "[SN:C5]", "V06": "[SN:C9]",
    "V07": "[SN:C12]", "V08": "[SN:C18]", "V09": "[SN:C25/C26]",
    "V10": "[SN:C29/C30]", "V11": "[SN:C13]", "V12": "[LT:C8]",
    "V13": "[LT:C1/C10]", "V14": "[LT:C3; RE:C-14]", "V15": "[LT:C23]",
    "V16": "[PP:C1]", "V17": "[PP:C2]", "V18": "[PP:C3/C6]",
    "V19": "[PP:C4/C5]", "V20": "[PP:C7/C8]", "V21": "[PP:C11]",
    "V22": "[PP:C10]", "V23": "[PP:C21]", "V24": "[PP:C19/C20]",
    "V25": "[PP:C17]", "V26": "[PP:C24]", "V27": "[PP:C13/C14]",
    "V28": "[RE:C-01]", "V29": "[RE:C-03/C-04]", "V30": "[RE:C-02]",
    "V31": "[RE:C-05/C-06]", "V32": "[RE:C-16]", "V33": "[RE:C-22/C-23]",
    "V34": "[RE:C-29]", "V35": "[RE:C-26]",
}

EXPECTED_BLOCKS = {
    "V01": "B1", "V02": "B1", "V03": "B1", "V04": "G0", "V05": "D1",
    "V06": "weekly state", "V07": "MS1", "V08": "R1", "V09": "A1",
    "V10": "IPDA levels", "V11": "every gate", "V12": "SW1", "V13": "W1",
    "V14": "W1", "V15": "SW1→DP1", "V16": "FVG", "V17": "IFVG",
    "V18": "BPR", "V19": "OB", "V20": "breaker", "V21": "mitigation",
    "V22": "IFVG lifecycle", "V23": "rejection", "V24": "CISD",
    "V25": "DP1", "V26": "A1 ordering", "V27": "OTE model",
    "V28": "admission", "V29": "PS1", "V30": "sizing", "V31": "PS1",
    "V32": "target", "V33": "management", "V34": "D1 if enabled",
    "V35": "budget",
}

_P = DefaultPolicy
EXPECTED_POLICIES = {
    "V02": _P.MODE,
    "V13": _P.DEFAULT_REPO,
    "V14": _P.MODE,
    "V31": _P.MODE,
    "V33": _P.MODE,
}

EXPECTED_REFUSALS = (
    "NT_DATA_MISSING", "NT_VARIANT_UNDECLARED", "NT_INSTRUMENT_UNSUPPORTED",
    "NT_NO_BIAS", "NT_BIAS_GATE", "NT_BIAS_CONFLICT", "NT_OUTSIDE_DAY",
    "NT_NO_DRAW", "NT_DRAW_TAKEN", "NT_DRAW_TOO_CLOSE", "NT_RANGE_INVALID",
    "NT_RANGE_TIME_ANCHORED", "NT_WRONG_HALF", "NT_AT_EQ",
    "NT_AT_EQ_NO_ARRAY", "NT_MATRIX_EMPTY", "NT_ARRAY_WRONG_SIDE",
    "NT_OUT_OF_WINDOW", "NT_DEAD_ZONE", "NT_NO_RAID",
    "NT_LEVEL_BROKEN_NOT_SWEPT", "NT_NO_DISPLACEMENT", "NT_NO_SHIFT",
    "NT_SHIFT_UNCONFIRMED", "NT_CHASE", "NT_TARGET_TAKEN",
    "NT_TARGET_TOO_NEAR", "NT_WINDOW_EXPIRED", "NT_FILL_TIMEOUT",
    "NT_ONE_ATTEMPT_USED", "NT_RR_BELOW_FLOOR", "NT_SIZE_UNSOLVABLE",
    "NT_BUDGET_SPENT", "NT_EVENT_VETO", "NT_CORRELATION",
    "NT_SESSION_CLOSED", "NT_STOP_ANCHOR_UNRESOLVED",
    "NT_FAILED_MANIPULATION", "NT_VOID_OR_GAP_UNUSABLE",
)


# --------------------------------------------------------------------------- #
# 1. register completeness
# --------------------------------------------------------------------------- #
def test_register_is_exactly_v01_through_v35():
    expected_ids = tuple(f"V{i:02d}" for i in range(1, 36))
    assert tuple(VARIANT_REGISTER) == expected_ids
    assert len(VARIANT_REGISTER) == 35
    for variant_id in expected_ids:
        spec = read_variant_spec(variant_id)
        assert isinstance(spec, VariantSpec)
        assert spec.id == variant_id
        assert spec.name == EXPECTED_NAMES[variant_id]
        assert spec.source == EXPECTED_SOURCES[variant_id]
        assert spec.blocks == EXPECTED_BLOCKS[variant_id]
        assert spec.default_policy is EXPECTED_POLICIES.get(
            variant_id, DefaultPolicy.BLOCK)
        assert spec.options, f"{variant_id} has no options"
        assert all(isinstance(option, str) and option.strip()
                   for option in spec.options)
        assert len(set(spec.options)) == len(spec.options)


def test_spec_read_path_normalizes_ids_and_rejects_unknown_ones():
    assert read_variant_spec("v04") is VARIANT_REGISTER["V04"]
    assert read_variant_spec(" V35 ") is VARIANT_REGISTER["V35"]
    with pytest.raises(UnknownVariant):
        read_variant_spec("V36")
    with pytest.raises(UnknownVariant):
        read_variant_spec("V1")


def test_all_three_dispositions_are_represented_and_labelled():
    assert DEFAULT_POLICIES == ("BLOCK", "MODE", "DEFAULT-REPO")
    policies = {spec.default_policy for spec in VARIANT_REGISTER.values()}
    assert policies == set(DefaultPolicy)
    # The one documented DEFAULT-REPO row (§14.4) is the window boundary.
    window = VARIANT_REGISTER["V13"]
    assert window.default_policy is DefaultPolicy.DEFAULT_REPO
    assert window.repo_default == "half-open"
    assert window.repo_default in window.options
    # MODE rows ship both positions and name their mode field.
    for variant_id in ("V02", "V14", "V31", "V33"):
        assert VARIANT_REGISTER[variant_id].default_policy is DefaultPolicy.MODE
        assert VARIANT_REGISTER[variant_id].notes


def test_undeclared_block_targets_and_sources_are_present():
    for spec in VARIANT_REGISTER.values():
        assert spec.source.startswith("[") and spec.source.endswith("]")
        assert spec.blocks.strip()


@pytest.mark.parametrize("variant_id,expected", [
    ("V01", 5),    # five attested bias primitives
    ("V04", 4),    # four day boundaries
    ("V07", 6),    # F1..F6
    ("V09", 15),   # roster size 3/6/7/8/9 × three orderings
    ("V10", 9),    # three IPDA variants × three target orders
    ("V18", 6),    # three polarities × two existence rules
    ("V19", 9),    # three extents × three gate sets
    ("V20", 16),   # four failure tests × G1..G4
    ("V23", 4),    # two cardinalities × two bounds
    ("V24", 6),    # three references × two units
    ("V28", 4),    # four RR modes
])
def test_declared_option_sets_have_the_spec_cardinality(variant_id, expected):
    assert len(VARIANT_REGISTER[variant_id].options) == expected


# --------------------------------------------------------------------------- #
# 2. typed refusal vocabulary (§12)
# --------------------------------------------------------------------------- #
def test_refusal_vocabulary_is_the_complete_section_12_set():
    assert tuple(code.value for code in RefusalCode) == EXPECTED_REFUSALS
    assert SPEC_REFUSAL_CODES == EXPECTED_REFUSALS
    assert len(SPEC_REFUSAL_CODES) == 39
    assert len(set(SPEC_REFUSAL_CODES)) == 39
    assert all(code.startswith("NT_") for code in SPEC_REFUSAL_CODES)
    assert RefusalCode.NT_VARIANT_UNDECLARED.value == "NT_VARIANT_UNDECLARED"
    assert RefusalCode("NT_NO_BIAS") is RefusalCode.NT_NO_BIAS


def test_refusal_is_a_typed_first_class_result():
    refusal = VariantConfig().check("V04")
    assert isinstance(refusal, Refusal)
    assert refusal.code is RefusalCode.NT_VARIANT_UNDECLARED
    assert refusal.variant_id == "V04"
    assert str(refusal) == "NT_VARIANT_UNDECLARED: V04 (day boundary)"
    # It carries no price, no fallback option and no second code.
    assert refusal.detail == "day boundary"
    with pytest.raises(ValueError):
        RefusalCode("NT_NOT_A_CODE")


# --------------------------------------------------------------------------- #
# 3. undeclared fork refuses — no default is chosen
# --------------------------------------------------------------------------- #
def test_every_undeclared_variant_refuses_with_nt_variant_undeclared():
    declared = VariantConfig()
    for variant_id in VARIANT_REGISTER:
        with pytest.raises(VariantUndeclared) as excinfo:
            declared.read(variant_id)
        assert excinfo.value.refusal.code is RefusalCode.NT_VARIANT_UNDECLARED
        assert excinfo.value.refusal.variant_id == variant_id
        assert excinfo.value.variant_id == variant_id
        assert declared.check(variant_id) is not None
        assert declared.is_declared(variant_id) is False


def test_default_repo_row_still_refuses_when_undeclared():
    # V13 has a documented §14.4 repo default, but P1-01 must not apply it.
    cfg = VariantConfig()
    assert VARIANT_REGISTER["V13"].repo_default == "half-open"
    with pytest.raises(VariantUndeclared):
        cfg.read("V13")
    refusal = cfg.check("V13")
    assert refusal.code is RefusalCode.NT_VARIANT_UNDECLARED
    # An explicit declaration - even one that differs from the repo default -
    # is the only way the fork becomes readable.
    assert VariantConfig({"V13": "inclusive"}).read("V13") == "inclusive"


def test_unknown_variant_refuses_instead_of_looking_up_a_default():
    cfg = VariantConfig()
    with pytest.raises(VariantUndeclared) as excinfo:
        cfg.read("V99")
    assert excinfo.value.refusal.code is RefusalCode.NT_VARIANT_UNDECLARED
    assert cfg.check("V99").code is RefusalCode.NT_VARIANT_UNDECLARED
    assert cfg.is_declared("V99") is False


def test_blank_option_is_not_a_declaration():
    with pytest.raises(VariantUndeclared) as excinfo:
        VariantConfig({"V01": ""})
    assert excinfo.value.refusal.code is RefusalCode.NT_VARIANT_UNDECLARED


def test_invalid_or_unknown_options_refuse_at_construction():
    with pytest.raises(ValueError):
        VariantConfig({"V01": "indicator stack"})
    with pytest.raises(ValueError):
        VariantConfig({"V01": "STRUCTURE READ"})
    with pytest.raises(ValueError):
        VariantConfig({"V01": 7})
    with pytest.raises(ValueError):
        VariantConfig({"V28": "floor_4_1"})


def test_declared_option_is_read_verbatim_and_not_normalized():
    cfg = VariantConfig({"V01": "structure read", "V07": "F4"})
    assert cfg.read("V01") == "structure read"
    assert cfg.read("V07") == "F4"
    assert cfg.check("V01") is None
    assert cfg.declared_ids == ("V01", "V07")
    assert cfg.as_dict() == {"V01": "structure read", "V07": "F4"}
    assert "V01" in cfg
    assert "V02" not in cfg
    assert len(cfg) == 2


def test_with_selection_returns_a_new_fail_closed_config():
    base = VariantConfig({"V01": "structure read"})
    extended = base.with_selection("V28", "filter_2_1")
    with pytest.raises(VariantUndeclared):
        base.read("V28")
    assert extended.read("V28") == "filter_2_1"
    assert base is not extended
    assert base.as_dict() == {"V01": "structure read"}
    with pytest.raises(ValueError):
        base.with_selection("V28", "unknown")


# --------------------------------------------------------------------------- #
# 4. config hash
# --------------------------------------------------------------------------- #
def test_config_hash_is_canonical_deterministic_and_sensitive():
    empty = config_hash()
    assert empty == config_hash(None)
    assert empty == config_hash(VariantConfig())
    assert empty == config_hash({})
    assert re.fullmatch(r"[0-9a-f]{64}", empty)
    assert CONFIG_HASH_SCHEMA == "ict-variant-config/1"

    one = config_hash({"V01": "structure read"})
    assert one == config_hash(VariantConfig({"V01": "structure read"}))
    assert one != empty

    # Insertion order must not matter; the declared option must.
    a = config_hash({"V01": "structure read", "V13": "inclusive"})
    b = config_hash({"V13": "inclusive", "V01": "structure read"})
    assert a == b
    assert a != config_hash({"V01": "liquidity objective", "V13": "inclusive"})


def test_config_hash_binds_extra_declared_fields():
    base = config_hash({"V01": "structure read"})
    with_extra = config_hash({"V01": "structure read"}, target_mode="liquidity")
    assert with_extra != base
    assert with_extra == config_hash(
        {"V01": "structure read"}, target_mode="liquidity")
    assert with_extra != config_hash(
        {"V01": "structure read"}, target_mode="fixed")


# --------------------------------------------------------------------------- #
# 5. the owned config surfaces read the one register
# --------------------------------------------------------------------------- #
def test_setup_config_reads_the_declared_register_and_has_no_fallback():
    cfg = SetupConfig()
    with pytest.raises(VariantUndeclared):
        cfg.read_variant("V01")
    base_hash = cfg.config_hash
    assert re.fullmatch(r"[0-9a-f]{64}", base_hash)

    declared = SetupConfig(variant_config=VariantConfig(
        {"V01": "structure read"}))
    assert declared.read_variant("V01") == "structure read"
    assert declared.config_hash != base_hash
    assert declared.config_hash == declared.config_hash


def test_strategy_config_reads_the_same_register_and_hashes_declarations():
    cfg = StrategyConfig()
    with pytest.raises(VariantUndeclared):
        cfg.read_variant("V28")
    base_hash = cfg.config_hash

    declared = StrategyConfig(variant_config=VariantConfig(
        {"V28": "filter_2_1"}))
    assert declared.read_variant("V28") == "filter_2_1"
    assert declared.config_hash != base_hash

    other_mode = StrategyConfig(rr_mode="floor_3_1")
    assert other_mode.config_hash != base_hash


def test_setup_config_hash_reflects_declared_target_policy():
    assert (SetupConfig(target_mode="fixed").config_hash
            != SetupConfig(target_mode="liquidity").config_hash)


def test_config_surfaces_stay_dataclass_and_copy_safe():
    import copy
    import dataclasses

    # The new field must not break asdict/copy for existing callers.
    setup = SetupConfig(variant_config=VariantConfig({"V01": "structure read"}))
    strategy = StrategyConfig(variant_config=VariantConfig({"V28": "filter_2_1"}))
    assert dataclasses.asdict(setup)["variant_config"] == {
        "selections": (("V01", "structure read"),)}
    assert dataclasses.asdict(strategy)["variant_config"] == {
        "selections": (("V28", "filter_2_1"),)}
    assert copy.deepcopy(setup).config_hash == setup.config_hash
    assert copy.deepcopy(strategy).config_hash == strategy.config_hash
