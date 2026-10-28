#!/usr/bin/env python3
"""Validate an ICT trade card before it is shown to a user.

Usage:
    python validate_plan.py output/trade_plan.json

This is deliberately stricter than JSON Schema: a TRADE card must contain a
finite stop, target, R multiple and a reachable entry.  It does not decide
whether the trade is good; it only rejects malformed / stale cards.
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path


def _finite(value) -> bool:
    return isinstance(value, (int, float)) and math.isfinite(float(value))


def validate(card: dict, max_entry_distance_atr: float = 0.5) -> list[str]:
    errors: list[str] = []
    decision = card.get("decision")
    if decision not in {"TRADE", "NO_TRADE"}:
        errors.append("decision must be TRADE or NO_TRADE")
        return errors
    if not card.get("symbol"):
        errors.append("symbol is required")
    if not card.get("last_time") and not card.get("as_of"):
        errors.append("last_time/as_of is required")

    rejected = card.get("rejected")
    if decision == "NO_TRADE":
        if card.get("setup") is not None:
            errors.append("NO_TRADE must not carry a setup")
        if not rejected and not card.get("reason"):
            errors.append("NO_TRADE needs rejected or reason")
        return errors

    if rejected:
        errors.append(f"TRADE cannot carry rejected={rejected!r}")
    setup = card.get("setup")
    if not isinstance(setup, dict):
        errors.append("TRADE requires setup")
        return errors

    for key in ("direction", "entry_reference", "stop", "target", "r_multiple"):
        if key not in setup:
            errors.append(f"setup.{key} is required")
    direction = setup.get("direction")
    if direction not in {"bull", "bear"}:
        errors.append("setup.direction must be bull or bear")
    for key in ("entry_reference", "stop", "target", "r_multiple"):
        if key in setup and not _finite(setup[key]):
            errors.append(f"setup.{key} must be finite")
    if errors:
        return errors

    entry = float(setup["entry_reference"])
    stop = float(setup["stop"])
    target = float(setup["target"])
    rr = float(setup["r_multiple"])
    if direction == "bull" and not (stop < entry < target):
        errors.append("bull setup must satisfy stop < entry < target")
    if direction == "bear" and not (target < entry < stop):
        errors.append("bear setup must satisfy target < entry < stop")
    if rr < 1.5:
        errors.append(f"gross R multiple {rr:.2f} is below 1.5")
    dist = card.get("entry_distance_atr")
    if dist is not None and _finite(dist) and float(dist) > max_entry_distance_atr:
        errors.append(
            f"entry_distance_atr {float(dist):.2f} > {max_entry_distance_atr:.2f} (stale entry)"
        )
    if not card.get("recent_sweeps"):
        errors.append("TRADE should include recent_sweeps evidence")
    return errors


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 1:
        print("usage: validate_plan.py PLAN.json", file=sys.stderr)
        return 2
    path = Path(argv[0])
    try:
        card = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        print(f"cannot read {path}: {exc}", file=sys.stderr)
        return 2
    errors = validate(card)
    result = {"ok": not errors, "errors": errors, "decision": card.get("decision")}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
