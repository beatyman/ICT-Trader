"""三引擎融合交易决策算法（Wyckoff + Ichimoku + ICT）。

严格实现融合决策：
  1. 方向共识投票
  2. 位置校验与入场区计算
  3. 失效条件生成
  4. 置信度计算（1-10）
  5. 四层评分（L1 结构 / L2 趋势过滤 / L3 ICT方向 / L4 位置校验）

纯函数：输入三系统信号，输出标准化融合计划。所有价格四舍五入到整数。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field


@dataclass
class FusionInput:
    current_price: float
    # Wyckoff
    wyckoff_phase: str = "中性"          # 吸筹 / 上涨 / 派发 / 下跌 / 中性
    wyckoff_valid: bool = True
    key_level: float = 0.0               # 关键失效位
    # Ichimoku
    ichimoku_filter: str = "观望"        # 多 / 空 / 观望
    cloud_bottom: float = 0.0
    cloud_top: float = 0.0
    # ICT
    ict_direction: str = "观望"          # 多 / 空 / 观望
    price_in_premium: bool = False
    wants_premium_for_short: bool = True


@dataclass
class FusionPlan:
    direction: str = "观望"
    lean: str = "中性"                   # 偏多 / 偏空 / 中性
    position_state: str = "需等待合适位置"  # 需等待合适位置 / 可考虑入场
    entry_low: float = 0.0
    entry_high: float = 0.0
    stop: float = 0.0
    target1: float = 0.0
    target2: float = 0.0
    invalidations: list = field(default_factory=list)
    confidence: int = 5
    layers: dict = field(default_factory=dict)
    summary: str = ""


def _round(v: float) -> int:
    """四舍五入（round half up）；加 1e-9 抵消浮点误差（如 100*1.025=102.4999…）。"""
    return int(math.floor(v + 0.5 + 1e-9))


def decide(inp: FusionInput) -> FusionPlan:
    """执行融合决策，返回 FusionPlan。"""
    p = FusionPlan()

    # ---- 1. 方向共识投票 ----
    vl = vs = 0
    if inp.ichimoku_filter == "多":
        vl += 1
    elif inp.ichimoku_filter == "空":
        vs += 1
    if inp.ict_direction == "多":
        vl += 1
    elif inp.ict_direction == "空":
        vs += 1
    if inp.wyckoff_valid:
        if inp.wyckoff_phase in ("吸筹", "上涨"):
            vl += 1
        elif inp.wyckoff_phase in ("派发", "下跌"):
            vs += 1
    if vs > vl:
        direction = "空"
    elif vl > vs:
        direction = "多"
    else:
        direction = "观望"
    p.direction = direction

    if direction == "观望":
        p.lean = "中性"
        p.position_state = "观望"
        p.invalidations = ["三系统方向未形成共识，继续观望"]
        p.confidence = 5
        p.layers = {"L1_结构": ("PASS" if inp.wyckoff_valid else "FAIL"),
                    "L2_趋势过滤": "FAIL",
                    "L3_ICT方向": "FAIL",
                    "L4_位置校验": "FAIL"}
        p.summary = "三系统方向未形成共识，观望。"
        return p

    # ---- 2. 位置校验与入场区 ----
    immediate = True
    if direction == "空":
        p.lean = "偏空"
        if (not inp.price_in_premium) and inp.wants_premium_for_short:
            immediate = False
            lower = min(inp.cloud_bottom, inp.key_level)
            upper = lower * 1.005
            stop = max(inp.cloud_top * 0.98, inp.current_price * 1.06)
            risk = stop - upper
        else:
            lower = inp.current_price * 0.998
            upper = inp.current_price * 1.002
            stop = inp.current_price * 1.025
            risk = stop - upper
        t1 = upper - risk * 2
        t2 = upper - risk * 3
    else:  # 多
        p.lean = "偏多"
        lower = inp.current_price * 0.998
        upper = inp.current_price * 1.002
        stop = inp.current_price * 0.975
        risk = lower - stop
        t1 = lower + risk * 2
        t2 = lower + risk * 3

    p.entry_low = _round(lower)
    p.entry_high = _round(upper)
    p.stop = _round(stop)
    p.target1 = _round(t1)
    p.target2 = _round(t2)
    p.position_state = "可考虑入场" if immediate else "需等待合适位置"

    # ---- 3. 失效条件 ----
    inv = []
    if direction == "空":
        inv.append(f"收盘站上 {_round(inp.cloud_top)} → 空头观点作废")
        if not inp.wyckoff_valid:
            inv.append(f"已跌破 {_round(inp.key_level)}，转为破位逻辑")
    else:
        inv.append(f"收盘跌破 {_round(inp.cloud_bottom)} → 多头观点作废")
    p.invalidations = inv

    # ---- 4. 置信度 ----
    conf = 5
    if inp.ichimoku_filter == direction:
        conf += 2
    if inp.ict_direction == direction:
        conf += 1
    if inp.wyckoff_valid and (
        (direction == "多" and inp.wyckoff_phase in ("吸筹", "上涨"))
        or (direction == "空" and inp.wyckoff_phase in ("派发", "下跌"))
    ):
        conf += 1
    elif (not inp.wyckoff_valid) or (
        (direction == "多" and inp.wyckoff_phase in ("派发", "下跌"))
        or (direction == "空" and inp.wyckoff_phase in ("吸筹", "上涨"))
    ):
        conf -= 1
    if not immediate:
        conf = min(conf, 6)
    p.confidence = max(1, min(10, conf))

    # ---- 5. 四层评分 ----
    p.layers = {
        "L1_结构": "PASS" if inp.wyckoff_valid else "FAIL",
        "L2_趋势过滤": "PASS" if inp.ichimoku_filter == direction else "FAIL",
        "L3_ICT方向": "PASS" if inp.ict_direction == direction else "FAIL",
        "L4_位置校验": "PASS" if immediate else "FAIL",
    }

    # ---- 摘要 ----
    parts = [f"方向共识{'空' if direction == '空' else '多'}"]
    if not immediate:
        parts.append("位置未到位需等待")
    p.summary = "；".join(parts) + "。"
    return p


def render(inp: FusionInput) -> str:
    """按强制输出格式渲染融合交易计划。"""
    p = decide(inp)
    if p.direction == "观望":
        lines = [
            "【融合交易计划】",
            f"- 方向：{p.direction}（中性，观望）",
            "- 入场区域：0 – 0",
            "- 止损：0",
            "- 目标1：0（约 1:2）",
            "- 目标2：0（约 1:3）",
            "- 失效条件：",
            "    ① 三系统方向未形成共识，继续观望",
            f"- 置信度：{p.confidence}/10",
            "",
            f"- 简要逻辑总结：{p.summary}",
            "",
            "四层评分：",
            f"  L1_结构: {p.layers['L1_结构']}",
            f"  L2_趋势过滤: {p.layers['L2_趋势过滤']}",
            f"  L3_ICT方向: {p.layers['L3_ICT方向']}",
            f"  L4_位置校验: {p.layers['L4_位置校验']}",
        ]
        return "\n".join(lines)

    lines = [
        "【融合交易计划】",
        f"- 方向：{p.direction}（{p.lean}，{p.position_state}）",
        f"- 入场区域：{p.entry_low} – {p.entry_high}",
        f"- 止损：{p.stop}",
        f"- 目标1：{p.target1}（约 1:2）",
        f"- 目标2：{p.target2}（约 1:3）",
        "- 失效条件：",
    ]
    for i, iv in enumerate(p.invalidations, 1):
        lines.append(f"    {['①', '②'][i-1]} {iv}")
    lines += [
        f"- 置信度：{p.confidence}/10",
        "",
        f"- 简要逻辑总结：{p.summary}",
        "",
        "四层评分：",
        f"  L1_结构: {p.layers['L1_结构']}",
        f"  L2_趋势过滤: {p.layers['L2_趋势过滤']}",
        f"  L3_ICT方向: {p.layers['L3_ICT方向']}",
        f"  L4_位置校验: {p.layers['L4_位置校验']}",
    ]
    return "\n".join(lines)
