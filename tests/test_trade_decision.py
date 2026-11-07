"""定时交易决策（仓位状态机）验收测试。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from trade_decision import decide  # noqa: E402


def _plan(valid=False, conf=60, entry_lo=2230.0, entry_hi=2240.0):
    s = {"id": "S1", "status": "valid" if valid else "conditional",
         "confidence": conf, "entry_zone": {"low": entry_lo, "high": entry_hi},
         "sl": 2245.0, "tp": [{"level": 2200.0, "rr": 2.0}]}
    return {"short_setups": [s], "long_setups": []}


def test_no_position_no_valid_setup_is_wait():
    st = {"position": None}
    action, _ = decide(st, _plan(valid=False), price=2233.0, timestamp="t", min_conf=50)
    assert action == "wait"


def test_no_position_valid_setup_in_zone_is_open():
    st = {"position": None}
    action, reason = decide(st, _plan(valid=True, entry_lo=2230.0, entry_hi=2240.0),
                            price=2235.0, timestamp="t", min_conf=50)
    assert action == "open"
    assert "entry zone" in reason


def test_long_position_hold_then_tp_sl():
    st = {"position": {"direction": "long", "sl": 100.0, "tp1": 110.0}}
    # hold：100 < price < 110
    a, _ = decide(st, _plan(), price=105.0, timestamp="t", min_conf=50)
    assert a == "hold"
    # take profit
    a, _ = decide(st, _plan(), price=111.0, timestamp="t", min_conf=50)
    assert a == "take_profit"
    # stop loss
    a, _ = decide(st, _plan(), price=99.0, timestamp="t", min_conf=50)
    assert a == "stop_loss"


def test_short_position_hold_then_tp_sl():
    st = {"position": {"direction": "short", "sl": 110.0, "tp1": 100.0}}
    # hold：100 < price < 110
    a, _ = decide(st, _plan(), price=105.0, timestamp="t", min_conf=50)
    assert a == "hold"
    # take profit：price <= 100
    a, _ = decide(st, _plan(), price=99.0, timestamp="t", min_conf=50)
    assert a == "take_profit"
    # stop loss：price >= 110
    a, _ = decide(st, _plan(), price=111.0, timestamp="t", min_conf=50)
    assert a == "stop_loss"
