"""Three-engine fusion decision algorithm (Wyckoff + Ichimoku + ICT).

Strict implementation of the fusion decision:
  1. Direction consensus vote
  2. Position check + entry-zone computation
  3. Invalidation conditions
  4. Confidence (1-10)
  5. Four-layer score (L1 structure / L2 trend filter / L3 ICT direction / L4 position)

Pure function: takes three-system signals, returns a standardized plan.
All prices are rounded half-up to integers.

Strings are English so console output never garbles (Windows GBK vs UTF-8).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

# direction / filter vocabulary
LONG = "long"
SHORT = "short"
WAIT = "wait"
# Wyckoff phase vocabulary (bullish / bearish / neutral)
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
    # Wyckoff
    wyckoff_phase: str = NEUTRAL           # accumulation/markup/distribution/markdown/neutral
    wyckoff_valid: bool = True
    key_level: float = 0.0                 # key invalidation level
    # Ichimoku
    ichimoku_filter: str = WAIT            # long / short / wait
    cloud_bottom: float = 0.0
    cloud_top: float = 0.0
    # ICT
    ict_direction: str = WAIT              # long / short / wait
    price_in_premium: bool = False
    wants_premium_for_short: bool = True


@dataclass
class FusionPlan:
    direction: str = WAIT
    lean: str = NEUTRAL                    # bullish / bearish / neutral
    position_state: str = "wait for position"   # wait for position / can enter
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
    """Round half up; +1e-9 absorbs float error (e.g. 100*1.025=102.4999...)."""
    return int(math.floor(v + 0.5 + 1e-9))


def decide(inp: FusionInput) -> FusionPlan:
    """Execute the fusion decision, return a FusionPlan."""
    p = FusionPlan()

    # ---- 1. direction consensus vote ----
    vl = vs = 0
    if inp.ichimoku_filter == LONG:
        vl += 1
    elif inp.ichimoku_filter == SHORT:
        vs += 1
    if inp.ict_direction == LONG:
        vl += 1
    elif inp.ict_direction == SHORT:
        vs += 1
    if inp.wyckoff_valid:
        if inp.wyckoff_phase in BULLISH_PHASES:
            vl += 1
        elif inp.wyckoff_phase in BEARISH_PHASES:
            vs += 1
    if vs > vl:
        direction = SHORT
    elif vl > vs:
        direction = LONG
    else:
        direction = WAIT
    p.direction = direction

    if direction == WAIT:
        p.lean = NEUTRAL
        p.position_state = "wait"
        p.invalidations = ["no consensus among the three systems, stay flat"]
        p.confidence = 5
        p.layers = {"L1_structure": ("PASS" if inp.wyckoff_valid else "FAIL"),
                    "L2_trend_filter": "FAIL",
                    "L3_ict_direction": "FAIL",
                    "L4_position": "FAIL"}
        p.summary = "no consensus among the three systems; stay flat."
        return p

    # ---- 2. position check + entry zone ----
    immediate = True
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

    # ---- 3. invalidation ----
    inv = []
    if direction == SHORT:
        inv.append(f"close above {_round(inp.cloud_top)} invalidates short")
        if not inp.wyckoff_valid:
            inv.append(f"broke below {_round(inp.key_level)}, switch to breakdown logic")
    else:
        inv.append(f"close below {_round(inp.cloud_bottom)} invalidates long")
    p.invalidations = inv

    # ---- 4. confidence ----
    conf = 5
    if inp.ichimoku_filter == direction:
        conf += 2
    if inp.ict_direction == direction:
        conf += 1
    if inp.wyckoff_valid and (
        (direction == LONG and inp.wyckoff_phase in BULLISH_PHASES)
        or (direction == SHORT and inp.wyckoff_phase in BEARISH_PHASES)
    ):
        conf += 1
    elif (not inp.wyckoff_valid) or (
        (direction == LONG and inp.wyckoff_phase in BEARISH_PHASES)
        or (direction == SHORT and inp.wyckoff_phase in BULLISH_PHASES)
    ):
        conf -= 1
    if not immediate:
        conf = min(conf, 6)
    p.confidence = max(1, min(10, conf))

    # ---- 5. four-layer score ----
    p.layers = {
        "L1_structure": "PASS" if inp.wyckoff_valid else "FAIL",
        "L2_trend_filter": "PASS" if inp.ichimoku_filter == direction else "FAIL",
        "L3_ict_direction": "PASS" if inp.ict_direction == direction else "FAIL",
        "L4_position": "PASS" if immediate else "FAIL",
    }

    # ---- summary ----
    parts = [f"direction consensus {direction}"]
    if not immediate:
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
            "    1. no consensus among the three systems, stay flat",
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
