"""Deterministic ICT setup rule registry.

The nine playbooks share one evidence pipeline.  This module only defines
which evidence each playbook may consume and its New York time window; it
does not invent prices or relax any gate.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import time
from typing import Literal
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
Window = tuple[time, time]


@dataclass(frozen=True, slots=True)
class ICTSetupRule:
    rule_id: str
    name: str
    priority: int
    windows_et: tuple[Window, ...]
    require_sweep: bool
    require_mss: bool
    require_fvg: bool
    require_ob: bool
    require_breaker: bool
    require_pd: bool
    entry: Literal["fvg", "ob", "breaker", "ote", "sweep_reclaim"]


def _w(h: int, m: int = 0) -> time:
    return time(h, m)


RULES: tuple[ICTSetupRule, ...] = (
    ICTSetupRule("SB", "Silver Bullet", 1,
                 ((_w(3), _w(4)), (_w(10), _w(11)), (_w(14), _w(15))),
                 True, True, True, False, False, True, "fvg"),
    ICTSetupRule("M2022", "ICT 2022 Model", 2,
                 ((_w(0), _w(23, 59)),),
                 True, True, True, False, False, True, "fvg"),
    ICTSetupRule("JUDAS", "Judas Swing", 3,
                 ((_w(0), _w(5)),),
                 True, True, True, True, False, True, "fvg"),
    ICTSetupRule("TURTLE", "Turtle Soup", 4,
                 ((_w(0), _w(23, 59)),),
                 True, True, True, False, False, True, "fvg"),
    ICTSetupRule("SWEEP_FVG", "Liquidity Sweep + FVG", 5,
                 ((_w(0), _w(23, 59)),),
                 True, True, True, False, False, True, "fvg"),
    ICTSetupRule("OB", "Order Block Retest", 6,
                 ((_w(0), _w(23, 59)),),
                 False, False, True, True, False, True, "ob"),
    ICTSetupRule("BREAKER", "Breaker Block", 7,
                 ((_w(0), _w(23, 59)),),
                 False, False, False, False, True, True, "breaker"),
    ICTSetupRule("OTE", "Optimal Trade Entry", 8,
                 ((_w(0), _w(23, 59)),),
                 True, True, True, True, False, True, "ote"),
    ICTSetupRule("AMD", "Power of 3 / AMD", 9,
                 ((_w(0), _w(5)), (_w(8), _w(12))),
                 True, True, True, False, False, True, "fvg"),
)


def in_window(ts, rule: ICTSetupRule) -> bool:
    et = ts.astimezone(NY).time()
    return any(start <= et < end for start, end in rule.windows_et)


def active_rules(ts) -> list[ICTSetupRule]:
    return [r for r in sorted(RULES, key=lambda x: x.priority) if in_window(ts, r)]


def rule_by_id(rule_id: str) -> ICTSetupRule:
    for r in RULES:
        if r.rule_id == rule_id:
            return r
    raise KeyError(rule_id)

