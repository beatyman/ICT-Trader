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
