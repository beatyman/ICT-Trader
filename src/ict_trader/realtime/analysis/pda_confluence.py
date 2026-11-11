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


def valid_ifvg_context(ifvg_direction: str, pd_zone: str,
                       failed_mss: bool = False) -> tuple[bool, tuple[str, ...]]:
    """Doc 502: an IFVG is only reliable in three contexts.

    L118: "is the IFVG forming in the premium zone, discount zone, or after a
    failed MSS? These are the three reliable conditions. Outside them, the IFVG
    is a coin flip."  A *bullish* IFVG (a violated bearish FVG) belongs in
    premium; a *bearish* IFVG belongs in discount.
    """
    reasons: list[str] = []
    if ifvg_direction == "bullish":
        if pd_zone not in ("premium", "deep_premium") and not failed_mss:
            reasons.append("bullish_ifvg_outside_premium")
    elif ifvg_direction == "bearish":
        if pd_zone not in ("discount", "deep_discount") and not failed_mss:
            reasons.append("bearish_ifvg_outside_discount")
    else:
        reasons.append("invalid_direction")
    return (not reasons, tuple(reasons))


def ifvg_ce(zone: dict) -> float:
    """Doc 502 L119: "the consequent encroachment (50% level) is the precise entry"."""
    return (float(zone["top"]) + float(zone["bottom"])) / 2.0


def ifvg_stop(direction: str, zone: dict, buffer: float = 0.0) -> float:
    """Doc 502 L121: stop beyond the IFVG extreme, never inside the zone."""
    if direction == "bullish":
        return float(zone["bottom"]) - abs(float(buffer))
    return float(zone["top"]) + abs(float(buffer))
