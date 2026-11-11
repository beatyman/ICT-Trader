"""Small, deterministic ICT evidence helpers.

No strategy thresholds live here.  These functions only map already-computed
events into JSON-safe evidence and record explicit data gaps.
"""
from __future__ import annotations

from typing import Any


def extract_sweep(sweeps: list[dict], *, direction: str,
                  source_kline_open_time: int) -> dict | None:
    want_type = "SSL" if direction == "long" else "BSL"
    want_bias = "bullish" if direction == "long" else "bearish"
    candidates = [
        s for s in sweeps
        if s.get("sweep_type") == want_type
        and s.get("direction") == want_bias
        and int(s.get("time", 0)) <= int(source_kline_open_time)
        and int(source_kline_open_time) <= int(s.get("expires_at", 0))
    ]
    if not candidates:
        return None
    s = max(candidates, key=lambda x: int(x["time"]))
    return {
        "type": s["sweep_type"],
        "direction": s["direction"],
        "time": int(s["time"]),
        "level": float(s["level"]),
        # doc 69 L181: the stop anchors on the sweep extreme, not on `level`.
        "extreme": float(s.get("sweep_price") or s["level"]),
        "reclaim_atr": float(s.get("reclaim_strength", 0.0)),
        "expires_at": int(s["expires_at"]),
    }


def structure_evidence(events: list[Any], end_idx: int,
                       times: list[int]) -> dict[str, Any] | None:
    usable = [e for e in events
              if int(getattr(e, "index", 0)) <= end_idx]
    if not usable:
        return None
    e = usable[-1]
    return {
        "event": str(e.kind),
        "direction": "bullish" if e.direction == "bull" else "bearish",
        "price": float(e.price),
        "time": int(times[min(int(e.index), len(times) - 1)]),
    }


def breaker_evidence(pd_arrays: list[Any], end_idx: int,
                     *, lookback: int = 500, limit: int = 3) -> list[dict[str, Any]]:
    out = []
    for a in pd_arrays:
        if str(getattr(a, "kind", "")) != "breaker":
            continue
        at = int(getattr(a, "confirmed_at", 0))
        if at > end_idx or end_idx - at > lookback:
            continue
        out.append({
            "direction": "bullish" if a.direction == "bull" else "bearish",
            "top": float(a.top),
            "bottom": float(a.bottom),
        })
    return out[-limit:]


def _box(b, kind: str, inverted: bool = False) -> dict:
    return {
        "kind": kind,
        "direction": "bullish" if getattr(b, "direction", "") == "bull" else "bearish",
        "top": float(b.top),
        "bottom": float(b.bottom),
        "created": int(getattr(b, "created", 0)),
        "confirmed_at": int(getattr(b, "confirmed_at", getattr(b, "created", 0))),
        "inverted": bool(inverted),
    }


def pd_array_evidence(result: Any, end_idx: int, *, limit: int = 5) -> dict:
    fvgs = [f for f in getattr(result, "fvgs", [])
            if int(getattr(f, "created", 0)) <= end_idx]
    obs = [o for o in getattr(result, "order_blocks", [])
           if int(getattr(o, "created", 0)) <= end_idx]
    arrays = [a for a in getattr(result, "pd_arrays", [])
              if int(getattr(a, "confirmed_at", 0)) <= end_idx]
    return {
        "fvg": [_box(f, "fvg", bool(getattr(f, "inverted", False)))
                for f in fvgs[-limit:]],
        "ifvg": [_box(f, "ifvg", True) for f in fvgs
                 if bool(getattr(f, "inverted", False))][-limit:],
        "ob": [_box(o, "ob") for o in obs[-limit:]],
        "pd_array": [{
            "kind": str(getattr(a, "kind", "")),
            "direction": "bullish" if getattr(a, "direction", "") == "bull" else "bearish",
            "top": float(a.top), "bottom": float(a.bottom),
        } for a in arrays[-limit:]],
    }


def hidden_ob_evidence(frame: Any, end_idx: int, *, limit: int = 3,
                       max_scan: int = 400) -> list[dict]:
    """Hidden order blocks near ``end_idx`` (doc 570).

    Bar indices are relative to the scanned window, so ``bars_ago`` is exposed
    instead of absolute indices.
    """
    from .hidden_ob import detect_hidden_ob

    if frame is None or len(frame) < 3 or end_idx < 1:
        return []
    lo = max(0, int(end_idx) - max_scan + 1)
    sub = frame.iloc[lo:int(end_idx) + 1]
    if len(sub) < 3:
        return []
    zones = detect_hidden_ob(sub)
    n = len(sub)
    out = []
    for z in zones[-limit:]:
        row = z.to_public()
        row.pop("first_bar", None)
        row.pop("second_bar", None)
        row["bars_ago"] = n - 1 - int(z.second_bar)
        out.append(row)
    return out


def ifvg_evidence(ifvgs: list[dict], dealing_range: Any = None) -> dict | None:
    """Annotate the latest IFVG with its doc-502 context and CE entry price."""
    from .pda_confluence import ifvg_ce, valid_ifvg_context

    if not ifvgs:
        return None
    best = ifvgs[-1]
    ce = ifvg_ce(best)
    zone = "unknown"
    if dealing_range is not None:
        try:
            zone = dealing_range.zone(ce)
        except Exception:  # pragma: no cover - zone() is total, kept defensive
            zone = "unknown"
    ok, reasons = valid_ifvg_context(str(best.get("direction", "")), zone)
    return {
        "direction": best.get("direction"), "top": best.get("top"),
        "bottom": best.get("bottom"), "ce": ce, "pd_zone": zone,
        "tradable": bool(ok), "reasons": list(reasons),
    }


def rvol_evidence(frame: Any, end_idx: int, *, window: int = 20) -> float | None:
    if "volume" not in frame or end_idx < 1:
        return None
    vol = frame["volume"].astype(float).to_numpy()
    lo = max(0, end_idx - window)
    base = float(vol[lo:end_idx].mean()) if end_idx > lo else 0.0
    if base <= 0:
        return None
    return float(vol[end_idx] / base)


def htf_bias_evidence(frame: Any, end_idx: int) -> dict | None:
    import pandas as pd
    from .structure import structure_bias

    sub = frame.iloc[:end_idx + 1].copy()
    if len(sub) < 20:
        return None
    sub["date"] = pd.to_datetime(sub["date"], utc=True)
    sub = sub.set_index("date")
    out = {}
    for rule in ("4h", "1D"):
        x = sub.resample(rule).agg({
            "open": "first", "high": "max", "low": "min",
            "close": "last", "volume": "sum",
        }).dropna().reset_index()
        if len(x) < 20:
            out[rule] = None
            continue
        b = structure_bias(x)
        out[rule] = {"bias": b["bias"], "event": b["event"],
                     "event_time": b["event_time"]}
    return out
