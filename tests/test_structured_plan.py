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
