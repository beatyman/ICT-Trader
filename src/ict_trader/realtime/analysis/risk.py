"""Risk algebra objects (P1-13): sizing, declarations, RR/expectancy targets.

This module is the realtime risk surface.  It builds a fail-closed ``RiskPlan``
from one declared stop policy, one declared RR mode, one declared risk band and
one declared minimum-distance floor.  No threshold is invented here:

* ``value_per_unit`` (I3) and ``lot_step`` (RK-14) are the two sizing inputs.
  ``value_per_unit`` comes from the instrument spec; ``lot_step`` is *missing*
  in the corpus and is declared as the repo no-rounding step in
  :mod:`ict_trader.strategy`.
* ``RR_MODE`` (V28) is one of the four attested gross-RR modes.  ``none`` makes
  the ratio an output, never a gate; every other mode gates admission.
* ``STOP_POLICY`` (V29) is the one anchor precedence + ATR buffer construction
  from :mod:`ict_trader.cisd`; this module re-exports it rather than keeping a
  second stop constructor.
* The risk band (V30) is one attested band with its ``source_id``; a sizing
  surface outside it refuses instead of scaling the trade.
* The minimum-distance floor (TG-11) is one of four incompatible attestations.
  The repo declares the ``>=3:1`` form; it is applied to the *nearest* target,
  so a farther pool can never be used to pass the distance gate (TG-07).
* A target is a ``(price, known_at)`` payload: a level known after the signal
  bar, or already swept/broken, is not admissible (TG-03, CB-07).

Weighted RR and expectancy are the corpus arithmetic from SYSTEM_SPEC §9.4.
A weighted-RR gate must carry an explicit hit-rate assumption; without one it
refuses with ``NT_DATA_MISSING`` instead of assuming the break-even rate.

P0-04 remains in force: a refused plan carries no size and no target ladder.
P0-18 remains in force: the (gross, net) pair comes from the one shared
:class:`~ict_trader.strategy.CostModel`; this module has no private fee basis.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Mapping, Sequence

from ict_trader.cisd import (  # re-exported: the one declared stop policy
    STOP_POLICY,
    StopPolicy,
    stop_from_anchor,
)
from ict_trader.strategy import (
    DEFAULT_COST_MODEL,
    DEFAULT_LOT_STEP,
    DEFAULT_LOT_STEP_POLICY,
    DEFAULT_MIN_DISTANCE_FLOOR,
    DEFAULT_RISK_BAND,
    DEFAULT_RISK_BAND_ID,
    DEFAULT_RISK_PCT,
    DEFAULT_RR_MODE,
    DEFAULT_VALUE_PER_UNIT,
    LOT_STEP_POLICIES,
    NET_RR_FLOOR,
    RR_MODE,
    RR_MODE_FLOORS,
    WEIGHTED_RR_FLOOR,
    CostModel,
    MinimumDistanceFloor,
    RiskBand,
    rr_floor,
    validate_risk_pct,
)

# --------------------------------------------------------------------------- #
# Declarations and §12 refusal codes
# --------------------------------------------------------------------------- #
#: §12 code: the stop policy produced no usable anchor (fail-closed, §1.5, A3).
STOP_ANCHOR_UNRESOLVED = "NT_STOP_ANCHOR_UNRESOLVED"

#: The declared repo net-RR blocker name (net RR is repo, RE:RM-16).  The
#: numeric suffix is derived from the one declared floor, never typed twice.
NET_RR_BLOCKER = f"net_rr_below_{NET_RR_FLOOR:g}"

#: §12 codes used by this module.  The strings are the typed vocabulary's
#: values (``ict_trader.variants.RefusalCode``); they are repeated here as
#: module constants so the risk surface has no behavioural import cycle.
NT_DATA_MISSING = "NT_DATA_MISSING"
NT_RR_BELOW_FLOOR = "NT_RR_BELOW_FLOOR"
NT_TARGET_TOO_NEAR = "NT_TARGET_TOO_NEAR"
NT_SIZE_UNSOLVABLE = "NT_SIZE_UNSOLVABLE"
NT_VARIANT_UNDECLARED = "NT_VARIANT_UNDECLARED"

#: The register rows this module reads (declared once in ``variants``).
RR_MODE_ID = "V28"
STOP_POLICY_ID = "V29"
RISK_BAND_ID = "V30"

#: ``STOP_POLICY`` is a V29 row: one anchor precedence and one ATR-buffer
#: construction.  The wrapper records the construction and its provenance
#: without editing :mod:`ict_trader.cisd` (which must stay importable without
#: the optional realtime stack).
STOP_POLICY_LABEL = "repo"
STOP_POLICY_SOURCE = "[RE:C-03/C-04]"
STOP_POLICY_CONSTRUCTION = "one anchor + declared ATR buffer + repo floor"


@dataclass(frozen=True, slots=True)
class StopPolicyDeclaration:
    """The declared V29 stop policy and the construction it names."""

    policy: StopPolicy
    construction: str
    source_id: str
    label: str = STOP_POLICY_LABEL
    variant_id: str = STOP_POLICY_ID

    def __post_init__(self) -> None:
        if not self.construction.strip():
            raise ValueError("the stop construction must be declared")
        if not self.source_id.strip():
            raise ValueError("the stop policy needs a source citation")


STOP_POLICY_DECLARATION = StopPolicyDeclaration(
    STOP_POLICY, STOP_POLICY_CONSTRUCTION, STOP_POLICY_SOURCE)


# --------------------------------------------------------------------------- #
# Target payload: (price, known_at)
# --------------------------------------------------------------------------- #
#: The lifecycle states a target level may carry (P1-08 ``ARRAY_STATES``).
TARGET_STATES = ("intact", "swept", "broken")


@dataclass(frozen=True, slots=True)
class TargetLevel:
    """One target as ``(price, known_at)`` plus its lifecycle state (TG-03).

    ``known_at`` is the bar index at which the level became knowable.  A level
    with no ``known_at`` cannot be proved admissible; a plan built with a
    ``signal_bar`` refuses it with ``NT_DATA_MISSING`` instead of assuming bar
    zero.  ``state`` must be ``intact``: a swept or broken level is not a
    forward target (TG-03).
    """

    price: float
    known_at: int | None = None
    state: str = "intact"
    tf: str = "base"

    def __post_init__(self) -> None:
        if not math.isfinite(float(self.price)):
            raise ValueError("target price must be finite")
        if self.known_at is not None and int(self.known_at) < 0:
            raise ValueError("known_at must be a non-negative bar index")
        if self.state not in TARGET_STATES:
            raise ValueError(
                f"unknown target state {self.state!r}; "
                f"declare one of {TARGET_STATES}")

    def is_known_at(self, signal_bar: int) -> bool:
        """CB-07: usable only once ``known_at <= signal_bar``."""
        return self.known_at is not None and int(self.known_at) <= int(signal_bar)

    def is_admissible(self, signal_bar: int) -> bool:
        """TG-03: known by the signal bar *and* still intact."""
        return self.state == "intact" and self.is_known_at(signal_bar)

    def as_pair(self) -> tuple[float, int | None]:
        """The canonical ``(price, known_at)`` payload."""
        return (float(self.price), self.known_at)

    def to_public(self) -> dict:
        return {
            "price": float(self.price),
            "known_at": self.known_at,
            "state": self.state,
            "tf": self.tf,
        }


def _coerce_target_level(pool: object) -> TargetLevel:
    """Coerce a price, a ``TargetLevel`` or a level object into a payload."""
    if isinstance(pool, TargetLevel):
        return pool
    if isinstance(pool, Mapping):
        price = pool.get("price", pool.get("level"))
        known_at = pool.get("known_at")
        state = pool.get("state")
        if state is None:
            state = ("broken" if pool.get("broken")
                     else "swept" if pool.get("swept") else "intact")
        tf = pool.get("tf", "base") or "base"
    else:
        price = getattr(pool, "price", getattr(pool, "level", pool))
        known_at = getattr(pool, "known_at", None)
        state = getattr(pool, "state", None)
        if state is None:
            state = ("broken" if getattr(pool, "broken", False)
                     else "swept" if getattr(pool, "swept", False)
                     else "intact")
        tf = getattr(pool, "tf", "base") or "base"
    if price is None:
        raise ValueError(f"target payload {pool!r} carries no price")
    return TargetLevel(
        float(price),
        None if known_at is None else int(known_at),
        str(state),
        str(tf),
    )


# --------------------------------------------------------------------------- #
# Plan
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class RiskPlan:
    """One fail-closed plan with its declared algebra and target payloads.

    ``targets`` keeps the legacy price-only ladder for callers that consumed
    ``RiskPlan`` before P1-13.  ``target_levels`` is the same ladder as
    ``(price, known_at)`` payloads.  Both are empty on a refusal (P0-04).
    """

    stop: float | None  # None on refusal: no price is authored (§1.5, X1)
    targets: tuple[float, ...]
    gross_rr: float
    net_rr: float
    quantity: float
    sl_atr: float
    blockers: tuple[str, ...] = ()
    target_levels: tuple[TargetLevel, ...] = ()
    rr_mode: str = DEFAULT_RR_MODE
    risk_band_id: str = DEFAULT_RISK_BAND_ID
    risk_pct: float = DEFAULT_RISK_PCT
    value_per_unit: float = DEFAULT_VALUE_PER_UNIT
    lot_step: float = DEFAULT_LOT_STEP
    min_distance_mode: str = DEFAULT_MIN_DISTANCE_FLOOR.mode

    @property
    def target_level(self) -> TargetLevel | None:
        """The nearest admissible target payload, if the plan has a ladder."""
        return self.target_levels[0] if self.target_levels else None

    @property
    def target_payloads(self) -> tuple[TargetLevel, ...]:
        """The full ladder as ``(price, known_at)`` payloads (TG-03)."""
        return self.target_levels

    @property
    def target(self) -> float | None:
        """The nearest admissible target price, if the plan has a ladder."""
        return self.targets[0] if self.targets else None


class StopAnchorUnresolved(ValueError):
    """No structural anchor exists for the stop; refuse rather than fabricate."""

    code = STOP_ANCHOR_UNRESOLVED


# --------------------------------------------------------------------------- #
# Stop construction (P0-06, re-exported V29)
# --------------------------------------------------------------------------- #
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


# --------------------------------------------------------------------------- #
# Sizing (I3 value_per_unit, RK-14 lot step)
# --------------------------------------------------------------------------- #
def round_to_lot_step(quantity: float, lot_step: float = DEFAULT_LOT_STEP, *,
                      policy: str = DEFAULT_LOT_STEP_POLICY) -> float:
    """Round a size to the declared lot step, **never upward** (RK-14).

    RK-14's lot step is missing in the corpus, so the caller must declare one;
    ``lot_step=0`` is the repo's explicit "no rounding" step.  ``down`` is the
    only policy: rounding up would spend more than ``equity * risk_pct``.
    """
    qty = float(quantity)
    step = float(lot_step)
    if not math.isfinite(qty) or qty < 0:
        raise ValueError("quantity must be a finite value >= 0")
    if not math.isfinite(step) or step < 0:
        raise ValueError("lot_step must be a finite value >= 0")
    if policy not in LOT_STEP_POLICIES:
        raise ValueError(
            f"unknown lot-step policy {policy!r}; "
            f"declare one of {LOT_STEP_POLICIES}")
    if step == 0.0:
        return qty
    # The epsilon guards an exact multiple that binary arithmetic renders as
    # 398.99999999999994; the result is still floored, never rounded up.
    units = math.floor(qty / step + 1e-12)
    return round(units * step, 12)


def position_size(equity: float, risk_pct: float, entry: float, stop: float, *,
                  value_per_unit: float = DEFAULT_VALUE_PER_UNIT,
                  lot_step: float = DEFAULT_LOT_STEP) -> float:
    """Solve ``size = (equity * risk_pct) / (stop_distance * value_per_unit)``.

    This is the corpus sizing formula (RE:RM-13).  ``value_per_unit`` is the I3
    instrument quantity; the result is rounded *down* to ``lot_step`` so the
    realised risk can never exceed the budget (RK-14, RK-10).
    """
    value = float(value_per_unit)
    if not math.isfinite(value) or value <= 0:
        raise ValueError("value_per_unit must be a finite value > 0")
    risk_money = float(equity) * float(risk_pct)
    risk_distance = abs(float(entry) - float(stop))
    if not math.isfinite(risk_money) or risk_money <= 0 or risk_distance <= 0:
        return 0.0
    return round_to_lot_step(
        risk_money / (risk_distance * value), lot_step)


# --------------------------------------------------------------------------- #
# RR / expectancy arithmetic (SYSTEM_SPEC §9.4)
# --------------------------------------------------------------------------- #
def weighted_rr(fractions: Sequence[float],
                rrs: Sequence[float]) -> float:
    """``weighted_RR = sum(f_i * RR_i)`` (RE:TS-16)."""
    weights = [float(f) for f in fractions]
    ratios = [float(r) for r in rrs]
    if not weights or len(weights) != len(ratios):
        raise ValueError("weights and RRs must be non-empty and the same length")
    if any(f < 0 for f in weights) or any(r < 0 for r in ratios):
        raise ValueError("weights and RRs must be >= 0")
    if sum(weights) <= 0:
        raise ValueError("weights must sum to > 0")
    return sum(f * r for f, r in zip(weights, ratios))


def expectancy(win_rate: float, rr: float,
               loss_rate: float | None = None) -> float:
    """``EV = (win_rate * RR) - (loss_rate * 1)`` (RE:TS-17).

    ``loss_rate`` defaults to ``1 - win_rate``.  Supplying both must keep the
    two rates complementary; a gate may not silently use a different total.
    """
    win = float(win_rate)
    if not 0.0 <= win <= 1.0:
        raise ValueError("win_rate must be in [0, 1]")
    loss = 1.0 - win if loss_rate is None else float(loss_rate)
    if not 0.0 <= loss <= 1.0:
        raise ValueError("loss_rate must be in [0, 1]")
    if abs(win + loss - 1.0) > 1e-9:
        raise ValueError("win_rate and loss_rate must sum to 1")
    return win * float(rr) - loss


def breakeven_win_rate(rr: float) -> float:
    """``BE_win = 1 / (1 + RR)`` (RE:TS-17)."""
    ratio = float(rr)
    if ratio <= -1.0:
        raise ValueError("RR must be > -1")
    return 1.0 / (1.0 + ratio)


@dataclass(frozen=True, slots=True)
class WeightedRRCheck:
    """The result of a weighted-RR gate with its declared hit-rate assumption."""

    weighted_rr: float
    win_rate: float | None
    expectancy: float | None
    floor: float
    blocker: str | None = None

    @property
    def admitted(self) -> bool:
        return self.blocker is None


def weighted_rr_check(value: float, *, win_rate: float | None = None,
                      floor: float = WEIGHTED_RR_FLOOR) -> WeightedRRCheck:
    """Gate a weighted RR only with an explicit hit-rate assumption.

    The corpus states that the one attested floor (``1.5:1``) is negative
    expectancy below ``40%`` (RE:MG-16/RE:C-25).  That percentage is the
    *derived* break-even rate, not a constant: no gate may silently run at it.
    A missing assumption refuses with ``NT_DATA_MISSING``.
    """
    weighted = float(value)
    if weighted < 0 or not math.isfinite(weighted):
        raise ValueError("weighted RR must be finite and >= 0")
    declared_floor = float(floor)
    if declared_floor < 0:
        raise ValueError("floor must be >= 0")
    if win_rate is None:
        return WeightedRRCheck(
            weighted, None, None, declared_floor, NT_DATA_MISSING)
    expected = expectancy(win_rate, weighted)
    blocker = None
    if weighted < declared_floor or expected < 0:
        blocker = NT_RR_BELOW_FLOOR
    return WeightedRRCheck(
        weighted, float(win_rate), expected, declared_floor, blocker)


def weighted_rr_check_from_legs(fractions: Sequence[float],
                                rrs: Sequence[float], *,
                                win_rate: float | None = None,
                                floor: float = WEIGHTED_RR_FLOOR
                                ) -> WeightedRRCheck:
    """Convenience wrapper: weight the legs, then gate the weighted result."""
    return weighted_rr_check(
        weighted_rr(fractions, rrs), win_rate=win_rate, floor=floor)


# --------------------------------------------------------------------------- #
# Plan construction
# --------------------------------------------------------------------------- #
def build_risk_plan(direction: str, entry: float, atr: float,
                    sweep: dict | None, structure: dict | None,
                    pools: Sequence[object], *, equity: float = 100_000,
                    risk_pct: float = DEFAULT_RISK_PCT,
                    cost: CostModel | None = None,
                    signal_bar: int | None = None,
                    rr_mode: str = DEFAULT_RR_MODE,
                    value_per_unit: float = DEFAULT_VALUE_PER_UNIT,
                    lot_step: float = DEFAULT_LOT_STEP,
                    min_distance: MinimumDistanceFloor | None = None,
                    risk_band: RiskBand | None = None) -> RiskPlan:
    """Build a fail-closed plan from the declared risk algebra (P1-13).

    ``pools`` accepts bare prices (legacy, ``known_at`` unknown) and level
    payloads (a :class:`TargetLevel`, a ``KeyLevel`` or a mapping with
    ``price``/``known_at``/``state``).  When ``signal_bar`` is supplied every
    level must carry ``known_at``; a level known after that bar is excluded
    (TG-03) and a level without a knowledge time refuses the plan with
    ``NT_DATA_MISSING``.

    The declared ``rr_mode`` gates the nearest target: ``none`` makes the ratio
    an output and therefore disables both the RR and the R-distance gates.  The
    declared distance floor is applied only after the RR gate passes, so one
    refusal is reported per plan instead of a redundant diagnostic pair.
    """
    if rr_mode not in RR_MODE_FLOORS:
        raise ValueError(
            f"unknown rr_mode {rr_mode!r}; "
            f"declare one of {tuple(RR_MODE_FLOORS)}")
    band = DEFAULT_RISK_BAND if risk_band is None else risk_band
    floor = (DEFAULT_MIN_DISTANCE_FLOOR
             if min_distance is None else min_distance)
    value = float(value_per_unit)
    step = float(lot_step)
    if not math.isfinite(value) or value <= 0:
        raise ValueError("value_per_unit must be a finite value > 0")
    if not math.isfinite(step) or step < 0:
        raise ValueError("lot_step must be a finite value >= 0")
    model = DEFAULT_COST_MODEL if cost is None else cost

    declarations = {
        "rr_mode": rr_mode,
        "risk_band_id": band.band_id,
        "risk_pct": float(risk_pct),
        "value_per_unit": value,
        "lot_step": step,
        "min_distance_mode": floor.mode,
    }

    def refuse(stop: float | None, sl_atr: float,
               blockers: tuple[str, ...]) -> RiskPlan:
        return RiskPlan(
            stop=stop, targets=(), gross_rr=0.0, net_rr=0.0, quantity=0.0,
            sl_atr=sl_atr, blockers=tuple(blockers), target_levels=(),
            **declarations)

    # V30: the configured risk percentage must lie inside the declared band.
    try:
        validate_risk_pct(risk_pct, band)
    except ValueError:
        return refuse(None, 0.0, (NT_VARIANT_UNDECLARED,))

    if atr <= 0:
        return refuse(None, 0.0, ("atr_invalid",))
    try:
        stop = structural_stop(direction, entry, atr, sweep, structure)
    except StopAnchorUnresolved:
        return refuse(None, 0.0, (STOP_ANCHOR_UNRESOLVED,))
    sl_atr = abs(float(entry) - stop) / float(atr)
    blockers: list[str] = []
    # The 1-3 ATR acceptance band is the labelled repo band (P0-06, RK-04).
    if not (1.0 <= sl_atr <= 3.0):
        blockers.append("sl_atr_out_of_bounds")

    levels = [_coerce_target_level(pool) for pool in pools]
    if signal_bar is not None:
        if any(level.known_at is None for level in levels):
            return refuse(stop, sl_atr,
                          tuple(blockers) + (NT_DATA_MISSING,))
        levels = [level for level in levels
                  if level.is_admissible(int(signal_bar))]
    valid = [level for level in levels
             if (level.price > float(entry) if direction == "long"
                 else level.price < float(entry))]
    valid.sort(key=lambda level: level.price, reverse=direction == "short")
    if not valid:
        return refuse(stop, sl_atr,
                      tuple(blockers) + ("no_liquidity_target",))

    # TG-05 pre-level offset: place the exit in front of the level.  The
    # ``0.1 ATR`` offset is the labelled repo value (the corpus gives a pip
    # offset per instrument); it never authors a target from the RR floor.
    tp1 = (valid[0].price - 0.1 * atr if direction == "long"
           else valid[0].price + 0.1 * atr)
    risk = abs(float(entry) - stop)
    if risk <= 0:
        return refuse(stop, sl_atr,
                      tuple(blockers) + ("sl_atr_out_of_bounds",))
    gross, net = model.net_rr(entry, stop, tp1, direction)

    # V28: one declared RR mode.  ``none`` is an output-only ratio, so it
    # disables both RR-based gates (the minimum distance is expressed in R).
    rr_blocker: str | None = None
    if rr_mode != "none":
        if net < NET_RR_FLOOR:
            rr_blocker = NET_RR_BLOCKER
        elif gross < rr_floor(rr_mode):
            rr_blocker = NT_RR_BELOW_FLOOR
    if rr_blocker is not None:
        blockers.append(rr_blocker)
    elif (rr_mode != "none" and not blockers
          and not floor.admits(abs(tp1 - float(entry)), risk)):
        # TG-11: the declared R-distance floor, evaluated on the nearest
        # target only so a far pool cannot pass the gate (TG-07).  An earlier
        # stop-band refusal keeps precedence, so one plan reports one reason.
        blockers.append(NT_TARGET_TOO_NEAR)

    if blockers:
        # Fail-closed: a blocked plan is never sized and never carries a ladder.
        return refuse(stop, sl_atr, tuple(blockers))

    qty = position_size(equity, risk_pct, entry, stop,
                        value_per_unit=value, lot_step=step)
    if qty <= 0:
        # RK-10: no trade when the declared lot step cannot solve a size.
        return refuse(stop, sl_atr, (NT_SIZE_UNSOLVABLE,))

    # The full admissible ladder, nearest first; the ladder mode - not this
    # module - decides how many rungs a plan uses (P0-05, [RE:C-17]).
    return RiskPlan(
        stop=stop,
        targets=tuple(level.price for level in valid),
        gross_rr=gross,
        net_rr=net,
        quantity=qty,
        sl_atr=sl_atr,
        blockers=(),
        target_levels=tuple(valid),
        **declarations)
