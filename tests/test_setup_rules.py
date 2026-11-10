from datetime import datetime, timezone
from ict_trader.setup_rules import RULES, active_rules, rule_by_id

def test_ny_silver_bullet_window():
    # 15:00 UTC = 10:00 EST in November
    ts = datetime(2026, 11, 10, 15, 0, tzinfo=timezone.utc)
    ids = {r.rule_id for r in active_rules(ts)}
    assert "SB" in ids

def test_outside_window():
    ts = datetime(2026, 11, 10, 20, 0, tzinfo=timezone.utc)  # 15:00 EST
    assert "SB" not in {r.rule_id for r in active_rules(ts)}

def test_rules_are_unique_and_prioritized():
    assert len(RULES) == 9
    assert len({r.rule_id for r in RULES}) == 9
    assert rule_by_id("M2022").require_mss is True

