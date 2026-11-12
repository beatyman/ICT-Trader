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


_MSS_DIRECTIONS = {
    "long": "bull", "bull": "bull", "bullish": "bull",
    "short": "bear", "bear": "bear", "bearish": "bear",
}


def structure_evidence(events: list[Any], end_idx: int,
                       times: list[int], *,
                       direction: str | None = None,
                       after_index: int | None = None) -> dict[str, Any] | None:
    """Latest **MSS** at/before ``end_idx`` -- never a BOS.

    The §8.1 conjunction requires an MSS *after* the sweep, not "any structure
    event": a BOS extends the existing trend and cannot confirm the reversal
    the setup needs (ST-06/ST-07; ``structure_arrays.md`` P0-17).  When
    ``after_index`` is supplied (the sweep bar), only MSS events at or after it
    are eligible.  ``direction`` is the entry side (long/short) and filters the
    MSS to the same shift, so a counter-direction break cannot be attached to
    the setup.
    """
    wanted = None
    if direction is not None:
        wanted = _MSS_DIRECTIONS.get(str(direction).strip().lower())
        if wanted is None:
            raise ValueError(f"unknown direction: {direction!r}")
    best: tuple[int, Any] | None = None
    for e in events:
        if str(getattr(e, "kind", "")) != "MSS":
            continue
        try:
            idx = int(getattr(e, "index"))
        except (TypeError, ValueError):
            continue
        if idx > int(end_idx):
            continue
        if after_index is not None and idx < int(after_index):
            continue
        if wanted is not None and str(getattr(e, "direction", "")) != wanted:
            continue
        if best is None or idx > best[0]:
            best = (idx, e)
    if best is None:
        return None
    idx, e = best
    return {
        "event": str(e.kind),
        "direction": "bullish" if e.direction == "bull" else "bearish",
        "price": float(e.price),
        "time": int(times[min(idx, len(times) - 1)]) if times else 0,
    }


#: Fields ``setup.build_setup`` records for its measured displacement.  The V25
#: displacement detector is a P1-07 object and is not implemented yet, so these
#: measurements are the only displacement evidence the engine can consume.  When
#: they are absent the conjunct is *unknown*, not true (P0-17, DP-07).
DISPLACEMENT_KEYS = ("disp_body_ratio", "disp_body_atr",
                     "disp_body_multiple", "disp_volume_ratio")
#: At least one *body* measure is required; a volume ratio alone is not a
#: displacement reading.
_REQUIRED_DISPLACEMENT_KEYS = ("disp_body_ratio", "disp_body_atr",
                               "disp_body_multiple")


def displacement_evidence(quality_parts: Any) -> dict[str, float] | None:
    """Measured displacement attached to a setup, or ``None`` when absent.

    ``quality_parts`` comes from ``setup.build_setup``.  At least one body
    measure (body/range, body/ATR or body/average-body) is required: a volume
    ratio alone does not establish expansion.  Absence is a refusal, never an
    implicit pass -- ``ICTEngine`` records ``NT_NO_DISPLACEMENT`` and emits no
    signal (P0-17).
    """
    if quality_parts is None or not hasattr(quality_parts, "get"):
        return None
    if not any(key in quality_parts for key in _REQUIRED_DISPLACEMENT_KEYS):
        return None
    out: dict[str, float] = {}
    for key in DISPLACEMENT_KEYS:
        if key in quality_parts:
            try:
                out[key] = float(quality_parts[key])
            except (TypeError, ValueError):
                return None
    return out


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


#: AS-04 requires the IFVG side tag's mode to be **declared**, never inferred
#: from whichever implementation happens to run.  The repo declares
#: ``by_position``: the side is the dealing-range half containing the IFVG CE.
#: ``valid_ifvg_context`` encodes the same reading, so the emitted ``side`` and
#: the context verdict cannot disagree (P0-14, AS-01..AS-04).
IFVG_SIDE_MODE = "by_position"
IFVG_SIDE_MODES = ("by_type", "by_position", "by_role")

_POSITION_SIDES = {
    "premium": "premium", "deep_premium": "premium", "above_range": "premium",
    "discount": "discount", "deep_discount": "discount", "below_range": "discount",
}


def ifvg_side(direction: str, pd_zone: str, *,
              side_mode: str = IFVG_SIDE_MODE) -> str:
    """Return the IFVG side tag under the declared ``side_mode`` (AS-04).

    Only the declared mode is implemented: labelling a position reading as
    ``by_type``/``by_role`` would silently swap AS-01/AS-02 for AS-03 on the
    arrays where they disagree, so an unsupported mode is refused.
    """
    if side_mode not in IFVG_SIDE_MODES:
        raise ValueError(f"unknown IFVG side_mode: {side_mode!r}")
    if side_mode != "by_position":
        raise ValueError(f"undeclared IFVG side_mode: {side_mode!r}")
    if str(direction).strip().lower() not in ("bullish", "bearish"):
        return "unknown"
    return _POSITION_SIDES.get(str(pd_zone), "unknown")


def ifvg_evidence(ifvgs: list[dict], dealing_range: Any = None) -> dict | None:
    """Annotate the latest IFVG with its doc-502 context and CE entry price.

    The side tag carries the declared ``side_mode`` with it (AS-04), so a
    reader can tell a by-position label from the by-type/by-role alternatives
    that the corpus also attests (P0-14).
    """
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
        "side": ifvg_side(str(best.get("direction", "")), zone),
        "side_mode": IFVG_SIDE_MODE,
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


#: Repo pivot width used to build the HTF swings (``ICTConfig.swing_len``'s
#: default).  It is a repo value, not a corpus constant (P0-20 declares it).
_HTF_PIVOT_LEFT = 5


def htf_bias_evidence(frame: Any, end_idx: int) -> dict | None:
    """HH/HL bias for the 4H and 1D tiers, from **closed** buckets only.

    The bias primitive is :func:`ict_trader.context.structure_bias` (BI-01:
    HH/HL ⇒ bullish, LH/LL ⇒ bearish).  The realtime pivot/BOS read is a
    different, repo rule and is deliberately not used here (P0-09); it is
    named ``pivot_bos_bias`` in :mod:`.structure`.

    A left-labelled resample bucket is knowable only at its close, so the
    still-forming bucket is dropped before the swings are built: its partial
    OHLC must never reach the bias attached to a signal (P0-01, CB-01/CB-07).
    """
    import pandas as pd
    from ...concepts import find_pivots
    from ...context import structure_bias

    sub = frame.iloc[:end_idx + 1].copy()
    if len(sub) < 20:
        return None
    sub["date"] = pd.to_datetime(sub["date"], utc=True)
    last_base = pd.Timestamp(sub["date"].iloc[-1])
    sub = sub.set_index("date")
    out = {}
    for rule in ("4h", "1D"):
        x = sub.resample(rule).agg({
            "open": "first", "high": "max", "low": "min",
            "close": "last", "volume": "sum",
        }).dropna().reset_index()
        # Left-labelled buckets are knowable only at their close.  Drop the
        # still-forming bucket so its partial OHLC (and in particular its
        # close) never reaches the bias attached to a signal (P0-01).
        if len(x):
            bucket_close = (pd.Timestamp(x["date"].iloc[-1])
                            + pd.tseries.frequencies.to_offset(rule))
            if bucket_close > last_base:
                x = x.iloc[:-1].reset_index(drop=True)
        if len(x) < 20:
            out[rule] = None
            continue
        swings = find_pivots(x, _HTF_PIVOT_LEFT, 1)
        out[rule] = {"bias": structure_bias(swings, len(x) - 1)}
    return out
