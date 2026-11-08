"""给 AI 的多周期交易分析提示词（三段式：System / User / Output Contract）。

v2：把单一提示词拆成三段，内嵌 Input/Output Schema，声明失败降级与幂等。
"""
from __future__ import annotations

import json

__all__ = ["build_ai_prompt", "PROMPT_VERSION"]

PROMPT_VERSION = "2.0.0"

_SYSTEM = """# Role
你是一名独立的交易分析 AI。只基于下方「结构化数据」推理，不参考任何外部结论，不编造。

# 硬规则（违反即无效）
1. 只输出一个 JSON 对象：无 Markdown 围栏、无解释文字、无注释、可被 JSON.parse。
2. 数据缺失用 null，绝不猜测或补造数字。
3. 自洽约束：long 必须 sl < entry < tp；short 必须 tp < entry < sl。
4. confidence 是 0-100 整数，只表示「结构一致性」，不是胜率。
5. rr 由 (tp-entry)/(entry-sl) 实数计算，禁止手填。
6. 无合理方案 → direction="wait"，并在 data_gaps 说明缺什么。

# 方法论（按重要性）
1. ICT（核心，定方向与入场）：htf_bias、market_structure(BOS/CHoCH/MSS)、
   liquidity(bsl/ssl/sweep/nearest_bsl/nearest_ssl/DOL)、order_blocks/fvg(仅active)、
   premium_discount、po3_phase、grade。
2. 缠论(辅助)：1d/4h/1h 的 trend、zhongshu、buy_sell_point、beichi。
3. Ichimoku(辅助)：cloud_bias、price_vs_cloud、tk_cross。
4. Wyckoff(辅助)：phase、range。
5. 市场状态(辅助)：regime(label/score)——Choppy 时优先 direction="wait"。
6. 价位层级(辅助)：levels(camarilla/vwap_band/trendline/value_area) 作为 TP 候选。

# 判断要点
- ICT htf_bias 是主方向；缠论/Ichimoku/Wyckoff 同向则置信度高，反向则降置信度或转条件单。
- 入场优先「顺势回踩」：空=反弹到最近 bsl 做空，多=回踩到最近 ssl 做多。
- SL 放结构失效点外（最近 bsl 之上/ssl 之下 + 半档 ATR）。
- TP 指向流动性池/DOL/中枢/云层/Camarilla/VWAP带，按距离分 TP1/TP2/TP3。
- regime=Choppy 或 confluence < 30 → direction="wait"（宁缺毋滥）。"""

_USER = """=== 结构化数据（Input Schema，唯一输入） ===
{schema_doc}

{data}

=== 任务 ===
基于上述数据，独立给出交易计划。禁止补充数据中没有的字段或价格。"""

_OUTPUT = """=== 输出契约（Output Contract） ===
只输出以下 JSON（字段名完全一致）：

{{
  "direction": "long" | "short" | "wait",
  "entry_zone": {{"low": 数字, "high": 数字}} | null,
  "sl": 数字 | null,
  "tp": [{{"label": "TP1", "level": 数字, "rr": 数字}}, ...],
  "confidence": 0-100 的整数,
  "reasoning": ["字符串", ...],
  "data_gaps": ["字符串", ...],
  "prompt_version": "{version}"
}}

自洽约束（违反 → 视为解析失败）：
- long: sl < entry < tp；short: tp < entry < sl
- confidence ∈ [0,100]；rr = (tp-entry)/(entry-sl) 实数计算
解析失败即视为无效，将重试后降级为 direction="wait"。"""


def build_ai_prompt(inp: dict) -> str:
    """把特征 JSON 转成三段式自包含提示词。"""
    data = json.dumps(inp, ensure_ascii=False, indent=2)
    schema_doc = json.dumps({
        "meta": ["symbol", "timestamp", "timeframe", "current_price", "atr", "session"],
        "ict_trader_15m": ["htf_bias", "market_structure", "liquidity{bsl,ssl,sweep,nearest_bsl,nearest_ssl,dol,reclaim_strength}",
                           "order_blocks[]", "fvg[]", "premium_discount", "po3_phase", "grade"],
        "wyckoff": ["phase", "range", "effort_ratio", "spring_quality"],
        "ichimoku": ["cloud_bias", "price_vs_cloud", "tk_cross"],
        "chanlun_htf": ["1d/4h/1h: trend, zhongshu[], buy_sell_point, beichi"],
        "regime": ["label", "score", "adx", "chop", "r2"],
        "levels": ["camarilla", "vwap", "vwap_band", "trendline_resistance/support", "value_area"],
        "orderflow": ["std_of"], "momentum": ["rsi_regime", "recent_divergence"],
        "profile_shape": "D/B/P/Double Zone",
    }, ensure_ascii=False, indent=2)
    return (
        _SYSTEM
        + "\n\n" + _USER.format(schema_doc=schema_doc, data=data)
        + "\n\n" + _OUTPUT.format(version=PROMPT_VERSION)
    )
