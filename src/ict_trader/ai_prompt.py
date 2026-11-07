"""给 AI 的多周期交易分析提示词模板。

把「输入特征 JSON」（ICT 主 + Wyckoff/Ichimoku/缠论 辅助 + metadata）转成
一段自包含的提示词，让独立的 AI 综合给出交易计划（direction/entry/sl/tp/confidence/reasoning）。
"""
from __future__ import annotations

import json

__all__ = ["build_ai_prompt"]

_SYSTEM = """你是一个独立的交易分析 AI。基于下面这段结构化市场数据，独立给出你的交易计划。不要参考任何外部或既定的结论，只基于这份数据推理。

分析方法论（按重要性）：
1. ICT（核心，定方向与入场）：htf_bias(1D/4H对齐方向)、market_structure(BOS/CHoCH/MSS)+structure_direction、
   liquidity(bsl=上方流动性/ssl=下方流动性/sweep=哪边被扫/nearest_bsl/nearest_ssl/DOL)、
   order_blocks/fvg(仅active)、premium_discount(溢价/折价)、po3_phase(吸筹/操纵/派发)、grade。
2. 缠论(辅助)：1d/4h/1h 的 trend(up/down/range)、zhongshu(中枢上下沿)、buy_sell_point(1B/2B/3B/1S/2S/3S)、beichi(背驰)。
3. Ichimoku(辅助)：cloud_bias、price_vs_cloud、tk_cross。
4. Wyckoff(辅助)：phase(accumulation/markup/distribution/markdown)、range。

判断要点：
- ICT 的 htf_bias 是主方向；缠论/Ichimoku/Wyckoff 与其同向则置信度更高，反向则降置信度或转条件单。
- 入场优先「顺势回踩」：空=反弹到最近 bsl 阻力做空(sell the rally)，多=回踩到最近 ssl 支撑做多(buy the dip)。
- SL 放在结构失效点之外（最近 bsl 之上/最近 ssl 之下 + 半档 ATR）。
- TP 指向流动性池(ssl/bsl)/DOL/中枢边界/云层边界，按距离分 TP1/TP2/TP3。
- 数据缺失用 null，绝不编造；无合理方案 direction="wait"。

要求：
1. 只输出一个 JSON 对象，不要 Markdown 代码围栏，不要任何解释文字、不要注释，确保可被 JSON.parse 解析。
2. 输出字段（字段名必须完全一致）：
   {
     "direction": "long"|"short"|"wait",
     "entry_zone": {"low": 数字, "high": 数字} | null,
     "sl": 数字 | null,
     "tp": [{"label":"TP1","level":数字,"rr":数字}, ...],
     "confidence": 0-100 的整数,
     "reasoning": ["字符串", ...]
   }
3. confidence 只表示结构一致性，不代表胜率；这是分析、不是投资建议。"""


def build_ai_prompt(inp: dict) -> str:
    """把特征 JSON 转成自包含的 AI 提示词。"""
    data = json.dumps(inp, ensure_ascii=False, indent=2)
    return f"{_SYSTEM}\n\n=== 结构化数据 ===\n{data}\n\n现在，只输出你的 JSON 交易计划。"
