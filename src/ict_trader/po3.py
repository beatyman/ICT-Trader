"""PO3 (Power of 3) state machine: Accumulation / Manipulation / Distribution.

A self-contained detector for the ICT "Power of 3" (AMD) cycle on one
time-basis slice of OHLCV bars, plus an injector that binds the PO3 state to
the existing GS/L rules.

State transitions
-----------------
  ACCUMULATING -> MANIPULATED   (wick through range boundary + close back)
  MANIPULATED  -> DISTRIBUTING  (close beyond opposite boundary + displacement)
  MANIPULATED  -> INVALID       (price keeps running past the manipulation
                                 extreme without reclaiming the range)
  DISTRIBUTING -> INVALID       (no same-direction MSS within 20 bars)
  DISTRIBUTING -> TRIGGERED     (CISD on the retrace into the range boundary)

The time basis is decided by the caller: pass the session/day slice as ``df``
and, optionally, the number of bars that define the accumulation range
(``accumulate_bars``).  For a US equity opening range use ~15 bars; for a
crypto Asian session use ~16-32 15m bars.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Iterable

import numpy as np
import pandas as pd

# states
ACCUMULATING = "ACCUMULATING"
MANIPULATED = "MANIPULATED"
DISTRIBUTING = "DISTRIBUTING"
INVALID = "INVALID"
TRIGGERED = "TRIGGERED"


@dataclass
class PO3State:
    state: str = ACCUMULATING
    sweep_direction: str | None = None        # "bull" | "bear" | None
    range_high: float | None = None
    range_low: float | None = None
    manipulation_extreme: float | None = None
    sweep_bar: int | None = None
    distribution_bar: int | None = None
    mss_confirmed: bool = False
    cisd_confirmed: bool = False
    invalid_reason: str | None = None
    plan: dict | None = None

    def as_dict(self) -> dict:
        return asdict(self)


def _body_ratio(open_: np.ndarray, close: np.ndarray, i: int, atr: float) -> float:
    rng = close[i] - open_[i]
    denom = atr if atr and atr > 0 else max(abs(close[i] - open_[i]), 1e-12)
    return abs(rng) / denom


def _atr_val(df: pd.DataFrame) -> float:
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - df["close"].shift()).abs(),
        (df["low"] - df["close"].shift()).abs(),
    ], axis=1).max(axis=1)
    v = tr.rolling(14, min_periods=1).mean().iloc[-1]
    return float(v) if pd.notna(v) else 0.0


def _detect_core(
    df: pd.DataFrame,
    accumulate_bars: int = 20,
    body_ratio_threshold: float = 0.6,
    mss_window: int = 20,
    atr: float | None = None,
    min_reclaim_ratio: float = 0.0,
    max_manipulation_bars: int = 0,
    mss_events: Iterable[tuple[int, str]] | None = None,
) -> tuple[PO3State, list[str]]:
    """Detect the PO3 cycle over ``df``.

    Returns ``(state, phases)`` where ``phases`` is a per-bar label list for
    plotting (ACCUMULATING / MANIPULATED / DISTRIBUTING / INVALID).

    ``min_reclaim_ratio`` filters noise spikes: a manipulation bar must close
    back inside the range by at least this fraction of its own high-low range
    (BTC 15m A/B: reclaim < 50% wins 28.7% at 2R, reclaim >= 50% wins 33.8%).
    ``max_manipulation_bars`` expires a MANIPULATED state that never
    distributes (0 disables).  ``mss_events`` is an iterable of
    ``(bar_index, direction)`` and enables the documented
    DISTRIBUTING -> INVALID transition; without it the state expires by time.
    """
    n = len(df)
    high = df["high"].to_numpy(float)
    low = df["low"].to_numpy(float)
    close = df["close"].to_numpy(float)
    open_ = df["open"].to_numpy(float)
    atr = atr if atr and atr > 0 else _atr_val(df)

    if n < 2:
        return PO3State(state=INVALID, invalid_reason="insufficient bars"), []

    acc_end = min(max(accumulate_bars, 1), n)
    range_high = float(np.max(high[:acc_end]))
    range_low = float(np.min(low[:acc_end]))

    st = PO3State(state=ACCUMULATING, range_high=range_high, range_low=range_low)
    phases = [ACCUMULATING] * acc_end + [ACCUMULATING] * (n - acc_end)

    # ---- ACCUMULATING -> MANIPULATED ---------------------------------
    i = acc_end
    while i < n and st.state == ACCUMULATING:
        rng = high[i] - low[i]
        reclaim = ((close[i] - low[i]) / rng) if rng > 0 else 0.0
        if low[i] < range_low and close[i] > range_low and reclaim >= min_reclaim_ratio:
            st.state = MANIPULATED
            st.sweep_direction = "bull"
            st.manipulation_extreme = float(np.min(low[i:i + 1]))
            st.sweep_bar = i
        elif high[i] > range_high and close[i] < range_high and (1.0 - reclaim) >= min_reclaim_ratio:
            st.state = MANIPULATED
            st.sweep_direction = "bear"
            st.manipulation_extreme = float(np.max(high[i:i + 1]))
            st.sweep_bar = i
        i += 1

    if st.state == ACCUMULATING:
        return st, phases

    m = st.sweep_bar
    # ---- MANIPULATED -> INVALID (breakaway) or DISTRIBUTING -----------
    while i < n and st.state == MANIPULATED:
        phases[i] = MANIPULATED
        if max_manipulation_bars > 0 and (i - m) > max_manipulation_bars:
            # A sweep that never distributes is not a PO3: without this the
            # state can sit in MANIPULATED for the rest of the session and any
            # later CISD is read as a distribution retest.
            st.state = INVALID
            st.invalid_reason = f"manipulation expired after {max_manipulation_bars} bars"
            phases[i:] = [INVALID] * (n - i)
            return st, phases
        if st.sweep_direction == "bull":
            # bull PO3: swept below, real move is up.  A close that keeps
            # running down through the manipulation extreme = real breakdown.
            if close[i] < st.manipulation_extreme:
                st.state = INVALID
                st.invalid_reason = "breakdown past manipulation low"
                break
            if close[i] > range_high and _body_ratio(open_, close, i, atr) > body_ratio_threshold:
                st.state = DISTRIBUTING
                st.distribution_bar = i
                phases[i] = DISTRIBUTING
                break
        else:
            # bear PO3: swept above, real move is down.
            if close[i] > st.manipulation_extreme:
                st.state = INVALID
                st.invalid_reason = "breakout past manipulation high"
                break
            if close[i] < range_low and _body_ratio(open_, close, i, atr) > body_ratio_threshold:
                st.state = DISTRIBUTING
                st.distribution_bar = i
                phases[i] = DISTRIBUTING
                break
        i += 1

    if st.state == INVALID:
        phases[i:] = [INVALID] * (n - i)
        return st, phases
    if st.state == MANIPULATED:
        return st, phases

    # ---- DISTRIBUTING: require MSS within mss_window ------------------
    d = st.distribution_bar
    # MSS check is done by the caller via inject (needs res.events); here we
    # only record the window for the injector.
    st._mss_deadline = d + mss_window  # type: ignore[attr-defined]
    phases[d:] = [DISTRIBUTING] * (n - d)
    # DISTRIBUTING -> INVALID when the promised MSS never arrives.  This
    # transition is in the module docstring; without it the state stayed
    # DISTRIBUTING for ever (BTC 15m: 103 of 114 distributing true days ended
    # past the MSS window).
    expected = "bull" if st.sweep_direction == "bull" else "bear"
    deadline = d + mss_window
    if n - 1 > deadline:
        if mss_events is not None:
            confirmed = any(
                direction == expected and d <= int(idx) <= deadline
                for idx, direction in mss_events
            )
            expired = not confirmed
            st.invalid_reason = None if confirmed else (
                f"no same-direction MSS within {mss_window} bars of distribution")
        else:
            expired = True
            st.invalid_reason = (
                f"distribution expired after {mss_window} bars (no MSS feed supplied)")
        if expired:
            st.state = INVALID
            phases[deadline + 1:] = [INVALID] * (n - deadline - 1)
    return st, phases


def detect_po3(
    df: pd.DataFrame,
    accumulate_bars: int = 20,
    body_ratio_threshold: float = 0.6,
    mss_window: int = 20,
    atr: float | None = None,
    min_reclaim_ratio: float = 0.0,
    max_manipulation_bars: int = 0,
    mss_events: Iterable[tuple[int, str]] | None = None,
    index_offset: int = 0,
) -> tuple[PO3State, list[str]]:
    """Detect the PO3 cycle over ``df``; see ``_detect_core`` for the rules.

    ``index_offset`` shifts ``sweep_bar`` / ``distribution_bar`` into the
    coordinates of the parent frame, so a state detected on a session (true
    day) slice can be matched against a result built on the full history.
    ``phases`` stays aligned to ``df`` itself.
    """
    st, phases = _detect_core(
        df, accumulate_bars=accumulate_bars,
        body_ratio_threshold=body_ratio_threshold, mss_window=mss_window, atr=atr,
        min_reclaim_ratio=min_reclaim_ratio,
        max_manipulation_bars=max_manipulation_bars, mss_events=mss_events,
    )
    if index_offset:
        for attr in ("sweep_bar", "distribution_bar"):
            value = getattr(st, attr, None)
            if value is not None:
                setattr(st, attr, int(value) + int(index_offset))
    return st, phases


def inject_po3_to_plan(
    po3: PO3State,
    plan: dict | None,
    *,
    sweep_hit: bool = False,
    mss_hit: bool = False,
    mss_direction: str | None = None,
    cisd_hit: bool = False,
    min_net_rr: float = 2.5,
) -> dict:
    """Bind the PO3 state to a GS/L plan and return the resolved plan.

    Returns a dict with ``status`` in {TRIGGERED, CONDITIONAL, REJECTED_*}.
    """
    if plan is None:
        return {"status": "REJECTED_NO_PLAN"}

    # 1. INVALID never trades.
    if po3.state == INVALID:
        return {**plan, "status": "REJECTED_PO3_INVALID", "po3": po3.as_dict(),
                "reason": po3.invalid_reason or "PO3 invalid"}

    # 2. ACCUMULATING: nothing to act on yet.
    if po3.state == ACCUMULATING:
        return {**plan, "status": "CONDITIONAL", "po3": po3.as_dict(),
                "reason": "PO3 accumulating"}

    # 3. MANIPULATED: the sweep must have been seen by L2-04 too.
    if po3.state == MANIPULATED:
        if not sweep_hit:
            return {**plan, "status": "REJECTED_PO3_FAKE_SWEEP", "po3": po3.as_dict(),
                    "reason": "PO3 manipulated but L2-04 sweep missed (noise spike)"}
        return {**plan, "status": "CONDITIONAL", "po3": po3.as_dict(),
                "reason": "PO3 manipulated, awaiting distribution"}

    # 4. DISTRIBUTING: require a same-direction MSS after distribution.
    if po3.state == DISTRIBUTING:
        expected = "bull" if po3.sweep_direction == "bull" else "bear"
        if not mss_hit:
            return {**plan, "status": "REJECTED_PO3_NO_MSS", "po3": po3.as_dict(),
                    "reason": "PO3 distributing but no MSS within window"}
        if mss_direction is not None and mss_direction != expected:
            return {**plan, "status": "REJECTED_PO3_MSS_DIR", "po3": po3.as_dict(),
                    "reason": f"MSS direction {mss_direction} != PO3 {expected}"}

        # 5. TRIGGERED only on CISD at the range-boundary retrace.
        status = TRIGGERED if cisd_hit else "CONDITIONAL"
        # 6. low RR veto.
        net_rr = plan.get("net_rr")
        if net_rr is not None and net_rr < min_net_rr:
            status = "REJECTED_PO3_LOW_RR"
        return {**plan, "status": status, "po3": po3.as_dict(),
                "reason": "" if status == TRIGGERED else "awaiting CISD retrace"}

    return {**plan, "status": "CONDITIONAL", "po3": po3.as_dict()}


def po3_plan(po3: PO3State, atr: float, range_stdv: float | None = None) -> dict | None:
    """Build a PO3 trade plan from the state (entry / SL / TP)."""
    if po3.state != DISTRIBUTING or po3.sweep_direction is None:
        return None
    if po3.range_high is None or po3.range_low is None or po3.manipulation_extreme is None:
        return None
    rng = abs(po3.range_high - po3.range_low)
    # TP = 2 x range, or the opposite boundary (whichever is reached first in
    # risk terms).  SL = manipulation extreme +/- 0.1 ATR.
    if po3.sweep_direction == "bull":
        entry = po3.range_high
        sl = po3.manipulation_extreme - 0.1 * atr
        tp = entry + 2 * rng
        direction = "long"
    else:
        entry = po3.range_low
        sl = po3.manipulation_extreme + 0.1 * atr
        tp = entry - 2 * rng
        direction = "short"
    risk = abs(entry - sl)
    reward = abs(tp - entry)
    cost = entry * 0.0007 + tp * 0.0007
    gross = reward / risk if risk > 0 else None
    net = (reward - cost) / (risk + cost) if risk > 0 else None
    return {
        "direction": direction, "entry": round(entry, 4), "sl": round(sl, 4),
        "tp": round(tp, 4), "gross_rr": None if gross is None else round(gross, 3),
        "net_rr": None if net is None else round(net, 3),
        "range": round(rng, 4),
        "po3": po3.as_dict(),
    }
