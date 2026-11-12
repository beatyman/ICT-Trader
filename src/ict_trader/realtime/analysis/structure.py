"""Current LTF market-structure read from confirmed pivots.

This is the **repo pivot/BOS read**, not the corpus HH/HL bias primitive
(BI-01).  It is deliberately named ``pivot_bos_bias`` so it never shares the
name of :func:`ict_trader.context.structure_bias` (P0-09).  The bias is based
on the *latest closed bar* and confirmed swings; a BOS from two hours ago must
not authorize a new setup.
"""
from __future__ import annotations

from typing import Any

import pandas as pd

from ...concepts import (CHoCH_LABEL, structure_family_spec, structure_label)
from ...context import BiasDirection, serialize_bias


def pivot_bos_bias(frame: pd.DataFrame, *, left: int = 2, right: int = 2,
                   lookback: int = 120,
                   mss_family: str | None = None) -> dict[str, Any]:
    """Pivot 2/2 + body-close BOS with a 20-bar trend fallback (repo rule).

    Returned ``bias`` uses the one declared state vocabulary (BI-03):
    ``bullish`` / ``bearish`` / ``neutral``.  ``event`` is the pivot/BOS event
    name and is not a bias state.

    P1-07 adds the corpus **label** beside the repo ``event`` name: ``label`` is
    ``MSS``/``BOS``/``CHoCH`` per ST-06/ST-08, and a ``CHoCH`` is emitted
    unconfirmed until the first BOS in the new direction (MS-R25).  The family
    is the declared V07 fork; with none declared the ST-06 label only is
    produced, never an inferred CHoCH.  ``event`` keeps its historical
    ``BOS_UP``/``BOS_DOWN`` spelling so existing readers do not silently change
    meaning.
    """
    if mss_family is not None:
        structure_family_spec(mss_family)  # refuse an unknown V07 family
    if frame is None or len(frame) < 20:
        return {"bias": serialize_bias(BiasDirection.NEUTRAL), "event": "",
                "event_time": 0, "swing_high": 0.0, "swing_low": 0.0,
                "label": "", "confirmed": True, "confirmed_at": 0,
                "choch": None, "mss_family": mss_family}
    df = frame.tail(max(40, lookback)).reset_index(drop=True)
    highs = df["high"].astype(float).to_numpy()
    lows = df["low"].astype(float).to_numpy()
    closes = df["close"].astype(float).to_numpy()
    times = [int(pd.Timestamp(x).timestamp() * 1000) for x in df["date"]]
    n = len(df)
    piv_h: dict[int, float] = {}
    piv_l: dict[int, float] = {}
    for i in range(left, n - right):
        if highs[i] >= max(highs[i - left:i]) and highs[i] >= max(highs[i + 1:i + right + 1]):
            piv_h[i] = float(highs[i])
        if lows[i] <= min(lows[i - left:i]) and lows[i] <= min(lows[i + 1:i + right + 1]):
            piv_l[i] = float(lows[i])

    swing_h: tuple[int, float] | None = None
    swing_l: tuple[int, float] | None = None
    trend = 0
    last_event = ""
    last_time = 0
    #: ST-08 state machine: the latest CHoCH and whether a same-direction BOS
    #: confirmed it.  ``pending`` is the same object while unconfirmed.
    last_label = ""
    choch: dict[str, Any] | None = None
    pending: dict[str, Any] | None = None
    for i in range(left + 1, n):
        if i in piv_h:
            swing_h = (i, piv_h[i])
        if i in piv_l:
            swing_l = (i, piv_l[i])
        c = float(closes[i])
        if swing_h and i > swing_h[0] and c > swing_h[1]:
            last_label = structure_label(trend, "bull", family=mss_family)
            if last_label == CHoCH_LABEL:
                choch = {"index": i, "time": times[i], "confirmed": False,
                         "confirmed_at": None}
                pending = choch
            elif pending is not None:
                if last_label == "BOS":
                    pending["confirmed"] = True
                    pending["confirmed_at"] = i
                    pending["confirmed_time"] = times[i]
                pending = None
            trend = 1
            last_event = "BOS_UP"
            last_time = times[i]
            swing_h = None
        if swing_l and i > swing_l[0] and c < swing_l[1]:
            last_label = structure_label(trend, "bear", family=mss_family)
            if last_label == CHoCH_LABEL:
                choch = {"index": i, "time": times[i], "confirmed": False,
                         "confirmed_at": None}
                pending = choch
            elif pending is not None:
                if last_label == "BOS":
                    pending["confirmed"] = True
                    pending["confirmed_at"] = i
                    pending["confirmed_time"] = times[i]
                pending = None
            trend = -1
            last_event = "BOS_DOWN"
            last_time = times[i]
            swing_l = None
    if trend == 0 and n >= 20:
        if closes[-1] > closes[-20] and lows[-1] >= lows[-20]:
            trend, last_event = 1, "TREND_UP"
        elif closes[-1] < closes[-20] and highs[-1] <= highs[-20]:
            trend, last_event = -1, "TREND_DOWN"
    state = (BiasDirection.BULL if trend > 0
             else BiasDirection.BEAR if trend < 0 else BiasDirection.NEUTRAL)
    confirmed = True
    confirmed_at = 0
    confirmed_time = 0
    if last_label == CHoCH_LABEL and choch is not None:
        confirmed = bool(choch["confirmed"])
        confirmed_at = int(choch.get("confirmed_at") or 0)
        confirmed_time = int(choch.get("confirmed_time") or 0)
    return {
        "bias": serialize_bias(state),
        "event": last_event,
        "event_time": last_time,
        "swing_high": swing_h[1] if swing_h else 0.0,
        "swing_low": swing_l[1] if swing_l else 0.0,
        "label": last_label,
        "confirmed": confirmed,
        "confirmed_at": confirmed_at,
        "confirmed_time": confirmed_time,
        "choch": dict(choch) if choch is not None else None,
        "mss_family": mss_family,
    }
