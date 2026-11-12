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
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import pandas as pd

from ...concepts import (
    ICTConfig,
    build_zigzag,
    cisd_config,
    detect_cisd,
    detect_dealing_range,
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
    anchor_price: float | None
    formed_at: int
    swept_at: int | None
    invalidated_at: int | None
    is_active: bool
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
            "formed_at": self.formed_at, "swept_at": self.swept_at,
            "invalidated_at": self.invalidated_at, "is_active": self.is_active,
        }


def _direction(value: str) -> str:
    return "bullish" if value == "bull" else "bearish"


def build_ict_arrays(frame: pd.DataFrame, symbol: str, interval: str,
                     max_inactive: int = 3) -> list[ICTArray]:
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

    dealing = detect_dealing_range(df, swings, swing_len=cfg.swing_len)

    arrays: list[ICTArray] = []
    serial = 0

    def add(kind: str, direction: str, upper: float, lower: float,
            formed: int, swept: int | None = None,
            invalidated: int | None = None, anchor: float | None = None) -> None:
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
        ))

    for f in fvgs:
        invalidated = None if f.active else ms[f.right]
        add("FVG", _direction(f.direction), f.top, f.bottom, ms[f.created],
            swept=invalidated, invalidated=invalidated)

    for f in ifvgs:
        add("IFVG", _direction(f.direction), f.top, f.bottom, ms[f.created])

    for ob in order_blocks:
        invalidated = None if ob.active else ms[ob.right]
        add("OB", _direction(ob.direction), ob.top, ob.bottom, ms[ob.created],
            invalidated=invalidated)

    # A CISD is a close-through level (a line), so it is emitted as an invalid
    # array rather than a zero-height zone.  Both directions share one type id.
    for c in cisds:
        add("CISD", _direction(c.direction), c.level, c.level, ms[c.end],
            anchor=float(c.level))

    if dealing is not None:
        add("DEALING_RANGE", "neutral", dealing.top, dealing.bottom,
            ms[dealing.known_at], anchor=dealing.ce)

    active = [a for a in arrays if a.is_active]
    inactive = [a for a in arrays if not a.is_active][-max_inactive:]
    limits = {
        "FVG": 8, "IFVG": 8, "OB": 8, "CISD": 5, "DEALING_RANGE": 1,
    }
    selected: list[ICTArray] = []
    for kind, limit in limits.items():
        selected.extend([a for a in active if a.array_type == kind][-limit:])
    return selected + inactive
