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

from .strategy import net_rr

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
    return net_rr(entry, sl, tp, direction)


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
def _recent_sweep(res, max_age: int | None, at: int | None):
    """Latest sweep, optionally bounded to the last ``max_age`` bars.

    Without the bound a sweep from weeks ago is used as the "current" trigger,
    so the card shows a plan for a level the market left long ago.
    """
    sweeps = list(res.sweeps)
    if not sweeps:
        return None
    s = sweeps[-1]
    if max_age is not None and at is not None and (at - int(s.reclaim_bar)) > max_age:
        return None
    return s


def _plan_b(audit: dict, res, atr: float, price: float, min_rr: float,
            max_sweep_age: int | None = None, at: int | None = None) -> dict | None:
    # needs: L2-08 PDA touch + L2-04 sweep + L1-04 valid MSS
    if not (_passed(audit, "L2-08") and _passed(audit, "L2-04") and _passed(audit, "L1-04")):
        return None
    s = _recent_sweep(res, max_sweep_age, at)
    if s is None:
        return None
    # MSS direction follows the sweep (bull sweep -> long reversal, bear -> short)
    direction = "long" if s.direction == "bull" else "short"
    # entry: retest of the POI edge (use the swept level as the retest zone)
    entry = float(s.level)
    # SL: sweep extreme +/- 0.1 ATR
    sl = (float(s.sweep_price) - 0.1 * atr) if direction == "long" else (float(s.sweep_price) + 0.1 * atr)
    # TP: nearest opposite-side unswept pool (DOL)
    tp = _dol(res, entry, direction, at)
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
def _plan_c(audit: dict, res, atr: float, price: float, min_rr: float,
            max_sweep_age: int | None = None, at: int | None = None) -> dict | None:
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
    tp = _dol(res, entry, direction, at)
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
def _plan_d(audit: dict, res, atr: float, price: float, min_rr: float,
            max_sweep_age: int | None = None, at: int | None = None) -> dict | None:
    # needs: no strong trend + a recent sweep with NO MSS
    if _passed(audit, "L1-04"):
        return None  # a valid MSS means it is not a fakeout
    l5 = _rule(audit, "L5-03")
    if l5 and l5.get("status") == "PASS":
        return None  # strong trend -> not a range fakeout
    s = _recent_sweep(res, max_sweep_age, at)
    if s is None:
        return None
    # fade the fakeout: bull sweep (fake breakdown) -> long, bear -> short
    direction = "long" if s.direction == "bull" else "short"
    entry = float(s.level)
    sl = (float(s.sweep_price) - 0.1 * atr) if direction == "long" else (float(s.sweep_price) + 0.1 * atr)
    tp = _dol(res, entry, direction, at)
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


def _dol(res, price: float, direction: str, at: int | None = None) -> float | None:
    """Highest-liquidity unswept pool on the target side.

    ``at`` restricts the pool to levels that were already known at that bar:
    a level confirmed after the decision bar is future data and cannot be a
    draw on liquidity.  Levels are ranked by liquidity quality first, then by
    distance, so price is drawn to the EQH/EQL stop cluster, not the nearest
    arbitrary swing.
    """
    levels = [k for k in res.key_levels
              if k.sweep_bar is None and k.broken_at is None
              and (at is None or getattr(k, "known_at", 0) <= at)]
    side = [k for k in levels if (k.price > price if direction == "long" else k.price < price)]
    if not side:
        return None
    side.sort(key=lambda k: (-getattr(k, "quality", 0.0), abs(k.price - price)))
    return float(side[0].price)


# --------------------------------------------------------------------------
# Evaluator
# --------------------------------------------------------------------------
def evaluate_scenarios(audit: dict, po3, res, atr: float, price: float,
                       min_net_rr: float = 2.5,
                       max_sweep_age: int | None = 15,
                       at: int | None = None,
                       po3_veto: bool = True,
                       po3_veto_scenarios: tuple[str, ...] = ("A", "B"),
                       min_risk_atr: float = 0.5) -> tuple[str | None, ScenarioPlan | None]:
    """Return (scenario_id, plan) honouring priority A > B > C > D.

    Only a *live* plan can win: a candidate whose status starts with
    ``REJECTED`` is demoted behind every live candidate instead of suppressing
    it (BTC 15m: 141 of 220 sampled windows published a rejected plan while
    suppressing a valid lower-priority one).  When ``po3_veto`` is set the
    PO3 INVALID state blocks every scenario, matching ``inject_po3_to_plan``.
    ``at`` is the decision bar index inside ``res.bars``; it bounds the sweep
    age and the DOL levels to what was known at that bar.

    Scenario A's ``cisd_confirmed`` flag is derived here from ``res.cisds``
    (nothing in the engine set it), and a CISD against the distribution
    cancels A.  ``min_risk_atr`` rejects a plan whose stop is too tight to be
    executable (``entry_invalid``).
    """
    # The PO3 veto belongs to the PO3-derived scenarios only.  C (trend
    # continuation) and D (range fakeout) describe the regime where there is no
    # clean AMD cycle at all, so vetoing them on INVALID would silence the very
    # setup they exist for (28 of 29 sampled BTC windows were D on an INVALID
    # PO3 day).
    po3_invalid = (po3 is not None and getattr(po3, "state", None) == "INVALID")
    if po3 is not None and getattr(po3, "state", None) == "DISTRIBUTING":
        expected = "bull" if po3.sweep_direction == "bull" else "bear"
        end = at if at is not None else len(res.bars) - 1
        d = int(po3.distribution_bar or 0)
        later = [c for c in res.cisds if d < int(c.end) <= end]
        if any(c.direction == expected for c in later):
            po3.cisd_confirmed = True
        elif later:
            # A CISD against the distribution means the retest failed: cancel
            # the plan instead of waiting to be stopped out inside the range.
            return None, None
    candidates: list[dict] = []
    for fn in (_plan_a, _plan_b, _plan_c, _plan_d):
        try:
            if fn is _plan_a:
                p = fn(po3, atr, min_net_rr)
            else:
                p = fn(audit, res, atr, price, min_net_rr,
                       max_sweep_age=max_sweep_age, at=at)
        except Exception:
            p = None
        if p:
            if po3_veto and po3_invalid and p.get("scenario") in po3_veto_scenarios:
                continue
            risk = abs(float(p["entry"]) - float(p["sl"]))
            if min_risk_atr > 0 and atr > 0 and risk < min_risk_atr * atr:
                p["status"] = "REJECTED_ENTRY_INVALID"
                p["reason"] = (f"stop {risk / atr:.2f} ATR < {min_risk_atr:.2f} ATR floor "
                               f"({p.get('reason', '')})")
            candidates.append(p)
    if not candidates:
        return None, None
    live = [p for p in candidates if not str(p.get("status", "")).startswith("REJECTED")]
    winner = live[0] if live else candidates[0]
    winner["suppressed"] = [p["scenario"] for p in candidates if p is not winner]
    return winner["scenario"], ScenarioPlan(**{k: v for k, v in winner.items() if k in ScenarioPlan.__dataclass_fields__})
