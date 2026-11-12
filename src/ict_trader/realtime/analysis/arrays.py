"""Persistent ICT array lifecycle (FVG/IFVG/OB/CISD/Dealing Range).

This module is a **lifecycle wrapper** over the canonical detectors in
:mod:`ict_trader.concepts`.  It owns identity, symbol/interval, anchor,
formed/swept/invalidated times, the ``known_at`` knowledge time (CB-07) and the
LQ-06 ``{intact, swept, broken}`` state so arrays can be replayed and audited
long after the chart window moved on.  It deliberately contains no
FVG/IFVG/OB/CISD/range arithmetic of its own: those bounds come from
``find_pivots``/``build_zigzag``, ``detect_structure``, ``detect_fvgs``,
``detect_ifvg``, ``detect_order_blocks``, ``detect_cisd`` and
``detect_dealing_range`` (P0-10, A7/X8, §7.1).

A zero-height "zone" is a **line**, not an array.  Rather than invent a height
for it, the lifecycle records it as a broken array (``state="broken"`` and
``invalidated_at == formed_at``); the historical ``is_active`` flag is kept as a
compatibility property (``state != "broken"``).  CISD lines therefore use one
symmetric type id (``CISD``) in both directions; direction is carried by
``direction``.

Object identity (P0-16, DR-00/X8/§7.1): ``anchor_price`` is the structural
anchor of an array (a swing, candle or level) and **never** a derived 50%.
Each "50%" has its own typed field produced by its own object:

* ``range_equilibrium`` -- the equilibrium of a structural dealing range (DR-03);
* ``gap_ce`` -- the consequent encroachment of an FVG/IFVG (AR-07);
* ``ob_body_mean`` -- the body mean threshold of an order block (AR-17).

A fixed-count high/low object is never emitted as ``DEALING_RANGE``: when the
structural pair is absent the range is ``None`` and the reason lives in the
``RangeDiagnostic`` returned by ``detect_dealing_range_state`` (DR-00/SN-R07).

``build_ict_arrays`` returns the **full inventory**.  A per-kind cap is a
presentation rule (a chart can only draw so many boxes), never a selection
rule: silently dropping the ninth active FVG would remove an eligible entry
array before §7.3 could choose it.  :func:`present_ict_arrays` is the only
place the visible caps are applied (P0-14, A1, §7.3).
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import pandas as pd

from ...concepts import (
    ARRAY_SIDES,
    DEFAULT_ARRAY_ORDER_MODE,
    DEFAULT_ARRAY_SIDE_MODE,
    DEFAULT_ARRAY_TOTAL_ORDER,
    ArrayState,
    ICTConfig,
    GapConsequentEncroachment,
    OrderBlockBodyMean,
    RangeEquilibrium,
    array_side,
    build_zigzag,
    cisd_config,
    detect_bpr,
    detect_cisd,
    detect_dealing_range_state,
    detect_fvgs,
    detect_ifvg,
    detect_liquidity,
    detect_order_blocks,
    detect_structure,
    find_pivots,
    knowledge_state,
    scan_arrays,
    validate_array_state,
)
from ...enhancements import detect_session_levels
from ...pd_arrays import detect_breakers
from ...sweeps import detect_key_levels, detect_sweeps
from ..models import dt_to_ms


@dataclass(slots=True)
class ICTArray:
    array_id: str
    symbol: str
    interval: str
    array_type: str
    direction: str
    price_upper: float
    price_lower: float
    #: Structural anchor only (swing/candle/level).  Never a derived 50%.
    anchor_price: float | None
    formed_at: int
    swept_at: int | None
    invalidated_at: int | None
    #: CB-07 knowledge time (epoch ms, same domain as ``formed_at``): the
    #: confirmation bar of the producer object, never before it.
    known_at: int
    #: LQ-06 state: ``intact`` / ``swept`` / ``broken``.  The old ``is_active``
    #: boolean is kept as a compatibility property (``state != "broken"``).
    state: ArrayState
    #: The three "50%" objects are separate, typed fields; at most one is set
    #: on an array and its class names the producing object (§7.1, X8).
    range_equilibrium: RangeEquilibrium | None = None
    gap_ce: GapConsequentEncroachment | None = None
    ob_body_mean: OrderBlockBodyMean | None = None
    htf_range_id: str = ""
    engine_version: str = "ict-arrays-1.0.0"
    #: AS-04: the declared side tag and the mode that produced it.  ``side`` is
    #: ``None`` when the declared mode cannot locate the array (e.g. a
    #: by-position array straddling price); no default side is invented.
    side: str | None = None
    side_mode: str = ""
    #: AR-09/AR-23: the first return into an inverted/breaker zone after its
    #: creation, recorded separately from ``swept_at`` because a retest is the
    #: **use** event, not an invalidation.
    retest_at: int | None = None
    #: AR-21: the bar the parent OB failed (breakers only).
    failure_bar: int | None = None
    #: V22: the declared IFVG removal rule (empty when undeclared).
    removal_mode: str = ""
    #: AS-09: reference geometry only; never an entry array.
    reference_only: bool = False

    def __post_init__(self) -> None:
        validate_array_state(self.state)

    @property
    def is_active(self) -> bool:
        """Compatibility view of the retired ``is_active`` field.

        It keeps the historical meaning -- the array has not been invalidated
        (``state != "broken"``) -- so existing readers do not change behaviour.
        LQ-06 gates that need *only* an ``intact`` level must read ``state``
        (or :meth:`state_at`), not this flag.
        """
        return self.state != "broken"

    @property
    def active(self) -> bool:
        """Alias for :attr:`is_active` for callers written against concepts."""
        return self.is_active

    def state_at(self, at: int) -> ArrayState | None:
        """LQ-06 state at epoch-ms ``at``; ``None`` before ``known_at``."""
        return knowledge_state(self.known_at, at, swept_at=self.swept_at,
                               broken_at=self.invalidated_at)

    def is_known_at(self, at: int) -> bool:
        """CB-07: true only when the array was already knowable at ``at``."""
        return int(self.known_at) <= int(at)

    def row(self) -> tuple:
        dt = lambda ms: datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc) if ms else None
        return (
            self.array_id, self.symbol, self.interval, self.array_type,
            self.direction, float(self.price_upper), float(self.price_lower),
            self.anchor_price, dt(self.formed_at), dt(self.swept_at),
            dt(self.invalidated_at), 1 if self.is_active else 0,
            self.htf_range_id, self.engine_version, datetime.now(timezone.utc),
        )

    def to_public(self) -> dict[str, Any]:
        return {
            "array_id": self.array_id, "symbol": self.symbol,
            "interval": self.interval, "array_type": self.array_type,
            "direction": self.direction, "price_upper": self.price_upper,
            "price_lower": self.price_lower, "anchor_price": self.anchor_price,
            "range_equilibrium": _midpoint(self.range_equilibrium),
            "gap_ce": _midpoint(self.gap_ce),
            "ob_body_mean": _midpoint(self.ob_body_mean),
            "formed_at": self.formed_at, "swept_at": self.swept_at,
            "invalidated_at": self.invalidated_at, "known_at": self.known_at,
            "state": self.state, "is_active": self.is_active,
            "side": self.side, "side_mode": self.side_mode,
            "retest_at": self.retest_at,
            "failure_bar": self.failure_bar,
            "removal_mode": self.removal_mode,
            "reference_only": self.reference_only,
        }


def _midpoint(value: RangeEquilibrium | GapConsequentEncroachment
              | OrderBlockBodyMean | None) -> dict[str, Any] | None:
    """Serialize a typed "50%" without collapsing its kind into a bare float."""
    if value is None:
        return None
    return {"kind": value.kind, "value": float(value.value)}


def _direction(value: str) -> str:
    return "bullish" if value == "bull" else "bearish"


def build_ict_arrays(frame: pd.DataFrame, symbol: str, interval: str, *,
                     config: ICTConfig | None = None,
                     side_mode: str | None = None) -> list[ICTArray]:
    """Build the complete array inventory for ``frame``.

    The returned list is the selection inventory: every constructed array is
    present, including active arrays older than the newest per type.  Use
    :func:`present_ict_arrays` when the caller only needs a drawable view.

    P1-09: every array carries its AS-04 ``side``/``side_mode`` tag, an IFVG
    carries its AR-09 ``retest_at`` and any declared V22 removal, and a breaker
    carries its AR-21 failure bar and AR-23 retest.  ``config`` exposes the
    declared V19/V20/V22/AR-13 choices; the defaults are the labelled repo
    readings documented on :class:`ICTConfig`.
    """
    if frame is None or len(frame) < 8:
        return []
    df = frame.reset_index(drop=True).copy()
    ms = [dt_to_ms(pd.Timestamp(x).to_pydatetime()) for x in df["date"]]
    cfg = config or ICTConfig()
    last_close = float(df["close"].iloc[-1])
    tag_mode = cfg.array_side_mode if side_mode is None else side_mode

    pivots = find_pivots(df, cfg.swing_len, 1)
    swings = build_zigzag(pivots)
    events, _ = detect_structure(df, swings)

    fvgs = detect_fvgs(df, cfg)
    ifvgs = (detect_ifvg(list(fvgs), df, removal=(cfg.ifvg_removal or None))
             if cfg.show_ifvg else [])
    bprs = (detect_bpr(list(fvgs), df, candle_floor=cfg.bpr_candle_floor)
            if cfg.show_bpr else [])
    # A breaker is a failed order block (PP:C9/R4); the declared tier needs the
    # sweep inventory at detection time (P0-20).  P1-09 adds the declared V20
    # failure test/geometry and the AR-23 retest gate (the retest is a use
    # condition, so it is recorded on the object, never used to filter it out).
    # The sweep inventory is built first so the V19 ``grab`` gate and the
    # sequenced breaker tier read the same objects the analysis path does.
    liquidity = detect_liquidity(df, pivots, cfg)
    sessions = detect_session_levels(df)
    key_levels = detect_key_levels(df, swings, sessions, liquidity)
    sweeps = (detect_sweeps(df, key_levels, reclaim_window=cfg.sweep_reclaim_bars)
              if cfg.show_sweeps else [])
    order_blocks = detect_order_blocks(df, events, swings, cfg,
                                       fvgs=fvgs, sweeps=sweeps)
    breakers = detect_breakers(order_blocks, sweeps, tier=cfg.breaker_tier,
                               df=df, fvgs=fvgs,
                               failure_test=cfg.breaker_failure_test,
                               geometry=cfg.breaker_geometry)

    cisds = []
    if cfg.show_cisd:
        cisd_levels = [(s.price, s.kind, s.confirmed_at) for s in swings]
        cisd_zones = [(f.top, f.bottom, f.created, f.direction)
                      for f in list(fvgs) + list(ifvgs)]
        cisds = detect_cisd(df, cisd_config(cfg), cisd_levels, cisd_zones)

    dealing, _dealing_diagnostic = detect_dealing_range_state(
        df, swings, swing_len=cfg.swing_len)

    arrays: list[ICTArray] = []
    serial = 0

    def add(kind: str, direction: str, upper: float, lower: float,
            formed: int, *, known_at: int | None = None,
            swept: int | None = None,
            invalidated: int | None = None, anchor: float | None = None,
            state: ArrayState | None = None,
            range_equilibrium: RangeEquilibrium | None = None,
            gap_ce: GapConsequentEncroachment | None = None,
            ob_body_mean: OrderBlockBodyMean | None = None,
            retest_at: int | None = None,
            failure_bar: int | None = None,
            removal_mode: str = "",
            reference_only: bool = False) -> None:
        nonlocal serial
        serial += 1
        upper, lower = float(upper), float(lower)
        if lower > upper:
            upper, lower = lower, upper
        formed = int(formed)
        # CB-07: the knowledge time is derived from the confirmation bar and can
        # never precede the bar that created the object.
        known = formed if known_at is None else int(known_at)
        if known < formed:
            raise ValueError(
                f"{kind} known_at={known} precedes its confirmation bar "
                f"formed_at={formed} (CB-07)")
        # A zero-height object is a line/invalid array, never a tradable zone.
        if not upper > lower and invalidated is None:
            invalidated = formed
        if state is None:
            if invalidated is not None:
                state = "broken"
            elif swept is not None:
                state = "swept"
            else:
                state = "intact"
        else:
            validate_array_state(state)
        # AS-04: the side tag is produced by the declared mode and carried with
        # it.  A neutral object has no by-type side; no side is invented.
        side = None
        if direction in ("bullish", "bearish") or tag_mode != "by_type":
            tag = array_side(direction, price=last_close, top=upper,
                             bottom=lower, mode=tag_mode)
            side = tag.side
        stable = f"{symbol}|{interval}|{kind}|{formed}|{serial}"
        aid = "ARR-" + hashlib.sha1(stable.encode()).hexdigest()[:20]
        arrays.append(ICTArray(
            aid, symbol.upper(), interval, kind, direction,
            upper, lower, anchor, formed,
            swept, invalidated, known, state,
            range_equilibrium=range_equilibrium, gap_ce=gap_ce,
            ob_body_mean=ob_body_mean,
            side=side, side_mode=tag_mode, retest_at=retest_at,
            failure_bar=failure_bar, removal_mode=removal_mode,
            reference_only=reference_only,
        ))

    for f in fvgs:
        invalidated = None if f.active else ms[f.right]
        # AR-10(a): a wick test is the LQ-06 ``swept`` state, not a break.
        swept = ms[f.touched_at] if f.touched_at is not None else None
        add("FVG", _direction(f.direction), f.top, f.bottom, ms[f.created],
            known_at=ms[f.known_at], swept=swept, invalidated=invalidated,
            gap_ce=GapConsequentEncroachment(f.ce))

    for f in ifvgs:
        invalidated = ms[f.removed_at] if f.removed_at is not None else None
        add("IFVG", _direction(f.direction), f.top, f.bottom, ms[f.created],
            known_at=ms[f.known_at],
            retest_at=(ms[f.t_retest] if f.t_retest is not None else None),
            invalidated=invalidated, removal_mode=f.removal_mode,
            gap_ce=GapConsequentEncroachment(f.ce))

    for f in bprs:
        invalidated = None if f.active else ms[f.right]
        swept = ms[f.touched_at] if f.touched_at is not None else None
        add("BPR", _direction(f.direction), f.top, f.bottom, ms[f.created],
            known_at=ms[f.known_at], swept=swept, invalidated=invalidated,
            gap_ce=GapConsequentEncroachment(f.ce))

    for ob in order_blocks:
        invalidated = None if ob.active else ms[ob.right]
        swept = ms[ob.swept_at] if ob.swept_at is not None else None
        add("OB", _direction(ob.direction), ob.top, ob.bottom, ms[ob.created],
            known_at=ms[ob.known_at], swept=swept, invalidated=invalidated,
            ob_body_mean=OrderBlockBodyMean(ob.mean))

    # A breaker is a zone; its knowledge time is the flip/confirmation bar.  The
    # AR-23 retest is recorded but never filters the object out: the breaker
    # exists at the failure bar and becomes *usable* only after the return.
    for br in breakers:
        confirmed = int(br.confirmed_at)
        add("BREAKER", _direction(br.direction), br.top, br.bottom,
            ms[confirmed], known_at=ms[confirmed],
            anchor=float((br.top + br.bottom) / 2.0),
            retest_at=(ms[br.retest_at] if br.retest_at is not None else None),
            failure_bar=(ms[br.failure_bar] if br.failure_bar is not None else None))

    # A CISD is a close-through level (a line), so it is emitted as an invalid
    # array rather than a zero-height zone.  Both directions share one type id.
    for c in cisds:
        add("CISD", _direction(c.direction), c.level, c.level, ms[c.end],
            known_at=ms[c.end], anchor=float(c.level))

    # DR-00/SN-R07: only a structural swing pair may be a dealing range.  The
    # old fixed-count 40-bar high/low fallback is gone -- no structural pair
    # means no DEALING_RANGE array (the diagnostic explains the absence where a
    # caller needs it, P0-16).  The range CE is a typed field, never
    # ``anchor_price`` (X8).
    if dealing is not None:
        add("DEALING_RANGE", "neutral", dealing.top, dealing.bottom,
            ms[dealing.known_at], known_at=ms[dealing.known_at],
            state=dealing.state,
            range_equilibrium=RangeEquilibrium(dealing.ce))

    return arrays


#: Repo presentation caps: how many arrays of each type a chart/dashboard view
#: may draw.  They are deliberately not used by :func:`build_ict_arrays`
#: (P0-14): a cap is a display decision, not a selection decision.
PRESENTATION_CAPS: dict[str, int] = {
    "FVG": 8, "IFVG": 8, "BPR": 8, "OB": 8, "BREAKER": 8, "CISD": 5,
    "DEALING_RANGE": 1,
}


def present_ict_arrays(arrays: list[ICTArray], *,
                       caps: dict[str, int] | None = None,
                       max_inactive: int = 3) -> list[ICTArray]:
    """Return the drawable presentation view of a full inventory.

    Only this function applies the per-kind caps (``PRESENTATION_CAPS`` by
    default) and the small tail of inactive arrays.  A type absent from
    ``caps`` is shown in full, so a future array family cannot be silently
    dropped by an incomplete presentation table.  Selection must always read
    :func:`build_ict_arrays` directly (P0-14, §7.3).
    """
    limits = PRESENTATION_CAPS if caps is None else caps
    active = [a for a in arrays if a.is_active]
    inactive = ([a for a in arrays if not a.is_active][-max_inactive:]
                if max_inactive > 0 else [])
    selected: list[ICTArray] = []
    # Preserve construction order between families while capping within one.
    for kind in dict.fromkeys(a.array_type for a in active):
        subset = [a for a in active if a.array_type == kind]
        limit = limits.get(kind)
        selected.extend(subset if limit is None else subset[-max(int(limit), 0):])
    return selected + inactive


def scan_ict_arrays(arrays: list[ICTArray], *, roster: object,
                    order_mode: str = DEFAULT_ARRAY_ORDER_MODE,
                    total_order: str = DEFAULT_ARRAY_TOTAL_ORDER,
                    side_mode: str | None = None,
                    direction: object | None = None,
                    half: str | None = None,
                    price: float | None = None,
                    at: int | None = None) -> tuple[list[ICTArray], object]:
    """§7.3 selection over the realtime inventory (V09/V26, AS-01..AS-09).

    A thin delegation to :func:`ict_trader.concepts.scan_arrays`: the roster,
    side mode and order are declared once in ``concepts.py`` (A7) and this
    wrapper only supplies the realtime array objects.  When ``side_mode`` is
    omitted it is read from the inventory's own AS-04 declaration, so a scan
    cannot silently re-tag a by-position inventory as by-type.  The declared
    order is a **scan order**, not a guarantee price cannot trade through a
    breaker (AS-05); freshness demotes instead of deleting (AS-07).
    """
    if half is not None and half not in ARRAY_SIDES:
        raise ValueError(
            f"unknown admitted half {half!r}; declare one of {ARRAY_SIDES}")
    if side_mode is None:
        modes = {array.side_mode for array in arrays if array.side_mode}
        if len(modes) > 1:
            raise ValueError(
                f"the inventory mixes side modes {sorted(modes)}; scan one "
                f"declared mode at a time (AS-04)")
        side_mode = modes.pop() if modes else DEFAULT_ARRAY_SIDE_MODE
    return scan_arrays(
        arrays, roster=roster, order_mode=order_mode, total_order=total_order,
        side_mode=side_mode, direction=direction, half=half, price=price,
        at=at)
