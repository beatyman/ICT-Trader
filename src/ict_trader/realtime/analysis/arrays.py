"""Persistent ICT array lifecycle (FVG/IFVG/OB/CISD/Dealing Range).

This layer is intentionally independent of chart colors.  It records the
anchor, formed/swept/invalidated times and active flag so arrays can be
replayed and audited long after the chart window moved on.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import pandas as pd

from ...concepts import build_zigzag, find_pivots
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


def build_ict_arrays(frame: pd.DataFrame, symbol: str, interval: str,
                     max_inactive: int = 3) -> list[ICTArray]:
    if frame is None or len(frame) < 8:
        return []
    df = frame.reset_index(drop=True).copy()
    ms = [dt_to_ms(pd.Timestamp(x).to_pydatetime()) for x in df["date"]]
    high = df["high"].astype(float).to_numpy()
    low = df["low"].astype(float).to_numpy()
    close = df["close"].astype(float).to_numpy()
    open_ = df["open"].astype(float).to_numpy()
    n = len(df)
    atr = sum(float(high[i] - low[i]) for i in range(max(0, n - 14), n)) / max(1, min(14, n))
    min_gap = max(atr * 0.035, abs(float(close[-1])) * 0.00005)
    arrays: list[ICTArray] = []
    serial = 0

    def add(kind: str, direction: str, upper: float, lower: float,
            formed: int, swept: int | None = None,
            invalidated: int | None = None, anchor: float | None = None) -> None:
        nonlocal serial
        serial += 1
        stable = f"{symbol}|{interval}|{kind}|{formed}|{serial}"
        import hashlib
        aid = "ARR-" + hashlib.sha1(stable.encode()).hexdigest()[:20]
        arrays.append(ICTArray(
            aid, symbol.upper(), interval, kind, direction,
            max(upper, lower), min(upper, lower), anchor, formed,
            swept, invalidated, invalidated is None,
        ))

    for i in range(2, n - 1):
        a, c = i - 2, i
        bull = low[c] - high[a]
        bear = low[a] - high[c]
        if bull > min_gap:
            failed = next((j for j in range(i + 1, n) if close[j] < high[a]), None)
            add("FVG", "bullish", high[a], low[c], ms[a],
                swept=ms[failed] if failed is not None else None,
                invalidated=ms[failed] if failed is not None else None)
            if failed is not None:
                add("IFVG", "bearish", high[a], low[c], ms[failed],
                    swept=None, invalidated=None)
        elif bear > min_gap:
            failed = next((j for j in range(i + 1, n) if close[j] > low[a]), None)
            add("FVG", "bearish", low[a], high[c], ms[a],
                swept=ms[failed] if failed is not None else None,
                invalidated=ms[failed] if failed is not None else None)
            if failed is not None:
                add("IFVG", "bullish", low[a], high[c], ms[failed],
                    swept=None, invalidated=None)

    for i in range(1, n):
        body = abs(close[i] - open_[i])
        if body < atr * 0.95:
            continue
        bullish = close[i] > open_[i]
        prev_bear = close[i - 1] < open_[i - 1]
        if bullish != prev_bear:
            continue
        upper = max(open_[i - 1], close[i - 1], high[i - 1])
        lower = min(open_[i - 1], close[i - 1], low[i - 1])
        failed = next((j for j in range(i + 1, n)
                       if (close[j] < lower if bullish else close[j] > upper)), None)
        add("OB", "bullish" if bullish else "bearish", upper, lower, ms[i - 1],
            invalidated=ms[failed] if failed is not None else None)

    # CISD line: anchor the start of a same-direction run.
    bear_run = bull_run = 0
    bear_first = bull_first = 0.0
    for i in range(n):
        if close[i] < open_[i]:
            if bull_run >= 2 and close[i] < bull_first:
                add("CISD_BEARISH", "bearish", bull_first, bull_first,
                    ms[i], anchor=bull_first)
            if bear_run == 0:
                bear_first = open_[i]
            bear_run += 1
            bull_run = 0
        elif close[i] > open_[i]:
            if bear_run >= 2 and close[i] > bear_first:
                add("CISD", "bullish", bear_first, bear_first,
                    ms[i], anchor=bear_first)
            if bull_run == 0:
                bull_first = open_[i]
            bull_run += 1
            bear_run = 0

    # A dealing range from the last confirmed structural swing pair.
    # Doc 601 L56: the range is defined by "structural swing points - not by
    # arbitrary time periods or fixed candle counts"; doc 631 L56 says the same.
    # The 40-bar fallback only runs when the zigzag produced no usable pair.
    if n >= 20:
        swings = []
        try:
            swings = build_zigzag(find_pivots(df, 5, 1))
        except Exception:  # pragma: no cover - defensive, keeps the overlay alive
            swings = []
        last_t = n - 1
        hs = [s for s in swings
              if s.kind == "H" and int(getattr(s, "confirmed_at", s.index)) <= last_t]
        ls = [s for s in swings
              if s.kind == "L" and int(getattr(s, "confirmed_at", s.index)) <= last_t]
        if hs and ls:
            h, l = hs[-1], ls[-1]
            add("DEALING_RANGE", "neutral", float(h.price), float(l.price),
                ms[min(int(h.index), int(l.index))],
                anchor=(float(h.price) + float(l.price)) / 2.0)
        else:
            lo = int(min(range(max(0, n - 40), n), key=lambda i: low[i]))
            hi = int(max(range(max(0, n - 40), n), key=lambda i: high[i]))
            add("DEALING_RANGE", "neutral", high[hi], low[lo], ms[min(lo, hi)],
                anchor=(high[hi] + low[lo]) / 2.0)

    active = [a for a in arrays if a.is_active]
    inactive = [a for a in arrays if not a.is_active][-max_inactive:]
    limits = {
        "FVG": 8, "IFVG": 8, "OB": 8, "CISD": 5,
        "CISD_BEARISH": 5, "DEALING_RANGE": 1,
    }
    selected: list[ICTArray] = []
    for kind, limit in limits.items():
        selected.extend([a for a in active if a.array_type == kind][-limit:])
    return selected + inactive
