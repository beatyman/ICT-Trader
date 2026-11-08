"""结构化数据契约（Input/Output Schema）+ 自洽校验验收测试。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ict_trader.schema import (validate_output, assign_signal_ids,  # noqa: E402
                               validate_input, parse_ai_response,
                               validate_visual_consistency)
from ict_trader.ai_prompt import build_ai_prompt, PROMPT_VERSION  # noqa: E402


def _setup(sid="L1", lo=100.0, hi=102.0, sl=99.0, tps=None, conf=70,
           signal_refs=None):
    return {"id": sid, "entry_zone": {"low": lo, "high": hi}, "sl": sl,
            "tp": tps if tps is not None else [{"label": "TP1", "level": 106.0, "rr": 2.0}],
            "confidence": conf,
            "signal_refs": signal_refs if signal_refs is not None else ["SWP-0001"]}


def _out(long=None, short=None):
    return {"meta": {"symbol": "X", "current_price": 100.0, "analysis_bias": "bullish",
                     "htf_confluence_score": 60},
            "long_setups": long or [], "short_setups": short or [],
            "no_trade_conditions": [], "data_gaps": [], "metadata": {}}


def test_validate_output_valid_long():
    r = validate_output(_out(long=[_setup()]))
    assert r["ok"] is True
    assert r["errors"] == []


def test_validate_output_long_order_violation():
    # long: tp < entry 违反 sl < entry < tp
    r = validate_output(_out(long=[_setup(tps=[{"label": "TP1", "level": 101.0, "rr": 1.0}])]))
    assert r["ok"] is False
    assert any("order violation" in e for e in r["errors"])


def test_validate_output_short_order_violation():
    # short: tp > entry 违反 tp < entry < sl
    s = _setup(sid="S1", lo=100.0, hi=102.0, sl=103.0,
               tps=[{"label": "TP1", "level": 101.5, "rr": 1.0}])
    r = validate_output(_out(short=[s]))
    assert r["ok"] is False
    assert any("order violation" in e for e in r["errors"])


def test_validate_output_confidence_range():
    r = validate_output(_out(long=[_setup(conf=150)]))
    assert r["ok"] is False
    assert any("confidence" in e for e in r["errors"])


def test_validate_output_rr_mismatch_is_warning():
    # rr 手填 5.0 但实数计算约 2.0 → 警告（非错误）
    r = validate_output(_out(long=[_setup(tps=[{"label": "TP1", "level": 106.0, "rr": 5.0}])]))
    assert r["ok"] is True
    assert any("rr" in w for w in r["warnings"])


def test_assign_signal_ids_stable():
    inp = {"ict_trader_15m": {
        "order_blocks": [{"type": "bullish", "low": 1, "high": 2},
                         {"type": "bearish", "low": 3, "high": 4}],
        "fvg": [{"type": "bullish", "low": 2, "high": 3}],
        "liquidity": {"sweep": "ssl"}},
        "chanlun_htf": {"1h": {"zhongshu": [{"low": 5, "high": 6}]}}}
    r1 = assign_signal_ids(inp)
    r2 = assign_signal_ids(inp)
    assert r1 == r2  # 幂等
    assert any(k.startswith("OB-") for k in r1)
    assert any(k.startswith("FVG-") for k in r1)
    assert any(k.startswith("SWP-") for k in r1)
    assert any(k.startswith("ZS-") for k in r1)


def test_ai_prompt_three_sections():
    p = build_ai_prompt({"meta": {"symbol": "X", "current_price": 1.0}})
    assert "硬规则" in p          # System
    assert "结构化数据" in p       # User
    assert "输出契约" in p         # Output Contract
    assert PROMPT_VERSION in p    # prompt_version 写入
    assert "data_gaps" in p       # 失败降级字段


def test_validate_input_missing_price():
    r = validate_input({"symbol": "X"})
    assert r["ok"] is False
    assert any("current_price" in e for e in r["errors"])


def test_validate_input_liquidity_inverted():
    r = validate_input({"symbol": "X", "current_price": 100.0,
                        "ict_trader_15m": {"liquidity": {"nearest_bsl": 99.0, "nearest_ssl": 101.0}}})
    assert r["ok"] is False
    assert any("inverted" in e for e in r["errors"])


def test_validate_input_direction_conflict_warning():
    r = validate_input({"symbol": "X", "current_price": 100.0,
                        "ict_trader_15m": {"htf_bias": "bullish", "structure_direction": "bearish"}})
    assert r["ok"] is True  # 冲突是警告不是错误
    assert any("conflict" in w for w in r["warnings"])


def test_parse_ai_response_valid_json():
    r = parse_ai_response('{"direction": "wait", "confidence": 0}')
    assert r["direction"] == "wait"


def test_parse_ai_response_strips_fence():
    r = parse_ai_response('```json\n{"direction": "long"}\n```')
    assert r["direction"] == "long"


def test_parse_ai_response_degrade_to_neutral():
    r = parse_ai_response("这不是 JSON 也不是任何可解析的东西", max_retries=1)
    assert r["direction"] == "wait"
    assert r["entry_zone"] is None
    assert any("parse failed" in g for g in r["data_gaps"])


def test_validate_visual_consistency_missing_ref():
    out = _out(long=[_setup(signal_refs=["SWP-0001", "OB-0001"])])
    r = validate_visual_consistency(out, [{"signal_id": "SWP-0001", "type": "sweep"}])
    assert r["ok"] is False
    assert any("OB-0001" in m for m in r["mismatches"])


def test_validate_visual_consistency_ok():
    out = _out(long=[_setup(signal_refs=["SWP-0001"])])
    r = validate_visual_consistency(out, [{"signal_id": "SWP-0001", "type": "sweep"}])
    assert r["ok"] is True
