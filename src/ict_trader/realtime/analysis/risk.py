"""Dynamic ATR/structural risk model. Pure functions, no broker.

P0-04 fail-closed contract: a refusal is a ``RiskPlan`` whose ``blockers`` name
the reason, and a refused plan carries **no size and no target ladder**.  A stop
is only ever computed from a structural anchor; when no anchor exists the plan
refuses with ``NT_STOP_ANCHOR_UNRESOLVED`` instead of inventing a ``1xATR`` stop
(§1.5, X3, X18, §12).
"""
from __future__ import annotations

from dataclasses import dataclass

#: §12 code: the stop policy produced no usable anchor (fail-closed, §1.5, A3).
STOP_ANCHOR_UNRESOLVED = "NT_STOP_ANCHOR_UNRESOLVED"


class StopAnchorUnresolved(ValueError):
    """No structural anchor exists for the stop; refuse rather than fabricate."""

    code = STOP_ANCHOR_UNRESOLVED


@dataclass(frozen=True, slots=True)
class RiskPlan:
    stop: float | None  # None on refusal: no price is authored (§1.5, X1)
    targets: tuple[float, ...]
    gross_rr: float
    net_rr: float
    quantity: float
    sl_atr: float
    blockers: tuple[str, ...] = ()


def structural_stop(direction: str, entry: float, atr: float,
                    sweep: dict | None, structure: dict | None,
                    ob: dict | None = None, *, pad_atr: float = 0.25,
                    min_stop_atr: float = 1.0) -> float:
    """Structural stop beyond the *outermost* invalidation reference.

    Corpus anchors, all of which must sit beyond the level itself:
      * doc 69 L181 - "The stop belongs beyond the sweep's extreme, not at the
        swept level ... a retest of that wick can take a stop placed there"
      * doc 556 L116 - "stop loss 10-20 pips beyond the OB extreme"
      * doc 491 L158 - "beyond the FVG far edge or beyond the MSS swing extreme"

    The sweep anchor therefore uses ``extreme`` (the wick that took the stops)
    and only falls back to ``level`` for legacy payloads.  ``min_stop_atr`` keeps
    the repo's existing floor (a 1 ATR minimum) explicit instead of hardcoded.

    No anchor at all is a refusal, not a default: ``StopAnchorUnresolved`` is
    raised instead of falling back to ``entry -/+ 1xATR``, which would author a
    price the corpus never provides (§1.5, X3, X18).
    """
    buffer = max(pad_atr * atr, 0.0)
    refs: list[float] = []
    if sweep:
        level = sweep.get("level")
        extreme = sweep.get("extreme", sweep.get("sweep_price"))
        if level is not None:
            refs.append(float(level))
        if extreme is not None:
            refs.append(float(extreme))
    if ob:
        for key in ("bottom", "top"):
            if ob.get(key) is not None:
                refs.append(float(ob[key]))
    if structure and structure.get("price") is not None:
        refs.append(float(structure["price"]))
    if not refs:
        raise StopAnchorUnresolved(
            "no structural stop anchor (sweep/ob/structure) is available")
    if direction == "long":
        anchor = min(refs)
        return min(anchor - buffer, entry - max(min_stop_atr, 0.0) * atr)
    anchor = max(refs)
    return max(anchor + buffer, entry + max(min_stop_atr, 0.0) * atr)


def build_risk_plan(direction: str, entry: float, atr: float,
                    sweep: dict | None, structure: dict | None,
                    pools: list[float], *, equity: float = 100_000,
                    risk_pct: float = 0.005, fee_bps: float = 7.0) -> RiskPlan:
    """Build a fail-closed plan: any blocker means zero size, empty ladder."""
    if atr <= 0:
        return RiskPlan(None, (), 0, 0, 0, 0, ("atr_invalid",))
    try:
        stop = structural_stop(direction, entry, atr, sweep, structure)
    except StopAnchorUnresolved:
        return RiskPlan(None, (), 0, 0, 0, 0, (STOP_ANCHOR_UNRESOLVED,))
    sl_atr = abs(entry - stop) / atr
    blockers = []
    if not (1.0 <= sl_atr <= 3.0):
        blockers.append("sl_atr_out_of_bounds")
    valid = [float(p) for p in pools if (p > entry if direction == "long" else p < entry)]
    valid.sort(reverse=direction == "short")
    if not valid:
        return RiskPlan(stop, (), 0, 0, 0, sl_atr, tuple(blockers + ["no_liquidity_target"]))
    tp1 = valid[0] - 0.1 * atr if direction == "long" else valid[0] + 0.1 * atr
    risk = abs(entry - stop)
    gross = abs(tp1 - entry) / risk
    fees_r = (abs(entry) + abs(tp1)) * (fee_bps / 10_000.0) / risk
    net = gross - fees_r
    if net < 2.0:
        blockers.append("net_rr_below_2")
    if blockers:
        # Fail-closed: a blocked plan is never sized and never carries a ladder.
        return RiskPlan(stop, (), gross, net, 0.0, sl_atr, tuple(blockers))
    qty = (equity * risk_pct) / risk
    return RiskPlan(stop, tuple(valid[:3]), gross, net, qty, sl_atr, ())
