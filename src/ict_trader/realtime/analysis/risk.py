"""Dynamic ATR/structural risk model. Pure functions, no broker.

P0-04 fail-closed contract: a refusal is a ``RiskPlan`` whose ``blockers`` name
the reason, and a refused plan carries **no size and no target ladder**.  A stop
is only ever computed from a structural anchor; when no anchor exists the plan
refuses with ``NT_STOP_ANCHOR_UNRESOLVED`` instead of inventing a ``1xATR`` stop
(§1.5, X3, X18, §12).

P0-06 stop policy: there is one declared policy (``STOP_POLICY``) and one price
constructor (``stop_from_anchor``).  The policy resolves **one** anchor in a
declared precedence order; it never unions every available anchor (RK-06,
[RE:C-04]).  The buffer is non-negative and the wired CISD stop is built from
the pivot plus that buffer instead of sitting on the pivot (RK-01, RK-04).

P0-05 target ladder: ``RiskPlan.targets`` carries the **full** admissible ladder
(nearest first).  It is never sliced, because the target count is a declared
ladder mode fork (1/2/3/5/8, [RE:C-17]), not a constant of this module.  The RR
gate is computed from the *nearest* target only, so a far target cannot be used
to pass the gate on a nearer setup (TG-07).

``STOP_POLICY``/``stop_from_anchor`` live in :mod:`ict_trader.cisd` because that
is the wired call site and the backtest must remain importable without the
optional realtime stack; they are re-exported here as the public risk API.

P0-18 accounting: this module carries no private RR formula, cost basis or
``risk_pct`` default.  It reads the one declared cost model and the declared net
RR floor from :mod:`ict_trader.strategy`, exactly like the backtest and the
broker, so ``plan.net_rr`` and the plan card are the same quantity.
"""
from __future__ import annotations

from dataclasses import dataclass

from ict_trader.cisd import (  # re-exported: the one declared stop policy
    STOP_POLICY,
    StopPolicy,
    stop_from_anchor,
)
from ict_trader.strategy import (
    DEFAULT_COST_MODEL,
    DEFAULT_RISK_PCT,
    NET_RR_FLOOR,
    CostModel,
)

#: §12 code: the stop policy produced no usable anchor (fail-closed, §1.5, A3).
STOP_ANCHOR_UNRESOLVED = "NT_STOP_ANCHOR_UNRESOLVED"

#: The declared repo net-RR blocker name (net RR is repo, RE:RM-16).  The
#: numeric suffix is derived from the one declared floor, never typed twice.
NET_RR_BLOCKER = f"net_rr_below_{NET_RR_FLOOR:g}"


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


def _select_stop_anchor(direction: str, sweep: dict | None,
                        structure: dict | None, ob: dict | None,
                        policy: StopPolicy) -> float:
    """Resolve exactly one anchor, in the policy's declared precedence order."""
    for name in policy.anchors:
        if name == "sweep_extreme":
            if not sweep:
                continue
            # ``level`` is deliberately not an anchor: it is the swept level,
            # which RK-01/RK-07 name as the misplaced stop.
            extreme = sweep.get("extreme", sweep.get("sweep_price"))
            if extreme is not None:
                return float(extreme)
        elif name == "structure":
            if structure and structure.get("price") is not None:
                return float(structure["price"])
        elif name == "ob_extreme":
            if not ob:
                continue
            edges = [float(ob[k]) for k in ("bottom", "top")
                     if ob.get(k) is not None]
            if edges:
                return min(edges) if direction == "long" else max(edges)
    raise StopAnchorUnresolved(
        "no structural stop anchor (sweep extreme/ob/structure) is available")


def structural_stop(direction: str, entry: float, atr: float,
                    sweep: dict | None, structure: dict | None,
                    ob: dict | None = None, *,
                    pad_atr: float | None = None,
                    floor_atr: float | None = None,
                    policy: StopPolicy | None = None) -> float:
    """Structural stop beyond the *one* anchor selected by ``STOP_POLICY``.

    The policy declares an ordered anchor precedence (sweep extreme -> structure
    -> OB extreme) and a non-negative ATR buffer.  Unlike the previous
    implementation, every available anchor is **not** unioned into one ``min``/
    ``max`` set: with more than one anchor present only the first is used, so the
    result is reproducible from the declaration (RK-06, [RE:C-04]).

    A sweep payload with only ``level`` (the swept level) has no anchor and is a
    refusal: RK-01/RK-07 place the stop beyond the wick, never on the level that
    was swept.

    ``pad_atr``/``floor_atr`` remain accepted for API compatibility and override
    ``policy.buffer_atr``/``policy.floor_atr`` when supplied.  The floor is the
    repo's labelled minimum-distance widening; it is applied after the buffer,
    only when an entry is known (P0-06, conflict #4).

    The widening parameter is named ``floor_atr``, matching
    ``StopPolicy.floor_atr``.  ``SetupConfig.min_stop_atr`` is a *different*
    object -- an admission filter that **skips** a setup whose stop is tighter
    than the declared minimum -- so it does not carry this name (P0-20,
    RE:C-14).

    No anchor at all is a refusal, not a default: ``StopAnchorUnresolved`` is
    raised instead of falling back to ``entry -/+ 1xATR``, which would author a
    price the corpus never provides (§1.5, X3, X18).
    """
    p = policy if policy is not None else STOP_POLICY
    anchor = _select_stop_anchor(direction, sweep, structure, ob, p)
    stop = stop_from_anchor(direction, anchor, atr, policy=p, buffer_atr=pad_atr)

    floor_atr = p.floor_atr if floor_atr is None else floor_atr
    floor = max(float(floor_atr), 0.0) * max(float(atr), 0.0)
    if floor > 0:
        # Declared repo widening, not a corpus value (RK-04).  It only ever
        # moves the stop farther from the entry.
        if direction == "long":
            stop = min(stop, float(entry) - floor)
        else:
            stop = max(stop, float(entry) + floor)
    return stop


def build_risk_plan(direction: str, entry: float, atr: float,
                    sweep: dict | None, structure: dict | None,
                    pools: list[float], *, equity: float = 100_000,
                    risk_pct: float = DEFAULT_RISK_PCT,
                    cost: CostModel | None = None) -> RiskPlan:
    """Build a fail-closed plan: any blocker means zero size, empty ladder.

    The net RR is computed by the one shared :class:`CostModel` (P0-18) and
    compared with the declared ``NET_RR_FLOOR``; the blocker name is derived
    from that floor.  There is no private ``fee_bps`` basis here.
    """
    model = DEFAULT_COST_MODEL if cost is None else cost
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
    gross, net = model.net_rr(entry, stop, tp1, direction)
    if net < NET_RR_FLOOR:
        blockers.append(NET_RR_BLOCKER)
    if blockers:
        # Fail-closed: a blocked plan is never sized and never carries a ladder.
        return RiskPlan(stop, (), gross, net, 0.0, sl_atr, tuple(blockers))
    qty = (equity * risk_pct) / risk
    # The full admissible ladder, nearest first; the ladder mode - not this
    # module - decides how many rungs a plan uses (P0-05, [RE:C-17]).
    return RiskPlan(stop, tuple(valid), gross, net, qty, sl_atr, ())
