"""Scalper FVG-nesting setup (the "stop overcomplicating this" model).

Five steps:
  1. wait for a 1H FVG to form
  2. wait for price to enter that 1H FVG
  3. wait for a new 5m FVG to form inside/near it
  4. enter on that 5m FVG (its CE)
  5. target 2R

Returns a dict with entry/sl/tp/2R and the two FVGs, or None.
"""
from __future__ import annotations

import pandas as pd

from .concepts import ICTConfig, analyze


def _active_fvgs(res):
    return [f for f in res.fvgs if getattr(f, "active", True) and not getattr(f, "inverted", False)]


def _net_rr(entry, sl, tp, direction):
    risk = (entry - sl) if direction == "long" else (sl - entry)
    reward = (tp - entry) if direction == "long" else (entry - tp)
    if risk <= 0:
        return None, None
    cost = entry * 0.0007 + tp * 0.0007
    gross = reward / risk
    net = (reward - cost) / (risk + cost)
    return gross, net


def detect_scalper(htf_df: pd.DataFrame, ltf_df: pd.DataFrame) -> dict | None:
    """htf_df = 1H bars, ltf_df = 5m bars (both up to the last closed bar)."""
    cfg = ICTConfig(mode="historical", show_sessions=False, show_htf_cisd=False,
                    cisd_all=False, cisd_min_grade=0, show_sweeps=False)
    htf = analyze(htf_df, cfg)
    ltf = analyze(ltf_df, cfg)
    price = float(ltf_df["close"].iloc[-1])
    now = ltf_df["date"].iloc[-1]

    # 1. latest active 1H FVG
    h_fvgs = sorted(_active_fvgs(htf), key=lambda f: f.created)
    if not h_fvgs:
        return None
    h = h_fvgs[-1]

    # 2. price must currently be inside the 1H FVG
    if not (float(h.bottom) <= price <= float(h.top)):
        return None

    # 3. a 5m FVG formed after the 1H FVG was created, overlapping its zone
    h_created_dt = htf_df["date"].iloc[h.created]
    l_fvgs = sorted(_active_fvgs(ltf), key=lambda f: f.created)
    candidates = []
    for l in l_fvgs:
        l_dt = ltf_df["date"].iloc[l.created]
        # same direction as the 1H FVG (continuation)
        if l.direction != h.direction:
            continue
        if l_dt < h_created_dt:
            continue
        # overlap with the 1H FVG zone
        if float(l.bottom) > float(h.top) or float(l.top) < float(h.bottom):
            continue
        candidates.append(l)
    if not candidates:
        return None
    l = candidates[-1]  # most recent qualifying 5m FVG

    # 4. entry on the 5m FVG CE; 5. 2R target
    direction = "long" if l.direction == "bull" else "short"
    entry = float(l.ce) if getattr(l, "ce", None) else (float(l.bottom) + float(l.top)) / 2
    if direction == "long":
        sl = float(l.bottom)
        risk = entry - sl
        tp = entry + 2 * risk
    else:
        sl = float(l.top)
        risk = sl - entry
        tp = entry - 2 * risk
    gross, net = _net_rr(entry, sl, tp, direction)
    return {
        "direction": direction,
        "entry": round(entry, 6),
        "sl": round(sl, 6),
        "tp": round(tp, 6),
        "rr_2": True,
        "gross_rr": None if gross is None else round(gross, 3),
        "net_rr": None if net is None else round(net, 3),
        "htf_fvg": {"bottom": round(float(h.bottom), 6), "top": round(float(h.top), 6),
                    "created": str(h_created_dt)},
        "ltf_fvg": {"bottom": round(float(l.bottom), 6), "top": round(float(l.top), 6),
                    "created": str(ltf_df["date"].iloc[l.created])},
        "price": round(price, 6),
        "as_of": str(now),
    }
