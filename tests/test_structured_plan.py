"""多周期结构化分析器（structured_plan）验收测试。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ict_trader.structured_plan import structured_plan  # noqa: E402
from ict_trader.ai_prompt import build_ai_prompt  # noqa: E402


def _inp(**over):
    d = {
        "symbol": "XAUUSD",
        "timestamp": "2026-01-01T12:00:00Z",
        "current_price": 2000.0,
        "account": {"risk_per_trade_pct": 1, "max_leverage": None},
        "ict_trader_15m": {
            "htf_bias": "bearish",
            "market_structure": "BOS",
            "liquidity": {"bsl": [2020, 2030], "ssl": [1980, 1970], "sweep": "bsl"},
            "order_blocks": [{"type": "bearish", "low": 2010, "high": 2020, "mitigated": False}],
            "fvg": [{"type": "bearish", "low": 2010, "high": 2020, "filled": False}],
            "killzone": "london",
            "po3_phase": "distribution",
        },
        "wyckoff": {"phase": "distribution", "events": ["upthrust"], "volume_note": None,
                    "range": {"low": 1980, "high": 2020}},
        "ichimoku": {"tenkan": 1990, "kijun": 1995, "senkou_a": 2020, "senkou_b": 2040,
                     "chikou": None, "cloud_bias": "bearish", "price_vs_cloud": "below",
                     "tk_cross": "bearish"},
        "chanlun_htf": {
            "1d": {"trend": "down", "zhongshu": [{"low": 1980, "high": 2020}],
                   "buy_sell_point": "2S", "beichi": False},
            "4h": {"trend": "down", "zhongshu": [{"low": 1990, "high": 2010}],
                   "buy_sell_point": "1S", "beichi": True},
            "1h": {"trend": "down", "zhongshu": [{"low": 1990, "high": 2010}],
                   "buy_sell_point": None, "beichi": False},
        },
        "notes": None,
    }
    d.update(over)
    return d


def test_output_is_valid_json_and_has_required_keys():
    out = structured_plan(_inp())
    # 可被 JSON.parse 解析
    json.loads(json.dumps(out, ensure_ascii=False))
    for k in ["meta", "long_setups", "short_setups", "no_trade_conditions", "data_gaps", "risk_notes"]:
        assert k in out
    assert out["meta"]["analysis_bias"] in ("bullish", "bearish", "neutral")


def test_bearish_alignment_gives_short_setup():
    out = structured_plan(_inp())
    assert out["meta"]["analysis_bias"] == "bearish"
    assert out["meta"]["htf_confluence_score"] > 0
    assert out["short_setups"], "bearish alignment should produce short setup"
    s = out["short_setups"][0]
    assert s["id"].startswith("S")
    assert s["entry_zone"]["low"] <= s["entry_zone"]["high"]
    assert s["sl"] > 0
    assert s["tp"]


def test_neutral_htf_gives_no_trade():
    inp = _inp()
    inp["ict_trader_15m"]["htf_bias"] = None  # ICT 也无方向
    for tf in ("1d", "4h", "1h"):
        inp["chanlun_htf"][tf]["trend"] = "range"
    inp["wyckoff"]["phase"] = None
    inp["ichimoku"]["cloud_bias"] = None
    inp["ichimoku"]["price_vs_cloud"] = "inside"
    out = structured_plan(inp)
    assert out["meta"]["analysis_bias"] == "neutral"
    assert any("neutral" in c.lower() for c in out["no_trade_conditions"])


def test_ict_primary_overrides_auxiliary_conflict():
    # ICT 主方向空，但 Wyckoff 吸筹(多)冲突 → 仍是空方向，不翻转
    inp = _inp()
    inp["ict_trader_15m"]["htf_bias"] = "bearish"
    inp["wyckoff"]["phase"] = "accumulation"  # 与空冲突
    out = structured_plan(inp)
    assert out["meta"]["analysis_bias"] == "bearish"  # ICT 主，不翻转
    assert out["meta"]["htf_confluence_detail"]["ict_htf"] == -3


def test_data_gaps_reported():
    out = structured_plan(_inp())
    # 输入完整时 data_gaps 为空（或只有缺失项）
    assert isinstance(out["data_gaps"], list)


def test_missing_current_price_is_no_trade():
    inp = _inp()
    inp["current_price"] = None
    out = structured_plan(inp)
    assert any("current_price" in c for c in out["no_trade_conditions"])


def test_ai_prompt_self_contained():
    """AI 提示词自包含：含方法论文本 + 完整 JSON + 输出字段契约。"""
    inp = _inp()
    p = build_ai_prompt(inp)
    assert "ICT" in p and "缠论" in p and "Ichimoku" in p and "Wyckoff" in p
    assert '"direction"' in p and '"entry_zone"' in p and '"confidence"' in p
    assert '"symbol": "XAUUSD"' in p  # 数据已内联
    assert "只输出" in p  # 输出约束


def _weak_confluence_inp():
    """弱共振场景：让 RS 加分可见（辅助都中性，只有 ICT 主方向）。"""
    inp = _inp()
    inp["ict_trader_15m"]["htf_bias"] = "bearish"
    for tf in ("1d", "4h", "1h"):
        inp["chanlun_htf"][tf]["trend"] = "range"
    inp["wyckoff"]["phase"] = None
    inp["ichimoku"]["cloud_bias"] = None
    inp["ichimoku"]["price_vs_cloud"] = "inside"
    return inp


def test_reclaim_strength_boosts_aligned_confidence():
    """RS 方向一致且强回抽(>=100) → 置信度加分。"""
    import json as _json
    aligned = _weak_confluence_inp()
    aligned["ict_trader_15m"]["liquidity"]["reclaim_strength"] = 150.0
    aligned["ict_trader_15m"]["liquidity"]["reclaim_direction"] = "bear"
    misaligned = _json.loads(_json.dumps(aligned))
    misaligned["ict_trader_15m"]["liquidity"]["reclaim_direction"] = "bull"
    c1 = structured_plan(aligned)["short_setups"][0]["confidence"]
    c2 = structured_plan(misaligned)["short_setups"][0]["confidence"]
    assert c1 > c2  # 方向一致加分，方向相反不加


def _bull_inp(**over):
    """看多场景（对照 ETHUSDT 实盘 2026-11-08 04:00 的计划）。"""
    d = _inp()
    d["current_price"] = 2238.12
    d["metadata"] = {"volatility_context": {"atr_14": 2.88}}
    d["ict_trader_15m"].update({
        "htf_bias": "bullish",
        "market_structure": "BOS",
        "liquidity": {"bsl": [2239.71, 2241.37], "ssl": [2236.78],
                      "sweep": "ssl", "dol": 2229.29},  # DOL 在价格下方：多单的错误侧
    })
    d["wyckoff"]["phase"] = "markup"
    d["ichimoku"].update({"cloud_bias": "bullish", "price_vs_cloud": "above"})
    for tf in ("1d", "4h", "1h"):
        d["chanlun_htf"][tf]["trend"] = "up"
    d.update(over)
    return d


def test_aligned_bias_marks_setup_valid():
    """回归：bias=bullish 与 direction=long 不再因字符串不同而永远 conditional。"""
    out = structured_plan(_bull_inp())
    assert out["meta"]["analysis_bias"] == "bullish"
    longs = out["long_setups"]
    assert longs, "bullish bias should produce long setups"
    assert longs[0]["status"] == "valid"
    assert longs[0]["confidence"] >= out["meta"]["htf_confluence_score"]


def test_neutral_bias_setups_are_conditional():
    inp = _bull_inp()
    inp["ict_trader_15m"]["htf_bias"] = None
    for tf in ("1d", "4h", "1h"):
        inp["chanlun_htf"][tf]["trend"] = "range"
    inp["wyckoff"]["phase"] = None
    inp["ichimoku"].update({"cloud_bias": None, "price_vs_cloud": "inside"})
    out = structured_plan(inp)
    assert out["meta"]["analysis_bias"] == "neutral"
    for s in out["long_setups"] + out["short_setups"]:
        assert s["status"] == "conditional"


def test_long_tps_are_above_entry_and_rr_ge_1():
    """回归：DOL 在错误侧时不得被塞进多单止盈；TP 必须能赚到。"""
    out = structured_plan(_bull_inp())
    s = out["long_setups"][0]
    entry_lo = s["entry_zone"]["low"]
    entry_hi = s["entry_zone"]["high"]
    assert entry_lo <= entry_hi
    assert s["sl"] < entry_lo
    levels = [t["level"] for t in s["tp"]]
    assert levels, "long setup must carry TPs"
    assert 2229.29 not in levels, "DOL below entry must not be a long TP"
    for t in s["tp"]:
        assert t["level"] > entry_lo, t
        assert t["rr"] >= 1.0, t


def test_short_tps_are_below_entry_and_rr_ge_1():
    """回归：空单止盈必须在入场下方，反向 DOL 被丢弃。"""
    inp = _inp()
    inp["ict_trader_15m"]["liquidity"] = {"bsl": [2020, 2030], "ssl": [1980, 1970],
                                          "sweep": "bsl", "dol": 2100.0}  # DOL 在上方：空单错误侧
    out = structured_plan(inp)
    s = out["short_setups"][0]
    levels = [t["level"] for t in s["tp"]]
    assert 2100.0 not in levels, "DOL above entry must not be a short TP"
    for t in s["tp"]:
        assert t["level"] < s["entry_zone"]["high"], t
        assert t["rr"] >= 1.0, t


@pytest.mark.parametrize("direction", ["long", "short"])
def test_sl_is_outside_entry_zone(direction):
    """回归：SL 必须落在入场区之外，否则价格扫到区间边缘成交即刻被止损。"""
    inp = _bull_inp() if direction == "long" else _inp()
    out = structured_plan(inp)
    setups = out["long_setups"] if direction == "long" else out["short_setups"]
    assert setups
    for s in setups:
        lo, hi = s["entry_zone"]["low"], s["entry_zone"]["high"]
        if direction == "long":
            assert s["sl"] < lo, f"{s['id']} SL {s['sl']} inside/above zone [{lo}, {hi}]"
        else:
            assert s["sl"] > hi, f"{s['id']} SL {s['sl']} inside/below zone [{lo}, {hi}]"
