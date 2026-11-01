"""Four core ICT intraday scenarios, layered on GS/L rules + PO3 state machine.

Priority (mutually exclusive by construction): A > B > C > D.

  A  PO3 Distribution Retest   (PO3 派发回踩)
  B  Key POI Reversal          (关键 POI 反转)
  C  Consecutive FVG Breakout  (连续失衡破位)
  D  Range Boundary Fakeout    (区间边缘假突破)

Each scenario returns a plan with entry/sl/tp/status/net_rr, or None.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict

SCENARIO_NAMES = {
    "A": "PO3 Distribution Retest",
    "B": "Key POI Reversal",
    "C": "Consecutive FVG Breakout",
    "D": "Range Boundary Fakeout",
}
SCENARIO_COLORS = {"A": "#1E88E5", "B": "#8E24AA", "C": "#FB8C00", "D": "#9E9E9E"}


@dataclass
class ScenarioPlan:
    scenario: str
    direction: str
    entry: float
    sl: float
    tp: float
    status: str                  # TRIGGERED | CONDITIONAL | REJECTED_LOW_RR
    gross_rr: float | None = None
    net_rr: float | None = None
    fill_probability: str | None = None   # "low" for C
    suppressed: list[str] = field(default_factory=list)
    reason: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


def _net_rr(entry: float, sl: float, tp: float, direction: str) -> tuple[float | None, float | None]:
    risk = (entry - sl) if direction == "long" else (sl - entry)
    reward = (tp - entry) if direction == "long" else (entry - tp)
    if risk <= 0 or reward <= 0:
        return None, None
    cost = entry * 0.0007 + tp * 0.0007
    gross = reward / risk
    net = (reward - cost) / (risk + cost)
    return gross, net


def _rule(audit: dict, rid: str) -> dict | None:
    for r in audit.get("rules", []):
        if r.get("rule_id") == rid:
            return r
    return None


def _passed(audit: dict, rid: str) -> bool:
    r = _rule(audit, rid)
    return bool(r and r.get("status") == "PASS")


# --------------------------------------------------------------------------
# Scenario A: PO3 Distribution Retest
# --------------------------------------------------------------------------
def _plan_a(po3, atr: float, min_rr: float) -> dict | None:
    if po3 is None or po3.state != "DISTRIBUTING" or po3.sweep_direction is None:
        return None
    if None in (po3.range_high, po3.range_low, po3.manipulation_extreme):
        return None
    rng = abs(po3.range_high - po3.range_low)
    if po3.sweep_direction == "bull":
        direction, entry = "long", po3.range_high
        sl = po3.manipulation_extreme - 0.1 * atr
        tp = entry + 2 * rng
    else:
        direction, entry = "short", po3.range_low
        sl = po3.manipulation_extreme + 0.1 * atr
        tp = entry - 2 * rng
    gross, net = _net_rr(entry, sl, tp, direction)
    status = "TRIGGERED" if po3.cisd_confirmed else "CONDITIONAL"
    if net is not None and net < min_rr:
        status = "REJECTED_LOW_RR"
    return {
        "scenario": "A", "direction": direction, "entry": round(entry, 6),
        "sl": round(sl, 6), "tp": round(tp, 6), "status": status,
        "gross_rr": None if gross is None else round(gross, 3),
        "net_rr": None if net is None else round(net, 3),
        "reason": "PO3 distribution retest",
    }


# --------------------------------------------------------------------------
# Scenario B: Key POI Reversal (HTF FVG/OB + sweep + MSS)
# --------------------------------------------------------------------------
def _plan_b(audit: dict, res, atr: float, price: float, min_rr: float) -> dict | None:
    # needs: L2-08 PDA touch + L2-04 sweep + L1-04 valid MSS
    if not (_passed(audit, "L2-08") and _passed(audit, "L2-04") and _passed(audit, "L1-04")):
        return None
    sweeps = list(res.sweeps)
    if not sweeps:
        return None
    s = sweeps[-1]
    # MSS direction follows the sweep (bull sweep -> long reversal, bear -> short)
    direction = "long" if s.direction == "bull" else "short"
    # entry: retest of the POI edge (use the swept level as the retest zone)
    entry = float(s.level)
    # SL: sweep extreme +/- 0.1 ATR
    sl = (float(s.sweep_price) - 0.1 * atr) if direction == "long" else (float(s.sweep_price) + 0.1 * atr)
    # TP: nearest opposite-side unswept pool (DOL)
    tp = _dol(res, entry, direction)
    if tp is None:
        return None
    gross, net = _net_rr(entry, sl, tp, direction)
    status = "TRIGGERED"
    if net is not None and net < min_rr:
        status = "REJECTED_LOW_RR"
    return {
        "scenario": "B", "direction": direction, "entry": round(entry, 6),
        "sl": round(sl, 6), "tp": round(tp, 6), "status": status,
        "gross_rr": None if gross is None else round(gross, 3),
        "net_rr": None if net is None else round(net, 3),
        "reason": "key POI reversal (sweep+MSS)",
    }


# --------------------------------------------------------------------------
# Scenario C: Consecutive FVG Breakout (strong trend, no pullback)
# --------------------------------------------------------------------------
def _plan_c(audit: dict, res, atr: float, price: float, min_rr: float) -> dict | None:
    # needs: L3-01 FVG present + HTF strong (L5-03 PASS)
    if not _passed(audit, "L3-01"):
        return None
    l5 = _rule(audit, "L5-03")
    if not (l5 and l5.get("status") == "PASS"):
        return None
    direction = l5.get("value", {}).get("direction")
    if direction not in ("long", "short"):
        return None
    # nearest active FVG on the trend side
    fvgs = [f for f in res.fvgs if getattr(f, "active", False)]
    if direction == "long":
        below = [f for f in fvgs if float(f.bottom) <= price]
        f = max(below, key=lambda x: float(x.bottom)) if below else None
        if f is None:
            return None
        entry = (float(f.bottom) + float(f.top)) / 2  # 50% CE
        sl = float(f.bottom)
    else:
        above = [f for f in fvgs if float(f.top) >= price]
        f = min(above, key=lambda x: float(x.top)) if above else None
        if f is None:
            return None
        entry = (float(f.bottom) + float(f.top)) / 2
        sl = float(f.top)
    tp = _dol(res, entry, direction)
    if tp is None:
        return None
    gross, net = _net_rr(entry, sl, tp, direction)
    status = "CONDITIONAL"
    if net is not None and net < min_rr:
        status = "REJECTED_LOW_RR"
    return {
        "scenario": "C", "direction": direction, "entry": round(entry, 6),
        "sl": round(sl, 6), "tp": round(tp, 6), "status": status,
        "gross_rr": None if gross is None else round(gross, 3),
        "net_rr": None if net is None else round(net, 3),
        "fill_probability": "low",
        "reason": "consecutive FVG breakout (chase, low fill prob)",
    }


# --------------------------------------------------------------------------
# Scenario D: Range Boundary Fakeout (lowest priority)
# --------------------------------------------------------------------------
def _plan_d(audit: dict, res, atr: float, price: float, min_rr: float) -> dict | None:
    # needs: no strong trend + a recent sweep with NO MSS
    if _passed(audit, "L1-04"):
        return None  # a valid MSS means it is not a fakeout
    l5 = _rule(audit, "L5-03")
    if l5 and l5.get("status") == "PASS":
        return None  # strong trend -> not a range fakeout
    sweeps = list(res.sweeps)
    if not sweeps:
        return None
    s = sweeps[-1]
    # fade the fakeout: bull sweep (fake breakdown) -> long, bear -> short
    direction = "long" if s.direction == "bull" else "short"
    entry = float(s.level)
    sl = (float(s.sweep_price) - 0.1 * atr) if direction == "long" else (float(s.sweep_price) + 0.1 * atr)
    tp = _dol(res, entry, direction)
    if tp is None:
        return None
    gross, net = _net_rr(entry, sl, tp, direction)
    status = "CONDITIONAL"
    if net is not None and net < min_rr:
        status = "REJECTED_LOW_RR"
    return {
        "scenario": "D", "direction": direction, "entry": round(entry, 6),
        "sl": round(sl, 6), "tp": round(tp, 6), "status": status,
        "gross_rr": None if gross is None else round(gross, 3),
        "net_rr": None if net is None else round(net, 3),
        "reason": "range boundary fakeout",
    }


def _dol(res, price: float, direction: str) -> float | None:
    """Nearest unswept pool on the target side."""
    levels = [k for k in res.key_levels
              if k.sweep_bar is None and k.broken_at is None]
    side = [k for k in levels if (k.price > price if direction == "long" else k.price < price)]
    if not side:
        return None
    return float(min(side, key=lambda k: k.price).price) if direction == "long" else float(max(side, key=lambda k: k.price).price)


# --------------------------------------------------------------------------
# Evaluator
# --------------------------------------------------------------------------
def evaluate_scenarios(audit: dict, po3, res, atr: float, price: float,
                       min_net_rr: float = 2.5) -> tuple[str | None, ScenarioPlan | None]:
    """Return (scenario_id, plan) honouring priority A > B > C > D."""
    candidates: list[dict] = []
    for fn in (_plan_a, _plan_b, _plan_c, _plan_d):
        try:
            if fn is _plan_a:
                p = fn(po3, atr, min_net_rr)
            else:
                p = fn(audit, res, atr, price, min_net_rr)
        except Exception:
            p = None
        if p:
            candidates.append(p)
    if not candidates:
        return None, None
    winner = candidates[0]
    winner["suppressed"] = [p["scenario"] for p in candidates[1:]]
    return winner["scenario"], ScenarioPlan(**{k: v for k, v in winner.items() if k in ScenarioPlan.__dataclass_fields__})
