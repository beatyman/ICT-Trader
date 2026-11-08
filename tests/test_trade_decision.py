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
    # 到 TP1 → be（移损到成本）
    a, _ = decide(st, _plan(), price=111.0, timestamp="t", min_conf=50)
    assert a == "be"
    # stop loss：跌破止损（未保本时）
    a, _ = decide(st, _plan(), price=99.0, timestamp="t", min_conf=50)
    assert a == "stop_loss"


def test_short_position_hold_then_tp_sl():
    st = {"position": {"direction": "short", "sl": 110.0, "tp1": 100.0}}
    # hold：100 < price < 110
    a, _ = decide(st, _plan(), price=105.0, timestamp="t", min_conf=50)
    assert a == "hold"
    # 到 TP1 → be（移损到成本）
    a, _ = decide(st, _plan(), price=99.0, timestamp="t", min_conf=50)
    assert a == "be"
    # stop loss：涨破止损（未保本时）
    a, _ = decide(st, _plan(), price=111.0, timestamp="t", min_conf=50)
    assert a == "stop_loss"


def test_be_after_tp1_moves_stop_to_entry():
    """TP1 到 → be（移损到成本）；之后回到成本 → take_profit(0R)。"""
    st = {"position": {"direction": "long", "entry": 100.0, "sl": 95.0, "tp1": 110.0, "be": False}}
    # 未到 TP1 → hold
    a, _ = decide(st, _plan(), price=105.0, timestamp="t", min_conf=50)
    assert a == "hold"
    # 到 TP1 → be（移损到成本）
    a, _ = decide(st, _plan(), price=111.0, timestamp="t", min_conf=50)
    assert a == "be"
    # 手动把状态改成 be=True（模拟 main() 的移损）
    st["position"]["be"] = True
    st["position"]["sl"] = 100.0
    # 回到成本 → take_profit（0R）
    a, reason = decide(st, _plan(), price=99.0, timestamp="t", min_conf=50)
    assert a == "take_profit"
    assert "BE" in reason or "0R" in reason


def test_position_with_null_tp1_does_not_crash():
    """回归：open 时若无 TP，state 里 tp1 为 None，早期版本 float(None) 会抛 TypeError。"""
    st = {"position": {"direction": "long", "sl": 100.0, "tp1": None}}
    a, _ = decide(st, _plan(), price=105.0, timestamp="t", min_conf=50)
    assert a == "hold"


def test_low_confidence_valid_setup_is_wait():
    st = {"position": None}
    a, _ = decide(st, _plan(valid=True, conf=20), price=2235.0, timestamp="t", min_conf=50)
    assert a == "wait"


def test_find_entry_setup_respects_confidence_and_zone():
    from trade_decision import find_entry_setup
    plan = _plan(valid=True, conf=60)
    assert find_entry_setup(plan, 2235.0, 50)["id"] == "S1"
    assert find_entry_setup(plan, 2235.0, 61) is None      # 置信度不足
    assert find_entry_setup(plan, 2225.0, 50) is None      # 不在入场区
    assert find_entry_setup({"short_setups": []}, 2235.0, 50) is None
