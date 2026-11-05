"""模块 5：大模型评分层（严格 JSON，可解释 breakdown）。

生产环境：把结构化输入交给大模型，输出下述 JSON。
本实现：无 LLM 可用，用确定性函数生成同一 Schema 的 JSON（可替换为 LLM 调用）。

评分规则：
  - ICT 结构/扫单/OB-FVG/折价溢价质量为主（ict_setup_quality 0-40 + liquidity 0-20）
  - Wyckoff / Ichimoku 只做加分或否决，neutral 不得抬成 enter
  - 引擎冲突 / 云内 / 区间内 / 逆背景 / 薄云 / 扫单未确认 → 扣分或 veto
  - 必须可解释，禁止只给总分
"""
from __future__ import annotations

from .main_strategy import ICTSignal
from .background import WyckoffBackground, IchimokuBackground
from .fusion import FusionOutput


def score(ict: ICTSignal, wy: WyckoffBackground, ichi: IchimokuBackground,
          fused: FusionOutput, symbol: str = "", timestamp: str = "") -> dict:
    """生成严格 JSON 评分。返回字典（调用方 json.dumps）。"""
    b = {}
    # ICT setup quality（0-40）：ICT 置信度 × 0.4
    b["ict_setup_quality"] = round(ict.confidence * 0.4, 1)

    # 流动性与结构（0-20）：扫单 + MSS + 位移
    liq = 0.0
    rules = ict.rules
    if rules.get("L2-04") == "PASS":
        liq += 8
    if rules.get("L1-04") == "PASS":
        liq += 6
    if rules.get("L3-10") == "PASS":
        liq += 6
    b["liquidity_and_structure"] = round(liq, 1)

    # Wyckoff 对齐（0-15）
    wy_score = 0.0
    if ict.direction == "long" and wy.allow_long:
        wy_score += 8
        if wy.bias == "long":
            wy_score += 7
    elif ict.direction == "short" and wy.allow_short:
        wy_score += 8
        if wy.bias == "short":
            wy_score += 7
    elif ict.direction == "neutral":
        wy_score = 0
    else:
        wy_score = 0  # veto → 0（另有 veto 扣分）
    b["wyckoff_alignment"] = round(wy_score, 1)

    # Ichimoku 对齐（0-15）
    ichi_score = 0.0
    if ict.direction == "long" and ichi.allow_long:
        ichi_score += 8
        if ichi.cloud_state == "above":
            ichi_score += 7
    elif ict.direction == "short" and ichi.allow_short:
        ichi_score += 8
        if ichi.cloud_state == "below":
            ichi_score += 7
    b["ichimoku_alignment"] = round(ichi_score, 1)

    # 风险回报质量（0-10）：RR
    rr = 0.0
    if ict.direction != "neutral" and ict.stop and ict.entry:
        risk = abs(ict.entry - ict.stop)
        reward = abs(ict.tp1 - ict.entry)
        if risk > 0:
            rr = reward / risk
    if rr >= 2.0:
        b["risk_reward_quality"] = 10
    elif rr >= 1.5:
        b["risk_reward_quality"] = 7
    elif rr > 0:
        b["risk_reward_quality"] = 4
    else:
        b["risk_reward_quality"] = 0

    total = sum(b.values())

    # veto / red flags 扣分（可把总分推到负）
    red_flags = []
    if fused.action == "avoid":
        red_flags.append(fused.veto_reason or "auxiliary veto")
    if fused.action == "avoid" or (ict.direction != "neutral" and fused.action == "wait"):
        red_flags.append("auxiliary veto or conflict")
    if wy.in_range:
        red_flags.append("price inside Wyckoff range")
    if ichi.cloud_state == "inside":
        red_flags.append("price inside Ichimoku cloud")
    if ichi.cloud_thickness == "thin":
        red_flags.append("thin cloud")
    if ict.direction != "neutral" and not (ict.rules.get("L2-04") == "PASS"):
        red_flags.append("sweep not confirmed")

    # veto → 总分直接压负；否则按 red_flags 每条 -10
    if fused.action == "avoid":
        score = -60 + min(total, 40)
    else:
        score = total - 10 * len(red_flags)
    score = max(-100, min(100, int(round(score))))

    action = "enter" if (fused.action == "enter" and score >= 60) else (
        "avoid" if fused.action == "avoid" else "wait")

    return {
        "timestamp": timestamp,
        "symbol": symbol,
        "ict": {
            "direction": ict.direction,
            "confidence": round(ict.confidence, 1),
            "entry": ict.entry, "stop": ict.stop,
            "tp1": ict.tp1, "tp2": ict.tp2,
            "invalidation": ict.invalidation,
            "setup_type": ict.setup_type,
            "htf_alignment": ict.htf_alignment,
            "premium_discount": ict.premium_discount,
        },
        "wyckoff_background": {
            "bias": wy.bias, "range_state": wy.range_state, "phase": wy.phase,
            "allow_long": wy.allow_long, "allow_short": wy.allow_short,
            "size_factor": wy.size_factor, "in_range": wy.in_range,
        },
        "ichimoku_background": {
            "cloud_state": ichi.cloud_state, "cloud_color": ichi.cloud_color,
            "cloud_thickness": ichi.cloud_thickness, "tk_state": ichi.tk_state,
            "kijun_state": ichi.kijun_state,
            "allow_long": ichi.allow_long, "allow_short": ichi.allow_short,
            "size_factor": ichi.size_factor,
        },
        "fused_direction": fused.direction,
        "fused_confidence": round(fused.confidence, 1),
        "size_factor": fused.size_factor,
        "score": score,
        "score_breakdown": b,
        "action": action,
        "entry": fused.entry, "stop": fused.stop, "tp1": fused.tp1, "tp2": fused.tp2,
        "invalidation": fused.invalidation,
        "reasoning": "; ".join(ict.reasons + fused.reasons),
        "red_flags": red_flags,
    }
