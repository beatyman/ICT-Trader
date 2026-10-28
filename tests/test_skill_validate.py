import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "ict_validate_plan", ROOT / "skills" / "ict-analysis" / "scripts" / "validate_plan.py"
)
assert SPEC and SPEC.loader
validate_plan = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(validate_plan)


def test_no_trade_card_is_valid():
    card = {
        "symbol": "TEST",
        "last_time": "2026-10-28 15:00:00",
        "decision": "NO_TRADE",
        "rejected": "price_too_far_from_entry",
        "setup": None,
    }
    assert validate_plan.validate(card) == []


def test_trade_card_rejects_missing_setup():
    card = {
        "symbol": "TEST",
        "last_time": "2026-10-28 15:00:00",
        "decision": "TRADE",
        "setup": None,
    }
    errors = validate_plan.validate(card)
    assert errors


def test_trade_card_rejects_stale_entry():
    card = {
        "symbol": "TEST",
        "last_time": "2026-10-28 15:00:00",
        "decision": "TRADE",
        "entry_distance_atr": 1.2,
        "setup": {
            "direction": "bull",
            "entry_reference": 100,
            "stop": 95,
            "target": 110,
            "r_multiple": 2.0,
            "gross_rr": 2.0,
            "net_rr": 1.9,
            "entry_distance_atr": 1.2,
        },
        "recent_sweeps": [{"direction": "bull", "name": "SSL", "level": 100}],
    }
    errors = validate_plan.validate(card)
    assert any("stale" in e for e in errors)


def test_trade_card_rejects_net_rr_below_two():
    card = {
        "symbol": "TEST",
        "last_time": "2026-10-28 15:00:00",
        "decision": "TRADE",
        "setup": {
            "direction": "bull",
            "entry_reference": 100,
            "stop": 95,
            "target": 110.5,
            "r_multiple": 2.1,
            "gross_rr": 2.1,
            "net_rr": 1.9,
        },
        "recent_sweeps": [{"direction": "bull", "name": "SSL", "level": 100}],
    }
    errors = validate_plan.validate(card)
    assert any("net RR" in e for e in errors)
