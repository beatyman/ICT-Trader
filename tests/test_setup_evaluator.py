from datetime import datetime, timezone
from ict_trader.setup_evaluator import evaluate
from ict_trader.setup_rules import rule_by_id

def features(**over):
    x = {
        "direction": "long", "price": 100.0,
        "htf_bias": {"4h": {"bias": "bullish"}, "1D": {"bias": "bullish"}},
        "sweep": {"level": 99.0},
        "structure": {"event": "MSS"},
        "fvg": [{"direction": "bullish", "top": 101.0, "bottom": 100.0}],
        "ob": [{"direction": "bullish", "top": 101.0, "bottom": 100.0}],
        "breaker": [{"direction": "bullish", "top": 101.0, "bottom": 100.0}],
        "targets": [106.0],
    }
    x.update(over); return x

def test_candidate_passes_all_required_conditions():
    now = datetime(2026, 11, 10, 15, 0, tzinfo=timezone.utc)
    c = evaluate(rule_by_id("SB"), features(), now=now, atr=1.0)
    assert c is not None and not c.blockers and c.rr >= 2

def test_bias_conflict_blocks():
    now = datetime(2026, 11, 10, 15, 0, tzinfo=timezone.utc)
    c = evaluate(rule_by_id("SB"), features(htf_bias={"4h": {"bias": "bearish"}, "1D": {"bias": "bearish"}}), now=now, atr=1.0)
    assert "htf_bias_conflict" in c.blockers

def test_missing_mss_blocks():
    now = datetime(2026, 11, 10, 15, 0, tzinfo=timezone.utc)
    c = evaluate(rule_by_id("M2022"), features(structure={"event": "BOS"}), now=now, atr=1.0)
    assert "missing_mss" in c.blockers

def test_rr_below_min_blocks():
    now = datetime(2026, 11, 10, 15, 0, tzinfo=timezone.utc)
    c = evaluate(rule_by_id("SB"), features(targets=[100.5]), now=now, atr=1.0)
    assert "rr_below_min" in c.blockers
