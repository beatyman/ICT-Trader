"""ICT-primary + Ichimoku/Wyckoff-auxiliary fusion decision.

Architecture (per user directive):
  PRIMARY   ICT        -> direction, entry, DOL, premium/discount, Grade
  AUXILIARY Ichimoku   -> cloud position / TK cross confirm the direction
  AUXILIARY Wyckoff    -> accumulation/distribution phase confirm the structure

Algorithm:
  1. Primary direction = ICT.direction (long / short / wait)
     - ICT wait -> stay flat (auxiliaries alone never trigger a trade)
  2. Auxiliary confirmation:
     - Ichimoku filter aligned -> +1, opposed -> -1, neutral -> 0
     - Wyckoff phase aligned (and valid) -> +1, opposed -> -1, neutral -> 0
  3. net = ichi_conf + wyck_conf; if net < 0 -> downgrade to wait (conflict)
  4. Position check + entry zone (premium/discount logic unchanged)
  5. Confidence = 5 + 1(ICT primary) + ichi_conf + wyck_conf, then caps
  6. Four-layer score (L1 structure / L2 trend filter / L3 ICT / L4 position)

All prices round half-up to integers. Strings are English (no console garble).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

LONG = "long"
SHORT = "short"
WAIT = "wait"
ACCUMULATION = "accumulation"
MARKUP = "markup"
DISTRIBUTION = "distribution"
MARKDOWN = "markdown"
NEUTRAL = "neutral"
BULLISH_PHASES = (ACCUMULATION, MARKUP)
BEARISH_PHASES = (DISTRIBUTION, MARKDOWN)


@dataclass
class FusionInput:
    current_price: float
    wyckoff_phase: str = NEUTRAL
    wyckoff_valid: bool = True
    key_level: float = 0.0
    ichimoku_filter: str = WAIT
    cloud_bottom: float = 0.0
    cloud_top: float = 0.0
    ict_direction: str = WAIT
    price_in_premium: bool = False
    wants_premium_for_short: bool = True
    chanlun_htf_direction: str = WAIT   # HTF 缠论方向（日线/周线），决定方向
    chanlun_ltf_bias: str = WAIT        # LTF 缠论（15m/1h）买卖点，只做入场择时


@dataclass
class FusionPlan:
    direction: str = WAIT
    lean: str = NEUTRAL
    position_state: str = "wait for position"
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
    return int(math.floor(v + 0.5 + 1e-9))


def _wait_plan(p: FusionPlan, layers: dict, summary: str) -> FusionPlan:
    p.direction = WAIT
    p.lean = NEUTRAL
    p.position_state = "wait"
    p.invalidations = [summary]
    p.confidence = 5
    p.layers = layers
    p.summary = summary + "."
    return p


def decide(inp: FusionInput) -> FusionPlan:
    """Execute the ICT-primary + auxiliary fusion decision."""
    p = FusionPlan()
    primary = inp.ict_direction

    # ---- 1. primary = ICT ----
    if primary == WAIT:
        return _wait_plan(
            p,
            {"L1_structure": "PASS" if inp.wyckoff_valid else "FAIL",
             "L2_trend_filter": "PASS" if inp.ichimoku_filter != WAIT else "FAIL",
             "L3_ict_direction": "FAIL",
             "L4_position": "FAIL"},
            "ICT has no direction; stay flat (auxiliaries never trigger a trade)",
        )

    # ---- 2. auxiliary confirmation ----
    ichi_conf = 0
    if inp.ichimoku_filter == primary:
        ichi_conf = 1
    elif inp.ichimoku_filter in (LONG, SHORT):
        ichi_conf = -1

    wy_conf = 0
    if inp.wyckoff_valid:
        wy_bull = inp.wyckoff_phase in BULLISH_PHASES
        wy_bear = inp.wyckoff_phase in BEARISH_PHASES
        if (primary == LONG and wy_bull) or (primary == SHORT and wy_bear):
            wy_conf = 1
        elif (primary == LONG and wy_bear) or (primary == SHORT and wy_bull):
            wy_conf = -1

    # 缠论：HTF 决定方向（与 Ichimoku/Wyckoff 同级），LTF 只做入场择时
    chan_htf_conf = 0
    if inp.chanlun_htf_direction == primary:
        chan_htf_conf = 1
    elif inp.chanlun_htf_direction in (LONG, SHORT):
        chan_htf_conf = -1

    chan_ltf_conf = 0
    if inp.chanlun_ltf_bias == primary:
        chan_ltf_conf = 1
    elif inp.chanlun_ltf_bias in (LONG, SHORT):
        chan_ltf_conf = -1

    net = ichi_conf + wy_conf + chan_htf_conf

    # ---- 3. final direction ----
    if net < 0:
        return _wait_plan(
            p,
            {"L1_structure": "PASS" if inp.wyckoff_valid else "FAIL",
             "L2_trend_filter": "PASS" if inp.ichimoku_filter == primary else "FAIL",
             "L3_ict_direction": "PASS",
             "L4_position": "FAIL"},
            f"ICT {primary} but both auxiliaries oppose; conflict, stay flat",
        )
    direction = primary
    p.direction = direction

    # ---- 4. position check + entry zone ----
    immediate = True
    if chan_ltf_conf == -1:
        # LTF 缠论未对齐（买卖点方向相反），只推迟入场，不翻转方向
        immediate = False
    if direction == SHORT:
        p.lean = "bearish"
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
    else:  # LONG
        p.lean = "bullish"
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
    p.position_state = "can enter" if immediate else "wait for position"

    # ---- 5. invalidation ----
    inv = []
    if direction == SHORT:
        inv.append(f"close above {_round(inp.cloud_top)} invalidates short")
        if not inp.wyckoff_valid:
            inv.append(f"broke below {_round(inp.key_level)}, switch to breakdown logic")
    else:
        inv.append(f"close below {_round(inp.cloud_bottom)} invalidates long")
    p.invalidations = inv

    # ---- 6. confidence ----
    conf = 5 + 1  # ICT is primary, always +1
    conf += ichi_conf
    conf += wy_conf
    conf += chan_htf_conf
    conf += chan_ltf_conf
    if not immediate:
        conf = min(conf, 6)
    p.confidence = max(1, min(10, conf))

    # ---- 7. four-layer score ----
    p.layers = {
        "L1_structure": "PASS" if inp.wyckoff_valid else "FAIL",
        "L2_trend_filter": "PASS" if inp.ichimoku_filter == direction else "FAIL",
        "L3_ict_direction": "PASS",
        "L4_position": "PASS" if immediate else "FAIL",
    }

    # ---- summary ----
    parts = [f"ICT primary {direction}"]
    if net > 0:
        parts.append(f"auxiliaries confirm (+{net})")
    elif net == 0:
        parts.append("auxiliaries neutral")
    if not immediate:
        if chan_ltf_conf == -1:
            parts.append("LTF chanlun not aligned, wait for entry")
        else:
            parts.append("position not ready, wait")
    p.summary = "; ".join(parts) + "."
    return p


def render(inp: FusionInput) -> str:
    """Render the fusion plan in the fixed output format (English)."""
    p = decide(inp)
    if p.direction == WAIT:
        return "\n".join([
            "[Fusion Trade Plan]",
            f"- Direction: {p.direction} (neutral, wait)",
            "- Entry zone: 0 - 0",
            "- Stop: 0",
            "- Target1: 0 (~1:2)",
            "- Target2: 0 (~1:3)",
            "- Invalidation:",
            f"    1. {p.invalidations[0] if p.invalidations else 'stay flat'}",
            f"- Confidence: {p.confidence}/10",
            "",
            f"- Summary: {p.summary}",
            "",
            "Four-layer score:",
            f"  L1_structure: {p.layers['L1_structure']}",
            f"  L2_trend_filter: {p.layers['L2_trend_filter']}",
            f"  L3_ict_direction: {p.layers['L3_ict_direction']}",
            f"  L4_position: {p.layers['L4_position']}",
        ])

    lines = [
        "[Fusion Trade Plan]",
        f"- Direction: {p.direction} ({p.lean}, {p.position_state})",
        f"- Entry zone: {p.entry_low} - {p.entry_high}",
        f"- Stop: {p.stop}",
        f"- Target1: {p.target1} (~1:2)",
        f"- Target2: {p.target2} (~1:3)",
        "- Invalidation:",
    ]
    for i, iv in enumerate(p.invalidations, 1):
        lines.append(f"    {i}. {iv}")
    lines += [
        f"- Confidence: {p.confidence}/10",
        "",
        f"- Summary: {p.summary}",
        "",
        "Four-layer score:",
        f"  L1_structure: {p.layers['L1_structure']}",
        f"  L2_trend_filter: {p.layers['L2_trend_filter']}",
        f"  L3_ict_direction: {p.layers['L3_ict_direction']}",
        f"  L4_position: {p.layers['L4_position']}",
    ]
    return "\n".join(lines)
