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

P1-14 adds the four vetoes of SYSTEM_SPEC §9.5.  Every one of them is
fail-closed and has no default:

* portfolio heat cap -- §13.9 lists "portfolio heat cap" as *missing*, so the
  cap must be declared; an undeclared cap refuses with ``NT_DATA_MISSING`` and
  an over-heated book refuses with ``NT_BUDGET_SPENT``.
* correlation veto (RK-13) -- the threshold and the pair correlations are not
  attested, so both must be supplied; an unknown pair correlation is
  ``NT_DATA_MISSING``, a simultaneous correlated position is ``NT_CORRELATION``.
* event veto (RK-12) with the economic-calendar input I7 -- no I7 is
  ``NT_DATA_MISSING`` (§1.5), a release inside the declared window is
  ``NT_EVENT_VETO`` (or a declared 50% size cap), and the NFP/FOMC/CPI names
  and the post-``08:30`` wait are separate rules.
* consecutive-loss kill switch and daily budget (RK-11) -- the second
  killzone loss closes the session (``NT_SESSION_CLOSED``) and the daily loss
  or trade-count cap latches the day (``NT_BUDGET_SPENT``), reset only at the
  declared V35 budget boundary.

A veto returns one :class:`VetoDecision` with exactly one §12 code (A3) and the
failing guard's inputs.  A refusal never re-authors a price (§15.5): it only
withholds size or caps an already-sized plan via :func:`apply_veto`.

P1-15 adds the management plan.  ``TargetLevel`` now carries the TG-08 expiry
it was stated with, ``RiskPlan`` records that statement, and
:func:`management_plan_for` carries a risk plan onto one
:class:`~ict_trader.strategy.ManagementPlan`: the V33 holding class, the MG-08
break-even form, the MG-05 session flat, the MG-01/MG-02 level ladder and the
TG-08 target expiry.  The plan is a frozen value (MG-09); a refused risk plan
refuses the management plan with the same §12 code, and a missing declaration
is ``NT_VARIANT_UNDECLARED`` / ``NT_DATA_MISSING`` -- never a default.

P0-04 remains in force: a refused plan carries no size and no target ladder.
P0-18 remains in force: the (gross, net) pair comes from the one shared
:class:`~ict_trader.strategy.CostModel`; this module has no private fee basis.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import datetime, time as dtime, timedelta, timezone
from typing import Mapping, Sequence
from zoneinfo import ZoneInfo

from ict_trader.cisd import (  # re-exported: the one declared stop policy
    STOP_POLICY,
    StopPolicy,
    stop_from_anchor,
)
from ict_trader.strategy import (
    CONSECUTIVE_LOSS_LIMIT,
    DEFAULT_COST_MODEL,
    DEFAULT_LOT_STEP,
    DEFAULT_LOT_STEP_POLICY,
    DEFAULT_MIN_DISTANCE_FLOOR,
    DEFAULT_RISK_BAND,
    DEFAULT_RISK_BAND_ID,
    DEFAULT_RISK_PCT,
    DEFAULT_RR_MODE,
    DEFAULT_VALUE_PER_UNIT,
    EVENT_VETO_CAP,
    EVENT_VETO_POST_RELEASE_WAIT_MINUTES,
    EVENT_VETO_WINDOW_HOURS,
    LOT_STEP_POLICIES,
    MISSING_VETO_INPUTS,
    NET_RR_FLOOR,
    RR_MODE,
    RR_MODE_FLOORS,
    WEIGHTED_RR_FLOOR,
    BEMode,
    CostModel,
    HoldingClass,
    HoldingClassPolicy,
    LadderLevel,
    LadderPermission,
    ManagementLadder,
    ManagementPlan,
    MinimumDistanceFloor,
    RiskBand,
    SessionFlat,
    TargetExpiry,
    build_management_plan,
    mg08_be_mode,
    read_holding_class,
    resolve_session_flat,
    rr_floor,
    validate_risk_pct,
)
from ict_trader.variants import read_variant_spec

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
#: §12 budget code: the daily loss cap, the trade-count cap **and** the
#: portfolio risk budget.  §12 has no dedicated heat code, so the portfolio
#: heat cap is declared here as the budget code (RK-11 / RK-13).
NT_BUDGET_SPENT = "NT_BUDGET_SPENT"
NT_EVENT_VETO = "NT_EVENT_VETO"
NT_CORRELATION = "NT_CORRELATION"
#: The kill-switch code.  §12 lists the "≥2 consecutive losses in the
#: killzone" condition under ``NT_BUDGET_SPENT`` as well, but its latch scope
#: is the *session*, and ``CODE_MAPPING`` P1-14 names ``NT_SESSION_CLOSED`` for
#: the consecutive-loss kill switch.  The repo declares the session-scoped
#: code for that latch: the loss cap latches the day, the kill switch closes
#: the session (the two codes stay distinguishable end-to-end, §5.3-5).
NT_SESSION_CLOSED = "NT_SESSION_CLOSED"

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
    #: TG-08: the expiry stated with the target.  ``None`` means the plan did
    #: not state one, and a management plan built from it refuses rather than
    #: inventing a session close (P1-15).
    expires_at: str | None = None

    def __post_init__(self) -> None:
        if not math.isfinite(float(self.price)):
            raise ValueError("target price must be finite")
        if self.known_at is not None and int(self.known_at) < 0:
            raise ValueError("known_at must be a non-negative bar index")
        if self.state not in TARGET_STATES:
            raise ValueError(
                f"unknown target state {self.state!r}; "
                f"declare one of {TARGET_STATES}")
        if self.expires_at is not None and not str(self.expires_at).strip():
            raise ValueError("expires_at, when stated, must not be empty")

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
        out = {
            "price": float(self.price),
            "known_at": self.known_at,
            "state": self.state,
            "tf": self.tf,
        }
        # TG-08: the expiry appears only when it was stated, so a legacy
        # ``(price, known_at)`` payload keeps its published shape.
        if self.expires_at is not None:
            out["expires_at"] = self.expires_at
        return out


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
    expires_at = (pool.get("expires_at") if isinstance(pool, Mapping)
                  else getattr(pool, "expires_at", None))
    return TargetLevel(
        float(price),
        None if known_at is None else int(known_at),
        str(state),
        str(tf),
        None if expires_at is None else str(expires_at),
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
    #: TG-08: the expiry stated with the plan.  ``None`` is an incomplete plan
    #: for management purposes; the realtime/management surface refuses it
    #: instead of inventing a session close.
    target_expiry: TargetExpiry | None = None

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
                    risk_band: RiskBand | None = None,
                    target_expiry: TargetExpiry | None = None) -> RiskPlan:
    """Build a fail-closed plan from the declared risk algebra (P1-13).

    ``pools`` accepts bare prices (legacy, ``known_at`` unknown) and level
    payloads (a :class:`TargetLevel`, a ``KeyLevel`` or a mapping with
    ``price``/``known_at``/``state``).  When ``signal_bar`` is supplied every
    level must carry ``known_at``; a level known after that bar is excluded
    (TG-03) and a level without a knowledge time refuses the plan with
    ``NT_DATA_MISSING``.

    ``target_expiry`` is TG-08's required statement: the accepted targets carry
    it as ``expires_at`` and the plan records it.  It is optional here only so
    pre-P1-15 callers keep working; the management surface refuses a plan that
    omitted it.

    The declared ``rr_mode`` gates the nearest target: ``none`` makes the ratio
    an output and therefore disables both the RR and the R-distance gates.  The
    declared distance floor is applied only after the RR gate passes, so one
    refusal is reported per plan instead of a redundant diagnostic pair.
    """
    if rr_mode not in RR_MODE_FLOORS:
        raise ValueError(
            f"unknown rr_mode {rr_mode!r}; "
            f"declare one of {tuple(RR_MODE_FLOORS)}")
    if target_expiry is not None and not isinstance(target_expiry, TargetExpiry):
        raise ValueError(
            f"target_expiry must be a TargetExpiry, got "
            f"{type(target_expiry).__name__}")
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
        "target_expiry": target_expiry,
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
    # module - decides how many rungs a plan uses (P0-05, [RE:C-17]).  TG-08:
    # the plan's stated expiry travels with every accepted target.
    stated_expiry = (None if target_expiry is None
                     else target_expiry.deadline)
    target_levels = tuple(
        replace(level, expires_at=stated_expiry)
        if stated_expiry is not None else level
        for level in valid)
    return RiskPlan(
        stop=stop,
        targets=tuple(level.price for level in valid),
        gross_rr=gross,
        net_rr=net,
        quantity=qty,
        sl_atr=sl_atr,
        blockers=(),
        target_levels=target_levels,
        **declarations)


# --------------------------------------------------------------------------- #
# P1-15 management plan (SYSTEM_SPEC §9.6: MG-01/02/05/08/09, TG-08, V33)
# --------------------------------------------------------------------------- #
#: The rule ids this surface implements.  They are exposed so a plan card and an
#: audit trail can name what was applied without importing the spec.
MANAGEMENT_RULE_IDS: tuple[str, ...] = (
    "MG-01", "MG-02", "MG-05", "MG-08", "MG-09", "TG-08")

#: The V33/V31 identifiers a management plan reads.
MANAGEMENT_HOLDING_CLASS_ID = "V33"
MANAGEMENT_BE_MODE_ID = "MG-08"


def management_plan_for(
    plan: RiskPlan,
    *,
    entry: float,
    holding_class: HoldingClass | str | None,
    be_mode: BEMode | str | None,
    session_clause: str | None = None,
    ladder: ManagementLadder | None = None,
    level_prices: Sequence[float] | None = None,
) -> ManagementPlan:
    """Carry a P1-13 risk plan onto a P1-15 management plan.

    No price is authored here: the stop and the terminal target are the risk
    plan's own values and the entry is the fill the caller observed.  A refused
    risk plan refuses the management plan with the same §12 code(s), and TG-08
    is enforced by reading the plan's stated ``target_expiry`` -- a plan without
    one blocks with ``NT_DATA_MISSING`` instead of borrowing a session close.
    """
    if plan.blockers:
        return ManagementPlan(
            holding_class="" if holding_class is None else str(holding_class),
            blockers=tuple(plan.blockers))
    return build_management_plan(
        holding_class=holding_class,
        ladder=ladder,
        be_mode=be_mode,
        session_clause=session_clause,
        target_expiry=plan.target_expiry,
        entry=float(entry),
        stop=plan.stop,
        terminal_target=(float(plan.targets[-1]) if plan.targets else None),
        level_prices=level_prices,
    )


# --------------------------------------------------------------------------- #
# P1-14 vetoes (SYSTEM_SPEC §9.5: RK-11, RK-12, RK-13 and the heat cap)
# --------------------------------------------------------------------------- #
#: The V row a veto reads.  V35 is the *daily budget reset* fork: the budget
#: account and the session latch may reset only at that declared boundary
#: (RE:C-26, T47), never at UTC midnight by accident (P0-03).
BUDGET_RESET_ID = "V35"

#: V11 is the *gate strength* fork.  RK-12's "defer or cap at 50%" is a
#: strength choice -- defer is the hard veto, the 50% cap is the quality
#: multiplier -- so the event-veto response is a V11 declaration.
GATE_STRENGTH_ID = "V11"

#: The three V35 options, read from the one register (A7).
BUDGET_RESET_OPTIONS: tuple[str, ...] = tuple(
    read_variant_spec(BUDGET_RESET_ID).options)

#: RK-12's two attested responses to a release inside the window.  Neither is
#: a default: a run declares one (V11) and an undeclared response refuses.
EVENT_VETO_RESPONSES: tuple[str, ...] = ("defer", "cap_50")

#: RK-12's attested post-release wait range, in minutes.  The repo selection
#: inside the range is labelled in ``strategy.VETO_REGISTER``; a declaration
#: outside the range is rejected instead of being clamped.
POST_RELEASE_WAIT_MINUTES_MIN = 5.0
POST_RELEASE_WAIT_MINUTES_MAX = 10.0

#: RK-12 names three event families to avoid outright, independent of the
#: calendar's impact taxonomy.
EVENT_VETO_BLOCKED_EVENTS: tuple[str, ...] = ("NFP", "FOMC", "CPI")

#: RK-11 requires the trade cap to declare its unit and its horizon; these are
#: mode fields, so both positions ship and exactly one is selected.
BUDGET_UNITS: tuple[str, ...] = ("fills", "attempts")
BUDGET_HORIZONS: tuple[str, ...] = ("day", "session")

#: The declared precedence of the veto guards.  A refusal carries exactly one
#: code (A3), so the first failing guard in this order is the one reported.
VETO_ORDER: tuple[str, ...] = ("budget", "event", "correlation", "heat")

_NY = ZoneInfo("America/New_York")
_US_EASTERN_0830 = dtime(8, 30)


@dataclass(frozen=True, slots=True)
class VetoDecision:
    """One veto result: admitted (optionally capped) or one §12 refusal.

    ``blocker`` is ``None`` for an admitted entry and exactly one §12 code for
    a refusal; a refusal carries ``size_multiplier`` 0.0 and the failing
    guard's inputs in ``inputs`` (A3).  The 50% event cap is expressed as a
    multiplier on an already-sized plan, never as a re-derived price (§15.5).
    """

    blocker: str | None = None
    detail: str = ""
    size_multiplier: float = 1.0
    inputs: tuple[tuple[str, object], ...] = ()

    @property
    def admitted(self) -> bool:
        return self.blocker is None

    @property
    def capped(self) -> bool:
        return self.admitted and self.size_multiplier < 1.0

    def input_dict(self) -> dict[str, object]:
        return dict(self.inputs)

    def to_public(self) -> dict[str, object]:
        return {
            "admitted": self.admitted,
            "blocker": self.blocker,
            "detail": self.detail,
            "size_multiplier": self.size_multiplier,
            "inputs": self.input_dict(),
        }


def _veto_admitted(detail: str = "", *, size_multiplier: float = 1.0,
                   **inputs: object) -> VetoDecision:
    return VetoDecision(
        detail=detail,
        size_multiplier=float(size_multiplier),
        inputs=tuple(sorted(inputs.items())),
    )


def _veto_refusal(code: str, detail: str, **inputs: object) -> VetoDecision:
    return VetoDecision(
        blocker=code,
        detail=detail,
        size_multiplier=0.0,
        inputs=tuple(sorted(inputs.items())),
    )


def _as_aware(at: object) -> datetime | None:
    """Coerce a tz-aware datetime; a naive stamp is never assumed to be UTC."""
    if not isinstance(at, datetime):
        return None
    if at.tzinfo is None or at.tzinfo.utcoffset(at) is None:
        return None
    return at


def _finite_non_negative(value: object) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number < 0:
        return None
    return number


# --------------------------------------------------------------------------- #
# RK-11: the declared budget-reset boundary (V35)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class BudgetBoundary:
    """The declared daily-budget reset boundary (V35).

    ``NY true day`` and ``UTC date`` are calendar dates in their zone;
    ``broker day`` has no corpus clock and therefore requires the broker's roll
    time and zone to be declared (:class:`ValueError` when absent).  The object
    only buckets instants: it never supplies a default boundary, so an
    undeclared V35 stays a refusal (:func:`session_budget_check`).
    """

    kind: str
    broker_roll: dtime | None = None
    broker_zone: str | None = None
    variant_id: str = BUDGET_RESET_ID

    def __post_init__(self) -> None:
        kind = str(self.kind).strip()
        if kind not in BUDGET_RESET_OPTIONS:
            raise ValueError(
                f"unknown budget-reset boundary {self.kind!r}; "
                f"{BUDGET_RESET_ID} declares one of {BUDGET_RESET_OPTIONS}")
        object.__setattr__(self, "kind", kind)
        if kind == "broker day":
            if self.broker_roll is None or not self.broker_zone:
                raise ValueError(
                    "V35='broker day' has no corpus clock: declare the "
                    "broker's roll time and IANA zone")
            object.__setattr__(
                self, "broker_roll", _parse_clock(self.broker_roll))
            try:
                ZoneInfo(str(self.broker_zone))
            except Exception:
                raise ValueError(
                    f"unknown broker zone {self.broker_zone!r}") from None
        elif self.broker_roll is not None or self.broker_zone is not None:
            raise ValueError(
                "broker_roll/broker_zone are only meaningful for "
                "V35='broker day'")

    def day_key(self, at: datetime) -> str:
        """The identity of the declared budget day containing ``at``."""
        stamp = _as_aware(at)
        if stamp is None:
            raise ValueError(
                "the budget boundary needs a tz-aware timestamp; a naive "
                "stamp is never assumed to be UTC")
        if self.kind == "NY true day":
            return stamp.astimezone(_NY).date().isoformat()
        if self.kind == "UTC date":
            return stamp.astimezone(timezone.utc).date().isoformat()
        local = stamp.astimezone(ZoneInfo(str(self.broker_zone)))
        day = local.date()
        if local.time() < self.broker_roll:  # type: ignore[operator]
            day = day - timedelta(days=1)
        return day.isoformat()

    def to_public(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "broker_roll": (None if self.broker_roll is None
                            else self.broker_roll.strftime("%H:%M")),
            "broker_zone": self.broker_zone,
            "variant_id": self.variant_id,
        }


def _parse_clock(value: object) -> dtime:
    if isinstance(value, dtime):
        return value
    text = str(value).strip()
    try:
        hour, minute = text.split(":", 1)
        return dtime(int(hour), int(minute))
    except (ValueError, TypeError):
        raise ValueError(
            f"a broker roll must be HH:MM, got {value!r}") from None


# --------------------------------------------------------------------------- #
# Portfolio heat cap (§13.9 blocked parameter, applied to RK-11's budget)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class PortfolioHeatCap:
    """One declared portfolio heat cap, with its source.

    §13.9 lists "portfolio heat cap" as a parameter the corpus leaves
    *missing*, so no numeric default exists anywhere in this module: the cap is
    ``equity * cap_pct`` and both the percentage and its source must be
    declared.  ``cap_pct`` is a fraction of equity (``0.02`` is 2%).
    """

    cap_pct: float
    source_id: str
    label: str = "repo"
    variant_id: str = ""  # §13.9 blocked parameter: no V row exists

    def __post_init__(self) -> None:
        pct = float(self.cap_pct)
        if not math.isfinite(pct) or not 0.0 <= pct <= 1.0:
            raise ValueError("portfolio heat cap_pct must be in [0, 1]")
        if not str(self.source_id).strip():
            raise ValueError(
                "a portfolio heat cap needs its source (author/attestation); "
                "the corpus value is missing, so it cannot be anonymous")
        if not str(self.label).strip():
            raise ValueError("a portfolio heat cap needs a provenance label")

    def limit(self, equity: float) -> float:
        return float(equity) * self.cap_pct

    def to_public(self) -> dict[str, object]:
        return {
            "cap_pct": self.cap_pct,
            "source_id": self.source_id,
            "label": self.label,
        }


def risk_money(quantity: float, entry: float, stop: float,
               value_per_unit: float = DEFAULT_VALUE_PER_UNIT) -> float:
    """The account-currency risk of one position (RK-10 sizing identity)."""
    return (abs(float(entry) - float(stop)) * float(quantity)
            * float(value_per_unit))


def plan_risk_money(plan: RiskPlan, entry: float) -> float:
    """The risk the P1-13 plan carries, for the portfolio heat gate."""
    if plan.stop is None or plan.quantity <= 0:
        return 0.0
    return risk_money(plan.quantity, entry, plan.stop, plan.value_per_unit)


def portfolio_heat_check(declaration: PortfolioHeatCap | None, *,
                         equity: float | None,
                         open_risk_money: float | None,
                         new_risk_money: float | None) -> VetoDecision:
    """Refuse an entry that would push portfolio heat above the declared cap.

    Heat is ``(open risk + new risk) / equity``.  A missing cap, equity, open
    risk or candidate risk refuses with ``NT_DATA_MISSING``; nothing is
    assumed to be zero.  The reversal of RK-13's "skip when already exposed":
    the gate is on total risk, not on a direction.
    """
    inputs = {
        "equity": equity,
        "open_risk_money": open_risk_money,
        "new_risk_money": new_risk_money,
        "cap_pct": None if declaration is None else declaration.cap_pct,
    }
    if declaration is None:
        return _veto_refusal(
            NT_DATA_MISSING,
            "no portfolio heat cap is declared; §13.9 lists it as missing, so "
            "the gate cannot run on a default", **inputs)
    balance = _finite_non_negative(equity)
    if balance is None or balance <= 0:
        return _veto_refusal(
            NT_DATA_MISSING, "the heat cap needs a positive equity", **inputs)
    open_risk = _finite_non_negative(open_risk_money)
    if open_risk is None:
        return _veto_refusal(
            NT_DATA_MISSING, "the open portfolio risk is unknown", **inputs)
    new_risk = _finite_non_negative(new_risk_money)
    if new_risk is None:
        return _veto_refusal(
            NT_DATA_MISSING, "the candidate trade risk is unknown", **inputs)
    limit = declaration.limit(balance)
    heat = open_risk + new_risk
    if heat > limit + 1e-9:
        return _veto_refusal(
            NT_BUDGET_SPENT,
            f"portfolio heat {heat:g} would exceed the declared cap {limit:g} "
            f"({declaration.cap_pct:g} of {balance:g})",
            **inputs)
    return _veto_admitted(
        f"portfolio heat {heat:g} inside the declared cap {limit:g}",
        heat=heat, heat_limit=limit, **inputs)


# --------------------------------------------------------------------------- #
# RK-13: correlation veto
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class CorrelationVeto:
    """One declared correlation veto threshold, with its source.

    RK-13 says "skip simultaneous correlated positions" and supplies no
    threshold, so the threshold and its source must be declared.  A pair whose
    correlation is not supplied is unknown, not zero (``NT_DATA_MISSING``).
    """

    threshold: float
    source_id: str
    label: str = "missing"
    variant_id: str = ""  # RK-13 has no V row

    def __post_init__(self) -> None:
        threshold = float(self.threshold)
        if not math.isfinite(threshold) or not 0.0 < threshold <= 1.0:
            raise ValueError(
                "correlation threshold must be in (0, 1]; RK-13 supplies no "
                "value, so a declared one must be a real correlation bound")
        if not str(self.source_id).strip():
            raise ValueError(
                "a correlation threshold needs its source; the corpus value "
                "is missing")
        if not str(self.label).strip():
            raise ValueError("a correlation veto needs a provenance label")

    def to_public(self) -> dict[str, object]:
        return {
            "threshold": self.threshold,
            "source_id": self.source_id,
            "label": self.label,
        }


@dataclass(frozen=True, slots=True)
class OpenPositionRisk:
    """One open position as the portfolio gates see it."""

    symbol: str
    risk_money: float

    def __post_init__(self) -> None:
        if not str(self.symbol).strip():
            raise ValueError("an open position needs its symbol")
        object.__setattr__(self, "symbol", str(self.symbol).strip().upper())
        risk = _finite_non_negative(self.risk_money)
        if risk is None:
            raise ValueError("an open position needs its finite risk >= 0")
        object.__setattr__(self, "risk_money", risk)


def _pair_key(symbol: str, other: str) -> tuple[str, str]:
    pair = (str(symbol).strip().upper(), str(other).strip().upper())
    return (pair[0], pair[1]) if pair[0] <= pair[1] else (pair[1], pair[0])


def _correlation_map(correlations: Mapping[object, object]
                     ) -> dict[tuple[str, str], float]:
    normal: dict[tuple[str, str], float] = {}
    for key, value in correlations.items():
        if not isinstance(key, (tuple, list)) or len(key) != 2:
            raise ValueError(
                f"a correlation key must be a symbol pair, got {key!r}")
        normal[_pair_key(str(key[0]), str(key[1]))] = float(value)
    return normal


def correlation_veto(declaration: CorrelationVeto | None, *,
                     symbol: str,
                     open_positions: Sequence[OpenPositionRisk] = (),
                     correlations: Mapping[object, object] | None = None
                     ) -> VetoDecision:
    """Refuse a new entry while a simultaneous correlated position is open.

    RK-13's condition is correlation, not direction: RK-13 names no direction
    conjunct, so adding one would author a rule the corpus does not state.
    With no open position the gate evaluates false from the book alone and
    needs no correlation data; with an open position, a missing threshold, a
    missing correlation map or an unknown pair refuses ``NT_DATA_MISSING``.
    """
    positions = tuple(open_positions)
    if not positions:
        return _veto_admitted("no simultaneous open position")
    candidate = str(symbol).strip().upper()
    inputs = {
        "symbol": candidate,
        "open_symbols": tuple(p.symbol for p in positions),
        "threshold": None if declaration is None else declaration.threshold,
    }
    if declaration is None:
        return _veto_refusal(
            NT_DATA_MISSING,
            "no correlation threshold is declared; RK-13 supplies no value, "
            "so the gate cannot run on a default", **inputs)
    if not candidate:
        return _veto_refusal(
            NT_DATA_MISSING, "the candidate symbol is unknown", **inputs)
    if correlations is None:
        return _veto_refusal(
            NT_DATA_MISSING,
            "no pair correlations are supplied; an unknown correlation is "
            "not zero", **inputs)
    try:
        table = _correlation_map(correlations)
    except (TypeError, ValueError) as exc:
        return _veto_refusal(
            NT_DATA_MISSING, f"the correlation input is unusable: {exc}",
            **inputs)
    for position in sorted(positions, key=lambda p: (p.symbol, p.risk_money)):
        pair = _pair_key(candidate, position.symbol)
        corr = table.get(pair)
        if corr is None:
            return _veto_refusal(
                NT_DATA_MISSING,
                f"the correlation of {candidate} vs {position.symbol} is "
                "unknown; it cannot be assumed uncorrelated",
                pair=pair, **inputs)
        if not math.isfinite(corr) or not -1.0 <= corr <= 1.0:
            return _veto_refusal(
                NT_DATA_MISSING,
                f"the correlation of {candidate} vs {position.symbol} is not "
                f"a correlation coefficient: {corr!r}",
                pair=pair, **inputs)
        if corr >= declaration.threshold:
            return _veto_refusal(
                NT_CORRELATION,
                f"{candidate} is correlated with the open {position.symbol} "
                f"(rho={corr:g} >= {declaration.threshold:g}); RK-13 skips "
                "simultaneous correlated positions",
                pair=pair, correlation=corr, **inputs)
    return _veto_admitted(
        "no open position is correlated above the declared threshold",
        **inputs)


# --------------------------------------------------------------------------- #
# RK-12: event veto with the economic-calendar input I7
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class CalendarRelease:
    """One economic release from the I7 calendar.

    ``high_impact`` is the calendar's own taxonomy bit.  §13.9 lists the
    "economic-calendar taxonomy" as *missing*, so this module never derives it
    from the event name: ``None`` means unclassified and refuses
    ``NT_DATA_MISSING`` when the release is inside the veto window.
    """

    at: datetime
    name: str
    high_impact: bool | None = None

    def __post_init__(self) -> None:
        stamp = _as_aware(self.at)
        if stamp is None:
            raise ValueError(
                "a calendar release needs a tz-aware timestamp (I7)")
        object.__setattr__(self, "at", stamp)
        name = str(self.name).strip()
        if not name:
            raise ValueError("a calendar release needs its event name")
        object.__setattr__(self, "name", name)
        if self.high_impact is not None and not isinstance(
                self.high_impact, bool):
            raise ValueError(
                "high_impact is the calendar's boolean classification "
                "or None (unclassified)")

    def local_time(self) -> dtime:
        return self.at.astimezone(_NY).time()

    def to_public(self) -> dict[str, object]:
        return {
            "at": self.at.isoformat(),
            "name": self.name,
            "high_impact": self.high_impact,
        }


@dataclass(frozen=True, slots=True)
class EconomicCalendar:
    """The I7 economic calendar: release timestamps plus their taxonomy bit.

    The calendar is a required input, not context (§1.4/§1.5): a run with no
    calendar cannot evaluate the event veto and refuses ``NT_DATA_MISSING``
    instead of treating the gate as passing.
    """

    releases: tuple[CalendarRelease, ...]
    source: str = "I7"

    def __post_init__(self) -> None:
        releases = tuple(self.releases)
        for release in releases:
            if not isinstance(release, CalendarRelease):
                raise ValueError(
                    f"a calendar entry must be a CalendarRelease, got "
                    f"{type(release).__name__}")
        object.__setattr__(
            self,
            "releases",
            tuple(sorted(releases, key=lambda r: r.at)),
        )
        if not str(self.source).strip():
            raise ValueError("the economic calendar must name its source (I7)")

    @classmethod
    def from_rows(cls, rows: Sequence[object], *,
                  source: str = "I7") -> "EconomicCalendar":
        """Build a calendar from mappings or ``(at, name, high_impact)`` rows."""
        releases: list[CalendarRelease] = []
        for row in rows:
            if isinstance(row, CalendarRelease):
                releases.append(row)
            elif isinstance(row, Mapping):
                releases.append(CalendarRelease(
                    row["at"], row["name"], row.get("high_impact")))
            else:
                values = tuple(row)  # type: ignore[arg-type]
                if len(values) == 2:
                    values = (values[0], values[1], None)
                if len(values) != 3:
                    raise ValueError(
                        "a calendar row must be (at, name, high_impact)")
                releases.append(CalendarRelease(*values))
        return cls(tuple(releases), source=source)

    def to_public(self) -> dict[str, object]:
        return {
            "source": self.source,
            "releases": [release.to_public() for release in self.releases],
        }


@dataclass(frozen=True, slots=True)
class EventVetoPolicy:
    """The declared RK-12 event veto: window, response and post-release wait.

    ``response`` is the V11 gate-strength selection: ``defer`` refuses every
    entry inside the window (hard veto), ``cap_50`` admits the entry at 50% of
    the sized quantity (quality multiplier).  RK-12 gives the range ``5-10``
    minutes for the post-``08:30`` wait and no preference inside it; the value
    comes from the labelled register (``strategy.VETO_REGISTER``) and a value
    outside the range is rejected rather than clamped.
    """

    response: str
    window_hours: float = EVENT_VETO_WINDOW_HOURS
    post_release_wait_minutes: float = (
        EVENT_VETO_POST_RELEASE_WAIT_MINUTES)
    blocked_events: tuple[str, ...] = EVENT_VETO_BLOCKED_EVENTS
    source: str = "RK-12 / [RE:RM-21]"
    variant_id: str = GATE_STRENGTH_ID

    def __post_init__(self) -> None:
        response = str(self.response).strip()
        if response not in EVENT_VETO_RESPONSES:
            raise ValueError(
                f"unknown event-veto response {self.response!r}; "
                f"{GATE_STRENGTH_ID} declares one of {EVENT_VETO_RESPONSES} "
                "(defer = hard veto, cap_50 = quality multiplier)")
        object.__setattr__(self, "response", response)
        window = float(self.window_hours)
        if not math.isfinite(window) or window <= 0:
            raise ValueError("the event-veto window must be a finite "
                             "positive number of hours")
        wait = float(self.post_release_wait_minutes)
        if not (POST_RELEASE_WAIT_MINUTES_MIN <= wait
                <= POST_RELEASE_WAIT_MINUTES_MAX):
            raise ValueError(
                f"the post-release wait must lie inside RK-12's attested "
                f"{POST_RELEASE_WAIT_MINUTES_MIN:g}-"
                f"{POST_RELEASE_WAIT_MINUTES_MAX:g} minutes, got {wait:g}")
        blocked = tuple(
            str(name).strip().upper() for name in self.blocked_events if
            str(name).strip())
        if not blocked:
            raise ValueError("the event veto needs its blocked event names")
        object.__setattr__(self, "blocked_events", blocked)
        if not str(self.source).strip():
            raise ValueError("the event veto must carry its source")

    @property
    def size_multiplier(self) -> float:
        """The declared cap for an admitted inside-window entry (RK-12)."""
        return 1.0 if self.response == "defer" else EVENT_VETO_CAP

    def _blocked_by_name(self, name: str) -> bool:
        return str(name).strip().upper() in self.blocked_events

    def to_public(self) -> dict[str, object]:
        return {
            "response": self.response,
            "window_hours": self.window_hours,
            "post_release_wait_minutes": self.post_release_wait_minutes,
            "blocked_events": self.blocked_events,
            "size_multiplier": self.size_multiplier,
            "variant_id": self.variant_id,
        }


def event_veto(policy: EventVetoPolicy | None, *,
               calendar: EconomicCalendar | None,
               at: datetime) -> VetoDecision:
    """Apply RK-12 to one prospective entry at ``at``.

    The declared rules are applied in this order, so a knowable refusal is
    never masked by missing data:

    1. A calendar is mandatory (I7): no calendar is ``NT_DATA_MISSING``.
    2. A blocked name (NFP/FOMC/CPI) on the same NY day, or inside the forward
       window, is ``NT_EVENT_VETO`` regardless of the impact taxonomy.
    3. A release inside the declared forward window with an unclassified
       impact is ``NT_DATA_MISSING`` (§13.9's missing taxonomy).
    4. A high-impact release inside the window is ``NT_EVENT_VETO`` under the
       ``defer`` response, or an admitted entry with the declared 50% cap
       under ``cap_50``.
    5. Within the declared 5-10 minute wait after an ``08:30`` NY release, a
       new entry is ``NT_EVENT_VETO``.

    The 48-hour window is read forward-looking: it guards entries *into* an
    upcoming release, while rule 5 covers the immediate aftermath of the
    ``08:30`` slot.  A release that is already in the past and outside rule 5
    does not veto.  "Within 48 h" does not state a direction, so this reading
    is declared here (and is the only reading under which rule 5 adds a rule
    rather than duplicating rule 4).
    """
    inputs: dict[str, object] = {
        "at": None if not isinstance(at, datetime) else at.isoformat(),
        "policy": None if policy is None else policy.response,
    }
    stamp = _as_aware(at)
    if stamp is None:
        return _veto_refusal(
            NT_DATA_MISSING,
            "the event veto needs a tz-aware entry timestamp", **inputs)
    if calendar is None:
        return _veto_refusal(
            NT_DATA_MISSING,
            "input I7 (economic calendar) is missing; the event veto "
            "evaluates N/A and may not pass a gate (§1.5)", **inputs)
    if policy is None:
        return _veto_refusal(
            NT_VARIANT_UNDECLARED,
            "the RK-12 response fork is undeclared; declare 'defer' or "
            "'cap_50' (V11 gate strength)", **inputs)

    local_day = stamp.astimezone(_NY).date()
    window = timedelta(hours=policy.window_hours)
    horizon = stamp + window
    releases = calendar.releases

    # 1. Named events: avoid them on their NY day and inside the window even
    #    when the calendar's taxonomy is missing.
    for release in releases:
        same_day = release.at.astimezone(_NY).date() == local_day
        in_window = stamp <= release.at <= horizon
        if policy._blocked_by_name(release.name) and (same_day or in_window):
            return _veto_refusal(
                NT_EVENT_VETO,
                f"RK-12 avoids {release.name} "
                f"({release.at.isoformat()})",
                release=release.to_public(), **inputs)

    # 2. An unclassified release inside the window is N/A, not "not high".
    for release in releases:
        if not (stamp <= release.at <= horizon):
            continue
        if release.high_impact is None:
            return _veto_refusal(
                NT_DATA_MISSING,
                f"the calendar classifies {release.name} "
                f"({release.at.isoformat()}) as neither high nor low impact; "
                "the missing taxonomy may not pass a gate", **inputs)

    # 3. High-impact release inside the forward window.
    multiplier = 1.0
    capped: list[CalendarRelease] = []
    for release in releases:
        if not (stamp <= release.at <= horizon) or not release.high_impact:
            continue
        if policy.response == "defer":
            return _veto_refusal(
                NT_EVENT_VETO,
                f"high-impact release {release.name} at "
                f"{release.at.isoformat()} is inside the declared "
                f"{policy.window_hours:g} h window",
                release=release.to_public(), **inputs)
        capped.append(release)
    if capped:
        multiplier = policy.size_multiplier

    # 4. The post-08:30 wait: separate from the forward window (the release is
    #    already in the past, so rule 3 cannot see it).
    wait = timedelta(minutes=policy.post_release_wait_minutes)
    for release in releases:
        if release.local_time() != _US_EASTERN_0830:
            continue
        if not (release.at <= stamp < release.at + wait):
            continue
        if release.high_impact is None:
            return _veto_refusal(
                NT_DATA_MISSING,
                f"the 08:30 release {release.name} is unclassified; the "
                "post-release wait cannot be evaluated", **inputs)
        if release.high_impact:
            return _veto_refusal(
                NT_EVENT_VETO,
                f"still inside the {policy.post_release_wait_minutes:g} min "
                f"wait after the 08:30 release {release.name}",
                release=release.to_public(), **inputs)

    if capped:
        return _veto_admitted(
            "inside the event window; the entry is capped at "
            f"{multiplier:.0%} (RK-12)",
            size_multiplier=multiplier,
            releases=tuple(r.to_public() for r in capped),
            **inputs)
    return _veto_admitted("no release inside the declared event window",
                          **inputs)


# --------------------------------------------------------------------------- #
# RK-11: daily budget, trade-count cap and the consecutive-loss kill switch
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class TradeBudget:
    """The declared RK-11 budget: daily cap, trade cap and kill switch.

    ``unit`` and ``horizon`` are mandatory mode fields (RK-11: "max trades/day
    declared with **unit** (trades vs attempts) and horizon").  A ``None`` cap
    is the declared `off` position the conflict table permits (RE:C-07/C-08),
    not a missing value: the declaration itself is always present.
    """

    unit: str
    horizon: str
    max_daily_loss_pct: float | None = None
    max_trades: int | None = None
    kill_switch_losses: int = CONSECUTIVE_LOSS_LIMIT
    source: str = "RK-11 / [RE:RM-17..RM-19]"

    def __post_init__(self) -> None:
        unit = str(self.unit).strip()
        if unit not in BUDGET_UNITS:
            raise ValueError(
                f"unknown trade-cap unit {self.unit!r}; RK-11 declares one "
                f"of {BUDGET_UNITS}")
        horizon = str(self.horizon).strip()
        if horizon not in BUDGET_HORIZONS:
            raise ValueError(
                f"unknown budget horizon {self.horizon!r}; RK-11 declares "
                f"one of {BUDGET_HORIZONS}")
        object.__setattr__(self, "unit", unit)
        object.__setattr__(self, "horizon", horizon)
        if self.max_daily_loss_pct is not None:
            pct = float(self.max_daily_loss_pct)
            if not math.isfinite(pct) or not 0.0 <= pct <= 1.0:
                raise ValueError("max_daily_loss_pct must be in [0, 1]")
            object.__setattr__(self, "max_daily_loss_pct", pct)
        if self.max_trades is not None and int(self.max_trades) < 0:
            raise ValueError("max_trades must be >= 0 or None (off)")
        # RK-11 fixes the trigger at ">= 2 consecutive losses in the killzone".
        # A stricter switch (fire after 1) is allowed because it stops no
        # later than the rule; a laxer one would skip the required stop.
        if not 1 <= int(self.kill_switch_losses) <= CONSECUTIVE_LOSS_LIMIT:
            raise ValueError(
                f"the kill switch may not fire later than RK-11's "
                f"{CONSECUTIVE_LOSS_LIMIT} consecutive killzone losses, got "
                f"{self.kill_switch_losses!r}")
        if not str(self.source).strip():
            raise ValueError("the trade budget must carry its source")

    def to_public(self) -> dict[str, object]:
        return {
            "unit": self.unit,
            "horizon": self.horizon,
            "max_daily_loss_pct": self.max_daily_loss_pct,
            "max_trades": self.max_trades,
            "kill_switch_losses": self.kill_switch_losses,
        }


@dataclass(slots=True)
class BudgetState:
    """The intraday budget account and the latches RK-11 resets at V35.

    ``day`` is the declared budget-day key.  When a later instant falls in a
    new declared day every counter and every latch resets together (T47 / V35),
    so the daily cap and the session kill switch cannot leak into the next
    declared day.  A latch is never dismissed mid-day (§11 F11).
    """

    day: str | None = None
    realized_pnl: float = 0.0
    killzone_losses: int = 0
    fills: int = 0
    attempts: int = 0
    latch_code: str | None = None
    latch_detail: str = ""

    def _roll(self, boundary: BudgetBoundary, at: datetime) -> bool:
        key = boundary.day_key(at)
        if key == self.day:
            return False
        self.day = key
        self.realized_pnl = 0.0
        self.killzone_losses = 0
        self.fills = 0
        self.attempts = 0
        self.latch_code = None
        self.latch_detail = ""
        return True

    def record_outcome(self, boundary: BudgetBoundary, at: datetime, pnl: float,
                       *, in_killzone: bool = False) -> None:
        """Book one closed trade; a killzone loss is what RK-11 counts."""
        if boundary is None:
            raise ValueError(
                "the budget account needs the declared V35 boundary")
        self._roll(boundary, at)
        value = float(pnl)
        if not math.isfinite(value):
            raise ValueError("a trade outcome must be a finite PnL")
        self.realized_pnl += value
        self.fills += 1
        if in_killzone:
            self.killzone_losses = (
                self.killzone_losses + 1 if value < 0.0 else 0)

    def record_attempt(self, boundary: BudgetBoundary, at: datetime) -> None:
        """Count one entry attempt for ``unit='attempts'`` budgets."""
        if boundary is None:
            raise ValueError(
                "the budget account needs the declared V35 boundary")
        self._roll(boundary, at)
        self.attempts += 1

    @property
    def trade_count(self) -> int:
        return self.fills

    def count_for(self, budget: TradeBudget) -> int:
        return self.attempts if budget.unit == "attempts" else self.fills

    def check(self, budget: TradeBudget | None, *,
              boundary: BudgetBoundary | None,
              at: datetime,
              equity: float | None) -> VetoDecision:
        """Evaluate the RK-11 gates for a prospective new entry."""
        inputs: dict[str, object] = {
            "day": self.day,
            "realized_pnl": self.realized_pnl,
            "killzone_losses": self.killzone_losses,
            "fills": self.fills,
            "attempts": self.attempts,
        }
        stamp = _as_aware(at)
        if stamp is None:
            return _veto_refusal(
                NT_DATA_MISSING,
                "the budget gate needs a tz-aware entry timestamp", **inputs)
        if budget is None:
            return _veto_refusal(
                NT_DATA_MISSING,
                "no RK-11 trade budget is declared; the daily cap, the trade "
                "cap and the kill switch cannot be evaluated", **inputs)
        if boundary is None:
            return _veto_refusal(
                NT_VARIANT_UNDECLARED,
                f"{BUDGET_RESET_ID} (daily budget reset) is undeclared; the "
                "budget account may not reset at an assumed boundary",
                **inputs)
        self._roll(boundary, stamp)
        inputs["day"] = self.day
        if self.latch_code is not None:
            return _veto_refusal(
                self.latch_code,
                f"the day/session is latched: {self.latch_detail}",
                **inputs)

        if budget.max_daily_loss_pct is not None:
            balance = _finite_non_negative(equity)
            if balance is None or balance <= 0:
                return _veto_refusal(
                    NT_DATA_MISSING,
                    "the daily loss cap needs the budget equity basis",
                    **inputs)
            limit = balance * budget.max_daily_loss_pct
            if self.realized_pnl <= -limit:
                self.latch_code = NT_BUDGET_SPENT
                self.latch_detail = (
                    f"daily loss {self.realized_pnl:g} reached the cap "
                    f"-{limit:g}")
                return _veto_refusal(
                    NT_BUDGET_SPENT,
                    f"daily loss {self.realized_pnl:g} reached the declared "
                    f"cap -{limit:g} ({budget.max_daily_loss_pct:g} of "
                    f"{balance:g}); new entries are halted for the day",
                    **inputs)

        if self.killzone_losses >= budget.kill_switch_losses:
            self.latch_code = NT_SESSION_CLOSED
            self.latch_detail = (
                f"{self.killzone_losses} consecutive killzone losses")
            return _veto_refusal(
                NT_SESSION_CLOSED,
                f"{self.killzone_losses} consecutive losses in the killzone "
                f"(RK-11 threshold {budget.kill_switch_losses}); the session "
                "is closed for the day", **inputs)

        if budget.max_trades is not None:
            count = self.count_for(budget)
            if count >= int(budget.max_trades):
                self.latch_code = NT_BUDGET_SPENT
                self.latch_detail = (
                    f"{count} {budget.unit} reached the declared cap "
                    f"{budget.max_trades}")
                return _veto_refusal(
                    NT_BUDGET_SPENT,
                    f"{count} {budget.unit} reached the declared "
                    f"{budget.horizon} cap {budget.max_trades}",
                    **inputs)
        return _veto_admitted(
            f"{self.count_for(budget)} {budget.unit} counted for the "
            f"declared {budget.horizon}",
            **inputs)


def session_budget_check(budget: TradeBudget | None, state: BudgetState | None,
                         *, boundary: BudgetBoundary | None,
                         at: datetime,
                         equity: float | None) -> VetoDecision:
    """Module-level RK-11 entry point; a missing account is a refusal."""
    if state is None:
        return _veto_refusal(
            NT_DATA_MISSING,
            "no budget account is supplied; the kill switch has no history "
            "and a missing history may not be read as zero losses")
    return state.check(budget, boundary=boundary, at=at, equity=equity)


# --------------------------------------------------------------------------- #
# The veto board: one declared precedence, one §12 code per refusal
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class VetoRequest:
    """Everything one prospective entry has to show the veto guards."""

    at: datetime
    equity: float | None = None
    symbol: str = ""
    new_risk_money: float | None = None
    open_risk_money: float | None = None
    open_positions: tuple[OpenPositionRisk, ...] = ()
    correlations: Mapping[object, object] | None = None
    calendar: EconomicCalendar | None = None


@dataclass(frozen=True, slots=True)
class RiskVetoBoard:
    """The four §9.5 vetoes, applied in the declared :data:`VETO_ORDER`.

    Every declaration is optional in the type system so an unconfigured run
    can be constructed and then refuse at the gate instead of raising at
    import/start-up; ``None`` means *undeclared* and never *off*.  The board
    returns the first refusal only, so a caller sees exactly one §12 code and
    that guard's inputs (A3).  A ``cap_50`` admission keeps its multiplier on
    the decision for :func:`apply_veto`.
    """

    budget: TradeBudget | None = None
    boundary: BudgetBoundary | None = None
    heat: PortfolioHeatCap | None = None
    correlation: CorrelationVeto | None = None
    events: EventVetoPolicy | None = None

    def check(self, request: VetoRequest, state: BudgetState | None) -> VetoDecision:
        multiplier = 1.0
        open_risk = request.open_risk_money
        if open_risk is None:
            open_risk = sum(p.risk_money for p in request.open_positions)
        for guard in VETO_ORDER:
            if guard == "budget":
                decision = session_budget_check(
                    self.budget, state, boundary=self.boundary,
                    at=request.at, equity=request.equity)
            elif guard == "event":
                decision = event_veto(
                    self.events, calendar=request.calendar, at=request.at)
            elif guard == "correlation":
                decision = correlation_veto(
                    self.correlation, symbol=request.symbol,
                    open_positions=request.open_positions,
                    correlations=request.correlations)
            elif guard == "heat":
                decision = portfolio_heat_check(
                    self.heat, equity=request.equity,
                    open_risk_money=open_risk,
                    new_risk_money=request.new_risk_money)
            else:  # pragma: no cover - VETO_ORDER is a closed tuple
                raise AssertionError(f"unknown veto guard {guard!r}")
            if not decision.admitted:
                return decision
            multiplier *= decision.size_multiplier
        return _veto_admitted(
            "all declared vetoes admit the entry",
            size_multiplier=multiplier,
            guards=VETO_ORDER)

    def to_public(self) -> dict[str, object]:
        return {
            "order": VETO_ORDER,
            "budget": None if self.budget is None else self.budget.to_public(),
            "boundary": (None if self.boundary is None
                         else self.boundary.to_public()),
            "heat": None if self.heat is None else self.heat.to_public(),
            "correlation": (None if self.correlation is None
                            else self.correlation.to_public()),
            "events": None if self.events is None else self.events.to_public(),
            "missing_inputs": MISSING_VETO_INPUTS,
        }


def apply_veto(plan: RiskPlan, decision: VetoDecision) -> RiskPlan:
    """Carry a veto decision onto a P1-13 plan, authoring no new price.

    A refusal returns the fail-closed plan of P0-04: no size, no ladder, and
    the §12 code as the single blocker.  A ``cap_50`` admission lowers the
    already-solved quantity (floored by the declared lot step, RK-14) and
    leaves every price untouched (§15.5).
    """
    if not decision.admitted:
        return replace(
            plan,
            targets=(),
            target_levels=(),
            quantity=0.0,
            blockers=(decision.blocker,) if decision.blocker else (),
        )
    if decision.size_multiplier >= 1.0:
        return plan
    return replace(
        plan,
        quantity=round_to_lot_step(plan.quantity * decision.size_multiplier,
                                   plan.lot_step),
    )
