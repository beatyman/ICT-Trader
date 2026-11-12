"""Immutable ICT feature snapshot for one symbol at one closed-bar as_of.

This is the v2 data contract.  It does not decide trades and does not call a
broker.  A snapshot may contain data_gaps; callers decide how strict to be.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import pandas as pd

from .pda_confluence import annotate_confluence
from .structure import pivot_bos_bias


TFS = ("1D", "4H", "1H", "15m")


@dataclass(frozen=True, slots=True)
class TFView:
    timeframe: str
    bias: str
    structure_event: str
    structure_price: float
    structure_time: int
    atr: float | None
    premium_discount: str = ""


@dataclass(frozen=True, slots=True)
class FeatureSnapshot:
    snapshot_id: str
    symbol: str
    as_of: datetime
    price: float
    views: dict[str, TFView]
    sweep: dict[str, Any] | None
    fvg: tuple[dict[str, Any], ...]
    ifvg: tuple[dict[str, Any], ...]
    ob: tuple[dict[str, Any], ...]
    breaker: tuple[dict[str, Any], ...]
    pd_array: tuple[dict[str, Any], ...]
    data_gaps: tuple[str, ...] = ()
    schema: str = "ict-feature-v2"

    def to_public(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "snapshot_id": self.snapshot_id,
            "symbol": self.symbol,
            "as_of": self.as_of.astimezone(timezone.utc).isoformat(),
            "price": self.price,
            "views": {k: {
                "timeframe": v.timeframe, "bias": v.bias,
                "structure_event": v.structure_event,
                "structure_price": v.structure_price,
                "structure_time": v.structure_time,
                "atr": v.atr, "premium_discount": v.premium_discount,
            } for k, v in self.views.items()},
            "sweep": self.sweep,
            "fvg": list(self.fvg), "ifvg": list(self.ifvg),
            "ob": list(self.ob), "breaker": list(self.breaker),
            "pd_array": list(self.pd_array),
            "data_gaps": list(self.data_gaps),
        }


def _atr(frame: pd.DataFrame, n: int = 14) -> float | None:
    if len(frame) < 2:
        return None
    high = frame["high"].astype(float).to_numpy()
    low = frame["low"].astype(float).to_numpy()
    close = frame["close"].astype(float).to_numpy()
    tr = pd.Series([
        max(high[i] - low[i], abs(high[i] - close[i - 1]),
            abs(low[i] - close[i - 1]))
        for i in range(1, len(frame))
    ])
    value = float(tr.tail(n).mean())
    return value if value > 0 else None


def _resample(frame: pd.DataFrame, rule: str) -> pd.DataFrame:
    sub = frame.copy()
    sub["date"] = pd.to_datetime(sub["date"], utc=True)
    out = (sub.set_index("date").resample(rule)
           .agg({"open": "first", "high": "max", "low": "min",
                 "close": "last", "volume": "sum"})
           .dropna().reset_index())
    return out


def build_feature_snapshot(symbol: str, frame: pd.DataFrame, result: Any,
                           *, as_of: datetime | None = None,
                           fvg_limit: int = 10,
                           sweep: dict[str, Any] | None = None) -> FeatureSnapshot:
    if frame is None or len(frame) < 120:
        raise ValueError("need at least 120 closed bars")
    df = frame.reset_index(drop=True).copy()
    as_of = as_of or pd.to_datetime(df["date"].iloc[-1], utc=True).to_pydatetime()
    price = float(df["close"].iloc[-1])
    views: dict[str, TFView] = {}
    gaps: list[str] = []

    for tf, rule in (("15m", None), ("1H", "1h"), ("4H", "4h"), ("1D", "1D")):
        x = df if rule is None else _resample(df, rule)
        if len(x) < 20:
            gaps.append(f"view_{tf.lower()}")
            continue
        b = pivot_bos_bias(x)
        views[tf] = TFView(
            timeframe=tf, bias=b["bias"], structure_event=b["event"],
            structure_price=float(b.get("swing_high") or b.get("swing_low") or 0.0),
            structure_time=int(b.get("event_time") or 0),
            atr=_atr(x),
        )

    raw_fvg = [{
        "direction": "bullish" if f.direction == "bull" else "bearish",
        "top": float(f.top), "bottom": float(f.bottom),
        "created": int(getattr(f, "created", 0)),
        "inverted": bool(getattr(f, "inverted", False)),
    } for f in getattr(result, "fvgs", [])[-fvg_limit:]]
    ob = tuple({
        "direction": "bullish" if o.direction == "bull" else "bearish",
        "top": float(o.top), "bottom": float(o.bottom),
    } for o in getattr(result, "order_blocks", [])[-fvg_limit:])
    fvg = tuple(annotate_confluence(raw_fvg, list(ob)))
    ifvg = tuple(x for x in fvg if x["inverted"])
    breaker = tuple([{
        "direction": "bullish" if a.direction == "bull" else "bearish",
        "top": float(a.top), "bottom": float(a.bottom),
    } for a in getattr(result, "pd_arrays", []) if a.kind == "breaker"][-5:])
    pd_arrays = tuple({
        "kind": str(a.kind),
        "direction": "bullish" if a.direction == "bull" else "bearish",
        "top": float(a.top), "bottom": float(a.bottom),
    } for a in getattr(result, "pd_arrays", [])[-fvg_limit:])

    if not views:
        gaps.append("no_timeframes")
    return FeatureSnapshot(
        snapshot_id=f"SNAP-{symbol}-{int(as_of.timestamp()*1000)}",
        symbol=symbol, as_of=as_of, price=price, views=views,
        sweep=sweep, fvg=fvg, ifvg=ifvg, ob=ob, breaker=breaker,
        pd_array=pd_arrays, data_gaps=tuple(sorted(set(gaps))),
    )
