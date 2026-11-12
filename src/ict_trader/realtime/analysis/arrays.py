"""Persistent ICT array lifecycle (FVG/IFVG/OB/CISD/Dealing Range).

This module is a **lifecycle wrapper** over the canonical detectors in
:mod:`ict_trader.concepts`.  It owns identity, symbol/interval, anchor,
formed/swept/invalidated times and the active flag so arrays can be replayed
and audited long after the chart window moved on.  It deliberately contains no
FVG/IFVG/OB/CISD/range arithmetic of its own: those bounds come from
``find_pivots``/``build_zigzag``, ``detect_structure``, ``detect_fvgs``,
``detect_ifvg``, ``detect_order_blocks``, ``detect_cisd`` and
``detect_dealing_range`` (P0-10, A7/X8, §7.1).

A zero-height "zone" is a **line**, not an array.  Rather than invent a height
for it, the lifecycle records it as an invalid array (``is_active=False`` and
``invalidated_at == formed_at``).  CISD lines therefore use one symmetric type
id (``CISD``) in both directions; direction is carried by ``direction``.

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
    ICTConfig,
    GapConsequentEncroachment,
    OrderBlockBodyMean,
    RangeEquilibrium,
    build_zigzag,
    cisd_config,
    detect_cisd,
    detect_dealing_range_state,
    detect_fvgs,
    detect_ifvg,
    detect_order_blocks,
    detect_structure,
    find_pivots,
)
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
    is_active: bool
    #: The three "50%" objects are separate, typed fields; at most one is set
    #: on an array and its class names the producing object (§7.1, X8).
    range_equilibrium: RangeEquilibrium | None = None
    gap_ce: GapConsequentEncroachment | None = None
    ob_body_mean: OrderBlockBodyMean | None = None
    htf_range_id: str = ""
    engine_version: str = "ict-arrays-1.0.0"

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
            "invalidated_at": self.invalidated_at, "is_active": self.is_active,
        }


def _midpoint(value: RangeEquilibrium | GapConsequentEncroachment
              | OrderBlockBodyMean | None) -> dict[str, Any] | None:
    """Serialize a typed "50%" without collapsing its kind into a bare float."""
    if value is None:
        return None
    return {"kind": value.kind, "value": float(value.value)}


def _direction(value: str) -> str:
    return "bullish" if value == "bull" else "bearish"


def build_ict_arrays(frame: pd.DataFrame, symbol: str, interval: str) -> list[ICTArray]:
    """Build the complete array inventory for ``frame``.

    The returned list is the selection inventory: every constructed array is
    present, including active arrays older than the newest per type.  Use
    :func:`present_ict_arrays` when the caller only needs a drawable view.
    """
    if frame is None or len(frame) < 8:
        return []
    df = frame.reset_index(drop=True).copy()
    ms = [dt_to_ms(pd.Timestamp(x).to_pydatetime()) for x in df["date"]]
    cfg = ICTConfig()

    pivots = find_pivots(df, cfg.swing_len, 1)
    swings = build_zigzag(pivots)
    events, _ = detect_structure(df, swings)

    fvgs = detect_fvgs(df, cfg)
    ifvgs = detect_ifvg(list(fvgs), df) if cfg.show_ifvg else []
    order_blocks = detect_order_blocks(df, events, swings, cfg)

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
            formed: int, swept: int | None = None,
            invalidated: int | None = None, anchor: float | None = None,
            range_equilibrium: RangeEquilibrium | None = None,
            gap_ce: GapConsequentEncroachment | None = None,
            ob_body_mean: OrderBlockBodyMean | None = None) -> None:
        nonlocal serial
        serial += 1
        upper, lower = float(upper), float(lower)
        if lower > upper:
            upper, lower = lower, upper
        # A zero-height object is a line/invalid array, never a tradable zone.
        if not upper > lower and invalidated is None:
            invalidated = formed
        stable = f"{symbol}|{interval}|{kind}|{formed}|{serial}"
        aid = "ARR-" + hashlib.sha1(stable.encode()).hexdigest()[:20]
        arrays.append(ICTArray(
            aid, symbol.upper(), interval, kind, direction,
            upper, lower, anchor, int(formed),
            swept, invalidated, invalidated is None,
            range_equilibrium=range_equilibrium, gap_ce=gap_ce,
            ob_body_mean=ob_body_mean,
        ))

    for f in fvgs:
        invalidated = None if f.active else ms[f.right]
        add("FVG", _direction(f.direction), f.top, f.bottom, ms[f.created],
            swept=invalidated, invalidated=invalidated,
            gap_ce=GapConsequentEncroachment(f.ce))

    for f in ifvgs:
        add("IFVG", _direction(f.direction), f.top, f.bottom, ms[f.created],
            gap_ce=GapConsequentEncroachment(f.ce))

    for ob in order_blocks:
        invalidated = None if ob.active else ms[ob.right]
        add("OB", _direction(ob.direction), ob.top, ob.bottom, ms[ob.created],
            invalidated=invalidated, ob_body_mean=OrderBlockBodyMean(ob.mean))

    # A CISD is a close-through level (a line), so it is emitted as an invalid
    # array rather than a zero-height zone.  Both directions share one type id.
    for c in cisds:
        add("CISD", _direction(c.direction), c.level, c.level, ms[c.end],
            anchor=float(c.level))

    # DR-00/SN-R07: only a structural swing pair may be a dealing range.  The
    # old fixed-count 40-bar high/low fallback is gone -- no structural pair
    # means no DEALING_RANGE array (the diagnostic explains the absence where a
    # caller needs it, P0-16).  The range CE is a typed field, never
    # ``anchor_price`` (X8).
    if dealing is not None:
        add("DEALING_RANGE", "neutral", dealing.top, dealing.bottom,
            ms[dealing.known_at],
            range_equilibrium=RangeEquilibrium(dealing.ce))

    return arrays


#: Repo presentation caps: how many arrays of each type a chart/dashboard view
#: may draw.  They are deliberately not used by :func:`build_ict_arrays`
#: (P0-14): a cap is a display decision, not a selection decision.
PRESENTATION_CAPS: dict[str, int] = {
    "FVG": 8, "IFVG": 8, "OB": 8, "CISD": 5, "DEALING_RANGE": 1,
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
