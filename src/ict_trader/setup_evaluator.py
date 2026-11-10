"""Evaluate the registered ICT playbooks against one evidence snapshot.

No market data is fetched here and no gate is relaxed.  A caller supplies
already-computed, closed-bar evidence; the evaluator returns a candidate or a
set of explicit blockers.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .setup_rules import ICTSetupRule, active_rules


@dataclass(slots=True)
class Candidate:
    rule_id: str
    direction: str
    entry_low: float
    entry_high: float
    stop: float
    targets: list[float]
    rr: float
    evidence: dict[str, Any] = field(default_factory=dict)
    blockers: list[str] = field(default_factory=list)


def _bias_ok(features: dict, direction: str) -> bool:
    want = "bullish" if direction == "long" else "bearish"
    htf = features.get("htf_bias") or {}
    for tf in ("4h", "1D"):
        item = htf.get(tf)
        if item and item.get("bias") not in (None, "", "neutral") and item["bias"] != want:
            return False
    return True


def _zone(boxes: list[dict], direction: str, price: float) -> dict | None:
    want = "bullish" if direction == "long" else "bearish"
    usable = [b for b in boxes if b.get("direction") == want]
    if not usable:
        return None
    return min(usable, key=lambda b: abs(((b["top"] + b["bottom"]) / 2) - price))


def evaluate(rule: ICTSetupRule, features: dict, *, now: datetime,
             min_rr: float = 2.0, atr: float = 0.0) -> Candidate | None:
    direction = features.get("direction")
    if direction not in ("long", "short"):
        return None
    blockers: list[str] = []
    if not _bias_ok(features, direction):
        blockers.append("htf_bias_conflict")
    if rule.windows_et != () and not any(
        r.rule_id == rule.rule_id for r in active_rules(now)
    ):
        blockers.append("outside_time_window")
    if rule.require_sweep and not features.get("sweep"):
        blockers.append("missing_sweep")
    if rule.require_mss and (features.get("structure") or {}).get("event") != "MSS":
        blockers.append("missing_mss")
    if rule.require_breaker and not features.get("breaker"):
        blockers.append("missing_breaker")

    price = float(features.get("price") or 0.0)
    boxes = features.get("fvg") or []
    if rule.entry in ("ob", "ote"):
        boxes = features.get("ob") or []
    if rule.entry == "breaker":
        boxes = features.get("breaker") or []
    zone = _zone(boxes, direction, price) if price else None
    if rule.require_fvg and not (features.get("fvg") or []):
        blockers.append("missing_fvg")
    if rule.require_ob and not (features.get("ob") or []):
        blockers.append("missing_ob")
    if zone is None:
        blockers.append("missing_entry_zone")
        return Candidate(rule.rule_id, direction, 0, 0, 0, [], 0, {}, blockers)

    entry_low, entry_high = sorted((float(zone["bottom"]), float(zone["top"])))
    entry = (entry_low + entry_high) / 2
    sweep = features.get("sweep") or {}
    if sweep and sweep.get("level") is not None:
        buffer = max(atr * 0.25, 0.0)
        stop = float(sweep["level"]) - buffer if direction == "long" else float(sweep["level"]) + buffer
    else:
        stop = entry_low - max(atr, 0.0) if direction == "long" else entry_high + max(atr, 0.0)
    targets = [float(x) for x in (features.get("targets") or [])]
    if not targets:
        blockers.append("missing_target")
        return Candidate(rule.rule_id, direction, entry_low, entry_high, stop, [], 0, {}, blockers)
    risk = abs(entry - stop)
    reward = abs(targets[0] - entry)
    rr = reward / risk if risk > 0 else 0.0
    if rr < min_rr:
        blockers.append("rr_below_min")
    if blockers:
        return Candidate(rule.rule_id, direction, entry_low, entry_high, stop, targets, rr, {}, blockers)
    return Candidate(rule.rule_id, direction, entry_low, entry_high, stop, targets, rr,
                     {"rule": rule.rule_id, "entry": entry})
