"""FVG/OB confluence and body-close invalidation.

Source: ICT_Corpus doc 233 ("FVG vs Order Block"), 2026-07.
No new entry thresholds are introduced here.
"""
from __future__ import annotations

from typing import Any


def overlap_ratio(a: dict, b: dict) -> float:
    lo = max(float(a["bottom"]), float(b["bottom"]))
    hi = min(float(a["top"]), float(b["top"]))
    if hi <= lo:
        return 0.0
    width = max(float(a["top"]) - float(a["bottom"]), 1e-12)
    return max(0.0, min(1.0, (hi - lo) / width))


def annotate_confluence(fvgs: list[dict], obs: list[dict], *,
                        min_overlap: float = 0.8) -> list[dict[str, Any]]:
    out = []
    for f in fvgs:
        best = 0.0
        for o in obs:
            if f.get("direction") != o.get("direction"):
                continue
            best = max(best, overlap_ratio(f, o))
        out.append({
            **f,
            "ob_overlap": round(best, 4),
            "pda_confluence": "fvg_inside_ob" if best >= min_overlap
            else "partial_overlap" if best > 0 else "separate",
        })
    return out


def body_close_invalidated(fvg: dict, last_bar: dict) -> str | None:
    """Return INVERTED when a body closes through the far edge."""
    close = float(last_bar["close"])
    if fvg["direction"] == "bullish" and close < float(fvg["bottom"]):
        return "INVERTED"
    if fvg["direction"] == "bearish" and close > float(fvg["top"]):
        return "INVERTED"
    return None
