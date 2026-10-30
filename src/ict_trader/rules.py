"""Executable checks for the layered ICT rule system.

The engine returns PASS / FAIL / N/A for every rule.  N/A is never treated as a
pass.  It does not invent structures or news; unavailable inputs stay N/A.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

import pandas as pd

from .concepts import ICTConfig, analyze
from .indicators import add_indicators, resample_htf


@dataclass
class RuleResult:
    rule_id: str
    layer: str
    name: str
    status: str
    blocking: bool
    value: Any = None
    reason: str = ""
    provisional: bool = False

    def as_dict(self) -> dict:
        return asdict(self)


def _atr(df: pd.DataFrame, n: int = 14) -> float:
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - df["close"].shift()).abs(),
        (df["low"] - df["close"].shift()).abs(),
    ], axis=1).max(axis=1)
    return float(tr.rolling(n, min_periods=1).mean().iloc[-1])


def _status(rule_id, layer, name, ok: bool | None, *, blocking=False,
            value=None, reason="", provisional=False) -> RuleResult:
    if ok is None:
        status = "N/A"
    else:
        status = "PASS" if ok else "FAIL"
    return RuleResult(rule_id, layer, name, status, blocking, value, reason, provisional)


_TF_DELTA = {"1h": pd.Timedelta(hours=1), "4h": pd.Timedelta(hours=4), "1d": pd.Timedelta(days=1)}


def _drop_incomplete_htf(df: pd.DataFrame, h: pd.DataFrame, tf: str) -> pd.DataFrame:
    """Drop the last HTF bar unless its period has fully closed."""
    if not len(h) or tf not in _TF_DELTA:
        return h
    if df["date"].iloc[-1] < h["date"].iloc[-1] + _TF_DELTA[tf]:
        return h.iloc[:-1]
    return h


def _cmp_state(a: float, b: float, tol: float) -> str:
    d = a - b
    if d > tol:
        return "bull"
    if d < -tol:
        return "bear"
    return "neutral"


def _score_row(r, atr: float, eq_atr: float = 0.05) -> dict:
    cloud_lo = min(float(r.span_a), float(r.span_b))
    cloud_hi = max(float(r.span_a), float(r.span_b))
    tol = max(eq_atr * atr, 1e-12)
    tk_state = _cmp_state(float(r.tenkan), float(r.kijun), tol)
    macd_state = _cmp_state(float(r.macd), float(r.macd_signal), tol)
    cloud_side = "bull" if r.close > cloud_hi else ("bear" if r.close < cloud_lo else "inside")
    return {
        "long": int(cloud_side == "bull") + int(tk_state == "bull") + int(macd_state == "bull"),
        "short": int(cloud_side == "bear") + int(tk_state == "bear") + int(macd_state == "bear"),
        "cloud_side": cloud_side,
        "tk_state": tk_state,
        "macd_state": macd_state,
        "eq_atr": eq_atr,
        "eq_abs": tol,
        "bar": str(r.date),
        "close": float(r.close),
        "cloud_lo": cloud_lo,
        "cloud_hi": cloud_hi,
        "tenkan": float(r.tenkan),
        "kijun": float(r.kijun),
        "macd": float(r.macd),
        "signal": float(r.macd_signal),
    }


def _htf_scores(df: pd.DataFrame, tf: str, eq_atr: float = 0.05) -> dict:
    h = resample_htf(df, tf)
    # If the caller already passed daily bars, the last daily bar is complete
    # relative to its own timestamp; do not drop it a second time.
    daily_input = (tf == "1d" and len(df) > 1
                   and (df["date"].iloc[-1] - df["date"].iloc[-2]) >= pd.Timedelta(hours=20))
    if not daily_input:
        h = _drop_incomplete_htf(df, h, tf)
    if len(h) < 2:
        return {"long": 0, "short": 0, "bar": None, "error": "insufficient_htf"}
    return _score_row(add_indicators(h).iloc[-1], _atr(h), eq_atr)



def structural_stop(direction: str, sweep_extreme: float, ob_edge: float,
                    atr: float, pad_atr: float = .10) -> float:
    if direction == "long":
        return min(float(sweep_extreme), float(ob_edge)) - pad_atr * atr
    return max(float(sweep_extreme), float(ob_edge)) + pad_atr * atr


def _plan_net_rr(plan: dict | None) -> tuple[float | None, float | None]:
    if not plan:
        return None, None
    entry, sl, tp = float(plan["entry"]), float(plan["sl"]), float(plan["tp"])
    direction = plan["direction"]
    risk = (entry - sl) if direction == "long" else (sl - entry)
    reward = (tp - entry) if direction == "long" else (entry - tp)
    if risk <= 0 or reward <= 0:
        return None, None
    cost = entry * 0.0007 + tp * 0.0007
    gross = reward / risk
    net = (reward - cost) / (risk + cost)
    return gross, net




def _delivery_state(direction: str, last_close: float, bottom: float, top: float,
                    htf_cisd: bool, htf_atr: float) -> bool:
    if direction == "long":
        return bool(htf_cisd and last_close >= top + .5 * htf_atr)
    return bool(htf_cisd and last_close <= bottom - .5 * htf_atr)


def _htf_pdas(df: pd.DataFrame, price: float, atr: float, touch_atr_source: str = "htf", touch_semantics: str = "current") -> list[dict]:
    """Return all active HTF PDAs with touch/delivery state."""
    out = []
    for rank, tf in enumerate(("1h", "4h")):
        h = _drop_incomplete_htf(df, resample_htf(df, tf), tf)
        if len(h) < 30:
            continue
        r = analyze(h, ICTConfig(mode="historical", show_sessions=False,
                                 show_htf_cisd=False, cisd_all=True, cisd_min_grade=0))
        h_atr = _atr(h)
        for kind, items in (("FVG", [x for x in r.fvgs if x.active]),
                            ("OB", [x for x in r.order_blocks if x.active])):
            for z in items:
                bottom, top = float(z.bottom), float(z.top)
                direction = "long" if z.direction == "bull" else "short"
                touch_atr = h_atr if touch_atr_source == "htf" else atr
                start_i = max(0, int(z.created))
                bars = h.iloc[start_i:]
                hist = bars[(bars["low"] <= top + .25 * touch_atr) &
                            (bars["high"] >= bottom - .25 * touch_atr)]
                current_touch = bottom - .25 * touch_atr <= price <= top + .25 * touch_atr
                last15 = df.iloc[-1]
                overlap_touch = bool(last15["low"] <= top + .25 * touch_atr and
                                     last15["high"] >= bottom - .25 * touch_atr)
                if touch_semantics == "historical":
                    touch = not hist.empty
                    touch_idx = int(hist.index[0]) if touch else None
                elif touch_semantics == "overlap":
                    touch = overlap_touch
                    touch_idx = int(h.index[-1]) if touch else None
                else:
                    touch = current_touch
                    touch_idx = int(h.index[-1]) if touch else None
                if not touch:
                    continue
                hist_idx = int(hist.index[0]) if not hist.empty else touch_idx
                cisds = [c for c in r.cisds if c.end >= hist_idx
                         and c.direction == z.direction]
                opposite = [c for c in r.cisds if c.end >= hist_idx
                            and c.direction != z.direction]
                htf_cisd = bool(cisds)
                reverse_cisd = bool(opposite) and not htf_cisd
                last_close = float(h["close"].iloc[-1])
                delivered = _delivery_state(direction, last_close, bottom, top, htf_cisd, h_atr)
                delivery_dist = ((last_close - top) / h_atr if direction == "long"
                                 else (bottom - last_close) / h_atr) if h_atr > 0 else 0.0
                dist = max(bottom - price, price - top, 0.0)
                out.append({
                        "tf": tf, "kind": kind, "bottom": bottom, "top": top,
                        "touch": touch, "delivered": delivered,
                        "delivery_distance_atr": round(float(delivery_dist), 3),
                        "touch_semantics": touch_semantics,
                        "current_touch": current_touch,
                        "historical_touch": not hist.empty,
                        "overlap_touch": overlap_touch,
                        "touch_bar": str(h["date"].iloc[touch_idx]),
                        "direction": direction, "htf_cisd": htf_cisd,
                        "reverse_cisd": reverse_cisd,
                        "last_bar": str(h["date"].iloc[-1]),
                        "touch_atr_source": touch_atr_source,
                        "cisd_bar": str(h["date"].iloc[cisds[-1].end]) if cisds else None,
                        "distance": dist, "tf_rank": rank,
                    })
    return out


def _htf_pda(df: pd.DataFrame, price: float, atr: float, touch_atr_source: str = "htf", touch_semantics: str = "current") -> dict | None:
    """Deprecated single-PDA view; prefer _htf_pdas + aggregation."""
    pds = _htf_pdas(df, price, atr, touch_atr_source, touch_semantics)
    if not pds:
        return None
    return min(pds, key=lambda x: (x["tf_rank"], x["distance"]))

def _smt_check(df: pd.DataFrame, smt_df: pd.DataFrame | None) -> tuple[bool | None, str]:
    if smt_df is None or "date" not in smt_df.columns:
        return None, "no second symbol"
    a = df[["date", "close"]].set_index("date")["close"]
    b = smt_df[["date", "close"]].set_index("date")["close"]
    joined = pd.concat([a.rename("a"), b.rename("b")], axis=1).dropna().tail(120)
    if len(joined) < 40:
        return None, "insufficient aligned bars"
    corr = float(joined["a"].pct_change().corr(joined["b"].pct_change()))
    if corr < .70:
        return False, f"correlation {corr:.2f} < 0.70"
    ra = analyze(df[df["date"].isin(joined.index)].reset_index(drop=True),
                 ICTConfig(mode="historical", show_sessions=False, show_htf_cisd=False,
                           cisd_all=True, cisd_min_grade=0))
    rb = analyze(smt_df[smt_df["date"].isin(joined.index)].reset_index(drop=True),
                 ICTConfig(mode="historical", show_sessions=False, show_htf_cisd=False,
                           cisd_all=True, cisd_min_grade=0))
    ah = [s for s in ra.swings if s.kind == "H"][-2:]
    bh = [s for s in rb.swings if s.kind == "H"][-2:]
    al = [s for s in ra.swings if s.kind == "L"][-2:]
    bl = [s for s in rb.swings if s.kind == "L"][-2:]
    if len(ah) == len(bh) == 2 and ah[-1].price > ah[-2].price and bh[-1].price <= bh[-2].price:
        return True, "bearish SMT: primary HH, secondary not HH"
    if len(al) == len(bl) == 2 and al[-1].price < al[-2].price and bl[-1].price >= bl[-2].price:
        return True, "bullish SMT: primary LL, secondary not LL"
    return False, "no aligned divergence"



def _displacement_quality(df: pd.DataFrame, i: int, atr: float,
                          swings, direction: str) -> dict:
    row = df.iloc[i]
    rng = float(row["high"] - row["low"])
    body = abs(float(row["close"] - row["open"]))
    body_ratio = body / rng if rng > 0 else 0.0
    range_atr = rng / atr if atr > 0 else 0.0
    if direction == "long":
        prior = [s for s in swings if s.kind == "H" and s.index < i]
        swing = prior[-1].price if prior else None
        beyond = (float(row["close"]) - swing) / atr if swing is not None else -1
    else:
        prior = [s for s in swings if s.kind == "L" and s.index < i]
        swing = prior[-1].price if prior else None
        beyond = (swing - float(row["close"])) / atr if swing is not None else -1
    follow = True
    if i + 1 < len(df):
        nxt = float(df.iloc[i + 1]["close"])
        follow = (nxt >= float(row["close"]) - .5 * body) if direction == "long" else (nxt <= float(row["close"]) + .5 * body)
    ok = body_ratio >= .60 and range_atr >= 1.0 and beyond >= .10 and follow
    return {"ok": bool(ok), "body_ratio": round(body_ratio, 3),
            "range_atr": round(range_atr, 3), "close_beyond_atr": round(float(beyond), 3),
            "follow_through": bool(follow), "swing": swing}



def _sequence_check(sweeps, events, cisds, entry_bar: int | None,
                    max_mss_gap: int = 12, max_cisd_gap: int = 6) -> dict:
    if not sweeps:
        return {"valid": False, "reason": "missing sweep",
                "sweep_bar": None, "mss_bar": None, "cisd_bar": None, "entry_bar": entry_bar}
    sweep = max(sweeps, key=lambda s: s.sweep_bar)
    if not events:
        return {"valid": False, "reason": "no MSS/CHoCH events",
                "sweep_bar": sweep.sweep_bar, "mss_bar": None, "cisd_bar": None, "entry_bar": entry_bar}
    msses = [e for e in events if e.kind in {"MSS", "CHoCH"} and e.index >= sweep.sweep_bar]
    if not msses:
        return {"valid": False, "reason": "no MSS after sweep",
                "sweep_bar": sweep.sweep_bar, "mss_bar": None, "cisd_bar": None, "entry_bar": entry_bar}
    mss = min(msses, key=lambda e: e.index)
    if mss.index - sweep.sweep_bar > max_mss_gap:
        return {"valid": False, "reason": "MSS too far from sweep",
                "sweep_bar": sweep.sweep_bar, "mss_bar": mss.index, "cisd_bar": None, "entry_bar": entry_bar}
    if not cisds:
        return {"valid": False, "reason": "no CISD",
                "sweep_bar": sweep.sweep_bar, "mss_bar": mss.index, "cisd_bar": None, "entry_bar": entry_bar}
    cds = [c for c in cisds if c.end >= mss.index]
    if not cds:
        return {"valid": False, "reason": "no CISD after MSS",
                "sweep_bar": sweep.sweep_bar, "mss_bar": mss.index, "cisd_bar": None, "entry_bar": entry_bar}
    c = min(cds, key=lambda x: x.end)
    if c.end - mss.index > max_cisd_gap:
        return {"valid": False, "reason": "CISD too far from MSS",
                "sweep_bar": sweep.sweep_bar, "mss_bar": mss.index, "cisd_bar": c.end, "entry_bar": entry_bar}
    return {"valid": True, "reason": "sweep -> MSS -> CISD",
            "sweep_bar": sweep.sweep_bar, "mss_bar": mss.index,
            "cisd_bar": c.end, "entry_bar": entry_bar}



def decide_from_good(good: list[dict], plan: dict | None) -> tuple[str, list[dict]]:
    hard = [g for g in good if g.get("necessary")]
    bad = [g for g in hard if g.get("status") != "PASS"]
    status = plan.get("plan_status", "conditional") if plan else None
    if bad or plan is None:
        return "NO_TRADE", bad
    if status == "triggered":
        return "TRADE", bad
    if status == "conditional":
        return "CONDITIONAL", bad
    return "NO_TRADE", bad


def audit(df: pd.DataFrame, daily: pd.DataFrame | None = None,
          market: str = "crypto", plan: dict | None = None,
          smt_df: pd.DataFrame | None = None, mode: str = "strict",
          touch_atr_source: str = "htf", touch_semantics: str = "current",
          htf_stub: dict | None = None) -> dict:
    """Run the L0-L5 checks and return a JSON-serialisable report."""
    if mode not in {"strict", "shadow"}:
        raise ValueError("mode must be strict or shadow")
    rules: list[RuleResult] = []
    n = len(df)
    last = df.iloc[-1]
    price = float(last["close"])
    res = analyze(
        df,
        ICTConfig(mode="historical", show_sessions=False, show_htf_cisd=False,
                  cisd_all=True, cisd_min_grade=0),
    )
    atr = _atr(df)
    trend = int(res.trend.iloc[-1]) if len(res.trend) else 0

    # ---- L0 ---------------------------------------------------------
    ohlc_ok = bool(
        ((df["high"] >= df[["open", "close", "low"]].max(axis=1)) &
         (df["low"] <= df[["open", "close", "high"]].min(axis=1))).all()
    )
    duplicate = bool(df["date"].duplicated().any())
    rules.append(_status("L0-01", "L0", "data integrity", ohlc_ok and not duplicate,
                         blocking=True, value={"ohlc": ohlc_ok, "duplicate": duplicate},
                         reason="OHLC invalid or duplicate timestamp" if not (ohlc_ok and not duplicate) else ""))

    session_ok = None
    if market == "crypto":
        session_ok = True
    elif market in {"us", "hk"}:
        h = last["date"].hour
        # 09:45..16:00 bars; the 16:00 bar is the 15:45-16:00 close bar.
        session_ok = 9 <= h <= 16
    rules.append(_status("L0-02", "L0", "market session", session_ok,
                         blocking=True, value=str(last["date"]),
                         reason="outside cash session" if session_ok is False else "session data unavailable" if session_ok is None else ""))
    rules.append(_status("L0-03", "L0", "news exclusion", None, blocking=False,
                         reason="no news calendar"))
    rules.append(_status("L0-04", "L0", "spread", None, blocking=False,
                         reason="no quote/spread data"))

    hist_ok = n >= 300
    if daily is not None:
        hist_ok = hist_ok and len(daily) >= 20
    rules.append(_status("L0-05", "L0", "history length", hist_ok,
                         blocking=True, value={"15m": n, "daily": 0 if daily is None else len(daily)}))

    # ---- L1 ---------------------------------------------------------
    swings = list(res.swings)
    rules.append(_status("L1-01", "L1", "swing definition", len(swings) >= 4,
                         blocking=True, value=len(swings),
                         reason="fewer than 4 confirmed swings" if len(swings) < 4 else ""))
    rules.append(_status("L1-02", "L1", "trend state", trend in (-1, 1),
                         blocking=False, value=trend,
                         reason="range" if trend == 0 else ""))
    recent_events = [e for e in res.events if e.index >= n - 50]
    recent_sweeps = [s for s in res.sweeps if s.sweep_bar >= n - 20]
    bos = [e for e in recent_events if e.kind == "BOS"]
    choch = [e for e in recent_events if e.kind in {"CHoCH", "MSS"}]
    rules.append(_status("L1-03", "L1", "BOS", bool(bos), value=len(bos)))
    entry_bar = None
    if plan and plan.get("plan_status", "conditional") == "triggered":
        if "trigger_bar" in plan:
            entry_bar = int(plan["trigger_bar"])
        elif "trigger_time" in plan:
            hits = df.index[df["date"] == pd.Timestamp(plan["trigger_time"])].tolist()
            entry_bar = int(hits[0]) if hits else None
    seq = _sequence_check(recent_sweeps, recent_events, res.cisds, entry_bar=entry_bar)
    rules.append(_status("L1-04", "L1", "CHoCH/MSS", seq["valid"], value=seq,
                         reason=seq["reason"] if not seq["valid"] else ""))
    rules.append(_status("L1-05", "L1", "range veto", trend != 0 if trend else None,
                         blocking=False, value="range" if trend == 0 else "trend"))

    # ---- L2 ---------------------------------------------------------
    daily_ok = daily is not None and len(daily) >= 2
    pdh = float(daily["high"].iloc[-2]) if daily_ok else None
    pdl = float(daily["low"].iloc[-2]) if daily_ok else None
    rules.append(_status("L2-01", "L2", "PDH/PDL", daily_ok,
                         value={"pdh": pdh, "pdl": pdl} if daily_ok else None,
                         reason="no daily data" if not daily_ok else ""))
    pools = [k for k in res.key_levels if k.name in {"EQH", "EQL"}]
    rules.append(_status("L2-02", "L2", "EQH/EQL", bool(pools), value=len(pools)))
    rules.append(_status("L2-03", "L2", "session range", None, blocking=False,
                         reason="no session range data"))
    rules.append(_status("L2-04", "L2", "liquidity sweep", bool(recent_sweeps),
                         blocking=True, value=len(recent_sweeps),
                         reason="no sweep within 20 bars" if not recent_sweeps else ""))
    rules.append(_status("L2-05", "L2", "sweep vs breakout", bool(recent_sweeps),
                         blocking=False, value="sweep" if recent_sweeps else "none"))
    if smt_df is None:
        rules.append(_status("L2-06", "L2", "SMT", None, blocking=False, reason="no second symbol"))
    else:
        rules.append(_status("L2-06", "L2", "SMT", None, blocking=False,
                             value="provided", reason="automated swing-divergence check not enabled"))

    visible = [k for k in res.key_levels
               if k.known_at <= n - 1
               and (k.sweep_bar is None or k.sweep_bar > n - 1)
               and (k.broken_at is None or k.broken_at > n - 1)]
    above = sorted([k for k in visible if k.price > price], key=lambda k: k.price)
    below = sorted([k for k in visible if k.price < price], key=lambda k: -k.price)
    if plan:
        # Do not use a pool created inside the last 20 bars as DOL: it is the
        # current swing extreme, not an untested draw.
        dol_side = [k for k in (above if plan["direction"] == "long" else below)
                    if k.known_at < n - 20]
        dol = dol_side[0] if dol_side else None
        rules.append(_status("L2-07", "L2", "DOL", dol is not None,
                             blocking=True,
                             value={"direction": plan["direction"],
                                    "dol": None if dol is None else round(dol.price, 4),
                                    "name": None if dol is None else dol.name},
                             reason="no unswept pool on plan side" if dol is None else ""))
    else:
        rules.append(_status("L2-07", "L2", "DOL", None, blocking=True,
                             value={"above": [round(k.price, 4) for k in above[:3]],
                                    "below": [round(k.price, 4) for k in below[:3]]},
                             reason="no plan direction"))

    pdas = _htf_pdas(df, price, atr, touch_atr_source, touch_semantics)
    delivered_pda = next((x for x in pdas if x["delivered"]), None)
    reverse_pda = next((x for x in pdas if x["reverse_cisd"] and not x["delivered"]), None)
    touch_pda = next((x for x in pdas if x["touch"]), None)
    pda = delivered_pda or reverse_pda or touch_pda
    if pda is not None:
        pda = dict(pda)
        pda["pdas"] = pdas
        pda["aggregate"] = ("delivered" if delivered_pda else
                            "reverse_cisd" if reverse_pda else "touch")
    rules.append(_status("L2-08", "L2", "HTF PDA delivery", pda is not None and not pda.get("reverse_cisd", False),
                         blocking=False, value=pda, provisional=False,
                         reason="price not in an active 1H/4H PDA" if pda is None else
                                ("reverse CISD" if pda.get("reverse_cisd") else
                                 ("delivered" if pda.get("delivered") else "touch only"))))
    rvol = float(df["volume"].iloc[-1] / df["volume"].tail(20).mean()) if "volume" in df else None
    rules.append(_status("L2-09", "L2", "volume / RVOL", None if rvol is None else rvol >= 1.5,
                         blocking=False, value=None if rvol is None else round(rvol, 3),
                         reason="no volume" if rvol is None else "RVOL below 1.5" if rvol < 1.5 else ""))
    smt_ok, smt_reason = _smt_check(df, smt_df)
    rules.append(_status("L2-10", "L2", "SMT divergence", smt_ok, blocking=False,
                         value=smt_reason, reason=smt_reason if smt_ok is not True else ""))

    # ---- L3 ---------------------------------------------------------
    # POI scope: only arrays relevant to the current entry (or current price).
    poi_ref = float(plan["entry"]) if plan else price
    poi_lo, poi_hi = poi_ref - .5 * atr, poi_ref + .5 * atr
    poi_time_lo = max(0, n - 60)
    fvgs = [f for f in res.fvgs if f.active and f.created >= poi_time_lo
            and float(f.top) >= poi_lo and float(f.bottom) <= poi_hi]
    obs = [o for o in res.order_blocks if o.active and o.created >= poi_time_lo
           and float(o.top) >= poi_lo and float(o.bottom) <= poi_hi]
    rules.append(_status("L3-01", "L3", "FVG", bool(fvgs), value={"count": len(fvgs), "entry_ref": poi_ref}))
    rules.append(_status("L3-02", "L3", "OB", bool(obs), value={"count": len(obs), "entry_ref": poi_ref}))
    rules.append(_status("L3-03", "L3", "Breaker", None, blocking=False,
                         reason="breaker classification not implemented"))
    cisds = [c for c in res.cisds if c.end >= n - 20]
    rules.append(_status("L3-04", "L3", "CISD", bool(cisds), blocking=True,
                         value=len(cisds), reason="no CISD within 20 bars" if not cisds else ""))
    sequence_ok = bool(seq.get("valid"))
    entry_ok = bool(sequence_ok and (fvgs or obs))
    rules.append(_status("L3-05", "L3", "entry model", entry_ok, blocking=True,
                         value={"sweep": bool(recent_sweeps), "cisd": bool(cisds),
                                "sequence": seq, "poi": bool(fvgs or obs)}))
    rules.append(_status("L3-06", "L3", "OTE", None, blocking=False,
                         reason="optional; no dealing range in this audit"))

    ote = None
    ote_reason = "no plan"
    if plan:
        highs = [s.price for s in res.swings if s.kind == "H"]
        lows = [s.price for s in res.swings if s.kind == "L"]
        if highs and lows:
            hi, lo = max(highs[-3:]), min(lows[-3:])
            if hi > lo:
                e = float(plan["entry"])
                ote = ((hi - e) / (hi - lo)) if plan["direction"] == "long" else ((e - lo) / (hi - lo))
                ote_reason = f"retracement {ote:.2f}"
    rules.append(_status("L3-07", "L3", "OTE 0.62-0.79",
                         None if ote is None else (0.62 <= ote <= 0.79),
                         blocking=False, value=None if ote is None else round(ote, 3),
                         reason=ote_reason if ote is None else ("outside OTE" if not (0.62 <= ote <= 0.79) else "")))
    rules.append(_status("L3-08", "L3", "Breaker", None, blocking=False,
                         reason="breaker classifier not implemented"))
    fresh = [f for f in fvgs if getattr(f, "fresh", False)]
    rules.append(_status("L3-09", "L3", "fresh FVG/CE", bool(fresh), blocking=False, value={"count": len(fresh), "entry_ref": poi_ref}))
    disp = None
    if cisds:
        last_c = max(cisds, key=lambda c: c.end)
        disp = _displacement_quality(df, last_c.end, atr, res.swings,
                                     "long" if last_c.direction == "bull" else "short")
    rules.append(_status("L3-10", "L3", "displacement quality", False if disp is None else disp["ok"],
                         blocking=False, value=disp,
                         reason="no CISD" if disp is None else ("" if disp["ok"] else "displacement below threshold")))

    # ---- L4 ---------------------------------------------------------
    gross, net = _plan_net_rr(plan)
    stop_value = None
    stop_ok = None
    stop_reason = "no plan"
    if plan:
        direction = plan["direction"]
        entry = float(plan["entry"])
        if direction == "short":
            levels = [k for k in res.key_levels if k.name == "BSL"
                      and k.sweep_bar is None and k.price > entry]
            level = min(levels, key=lambda k: k.price) if levels else None
            expected = float(level.price) + .1 * atr if level is not None else None
        else:
            levels = [k for k in res.key_levels if k.name == "SSL"
                      and k.sweep_bar is None and k.price < entry]
            level = max(levels, key=lambda k: k.price) if levels else None
            expected = float(level.price) - .1 * atr if level is not None else None
        if expected is not None:
            stop_ok = abs(float(plan["sl"]) - expected) <= .5 * atr
            stop_value = {"expected": round(expected, 4), "plan_sl": float(plan["sl"]),
                          "delta_atr": round(abs(float(plan["sl"]) - expected) / atr, 3),
                          "level": level.name, "level_price": float(level.price)}
            stop_reason = "" if stop_ok else "plan SL outside structural level"
        else:
            stop_reason = "no opposite-side BSL/SSL level"
    rules.append(_status("L4-01", "L4", "stop", stop_ok, blocking=False,
                         value=stop_value, reason=stop_reason))
    rules.append(_status("L4-02", "L4", "target", None if not plan else abs(plan["tp"] - plan["entry"]) > 0,
                         blocking=False, value=None if not plan else plan["tp"]))
    rules.append(_status("L4-03", "L4", "net RR", None if net is None else net >= 2.5,
                         blocking=True, value=None if net is None else round(net, 3),
                         reason="no plan" if plan is None else "below 2.5 net RR" if net < 2.5 else ""))
    rules.append(_status("L4-04", "L4", "time stop", None, blocking=False, reason="execution policy"))
    rules.append(_status("L4-05", "L4", "position sizing", None, blocking=False, reason="portfolio input required"))
    rules.append(_status("L4-06", "L4", "invalidation/overnight", None, blocking=False, reason="execution policy"))

    # ---- L5 ---------------------------------------------------------
    rules.append(_status("L5-01", "L5", "killzone", None, blocking=False,
                         reason="session/killzone input required"))
    rules.append(_status("L5-02", "L5", "news filter", None, blocking=False,
                         reason="no news calendar"))
    if daily is not None and len(daily) >= 2:
        daily_closed = daily
        if pd.Timestamp(daily["date"].iloc[-1]).date() >= pd.Timestamp(df["date"].iloc[-1]).date():
            daily_closed = daily.iloc[:-1]
        d_scores = _score_row(add_indicators(daily_closed).iloc[-1], _atr(daily_closed))
    else:
        d_scores = _htf_scores(df, "1d")
    h_scores = _htf_scores(df, "1h")
    f_scores = _htf_scores(df, "4h")
    if plan:
        direction = plan["direction"]
    elif trend > 0:
        direction = "long"
    elif trend < 0:
        direction = "short"
    else:
        direction = None
    if direction is None:
        htf_ok = None
    else:
        key = direction
        htf_ok = d_scores.get(key, 0) >= 2 and f_scores.get(key, 0) >= 2
    rules.append(_status("L5-03", "L5", "HTF alignment", htf_ok, blocking=True,
                         value={"direction": direction,
                                "1d": {"long": d_scores.get("long"), "short": d_scores.get("short"), "bar": d_scores.get("bar")},
                                "1h": {"long": h_scores.get("long"), "short": h_scores.get("short"), "bar": h_scores.get("bar")},
                                "4h": {"long": f_scores.get("long"), "short": f_scores.get("short"), "bar": f_scores.get("bar")}},
                         reason=("no direction" if direction is None else
                                 ("1D/4H not aligned" if not htf_ok else ""))))
    if htf_stub is not None and direction is not None:
        l5 = next((r for r in rules if r.rule_id == "L5-03"), None)
        if l5 is not None:
            score = int(htf_stub.get(direction, 0))
            l5.status = "PASS" if score >= 2 else "FAIL"
            l5.reason = "" if score >= 2 else "stub below 2/3"
            l5.value = {"direction": direction, "stub": True,
                        "long": int(htf_stub.get("long", 0)),
                        "short": int(htf_stub.get("short", 0))}
    rules.append(_status("L5-04", "L5", "session sweep", None, blocking=False,
                         reason="no session pool data"))
    rules.append(_status("L5-05", "L5", "SMT filter", None, blocking=False,
                         reason="no second symbol"))

    plan_info = None
    if plan:
        dist = abs(float(plan["entry"]) - price)
        dist_atr = dist / atr if atr > 0 else None
        dist_pct = dist / price if price else None
        plan_info = {
            "plan_status": plan.get("plan_status", "conditional"),
            "distance_to_entry_atr": None if dist_atr is None else round(dist_atr, 3),
            "distance_to_entry_pct": None if dist_pct is None else round(dist_pct * 100, 3),
            "fill_probability_flag": "low" if (dist_atr is not None and (dist_atr > 1.0 or dist_pct > .015)) else "normal",
        }
    # ---- GS chain ----------------------------------------------------
    by_id = {r.rule_id: r for r in rules}

    def gs(gid, name, sources, necessary, note="", any_of=False):
        objs = [by_id[s] for s in sources if s in by_id]
        statuses = [r.status for r in objs]
        provisional = any(r.status == "PASS" and r.provisional for r in objs)
        if not statuses:
            st = "N/A"
        elif any_of:
            st = "PASS" if "PASS" in statuses else ("FAIL" if all(x == "FAIL" for x in statuses) else "N/A")
        elif "FAIL" in statuses:
            st = "FAIL"
        elif all(x == "PASS" for x in statuses):
            st = "PASS"
        else:
            st = "N/A"
        if necessary and provisional and st == "PASS":
            st = "N/A"
        return {"id": gid, "name": name, "status": st, "necessary": necessary,
                "sources": sources, "note": note, "provisional": provisional}

    good = [
        gs("GS-01", "HTF alignment", ["L5-03"], True),
        gs("GS-02", "clear DOL", ["L2-07"], True),
        gs("GS-03", "liquidity sweep", ["L2-04"], True),
        gs("GS-04", "displacement/CISD", ["L3-04", "L3-10"], True),
        gs("GS-05", "structure confirmation", ["L1-04"], True),
        gs("GS-06", "FVG/OB/CE entry", ["L3-01", "L3-02"], True, "either POI", any_of=True),
        gs("GS-07", "structural stop", ["L4-01"], True),
        gs("GS-08", "net RR >= 2.5", ["L4-03"], True),
        gs("GS-09", "killzone/session", ["L5-01"], False),
        gs("GS-10", "HTF PDA delivery", ["L2-08"], False),
        gs("GS-11", "volume/RVOL", ["L2-09"], False),
        gs("GS-12", "SMT", ["L2-10"], False),
        gs("GS-13", "OTE", ["L3-07"], False),
        gs("GS-14", "Breaker", ["L3-08"], False),
        gs("GS-15", "fresh FVG/CE", ["L3-09"], False),
    ]
    if pda is not None:
        for g in good:
            if g["id"] == "GS-10":
                g["note"] = "reverse_cisd" if pda.get("reverse_cisd") else ("delivered" if pda.get("delivered") else "touch_only")
                g["touch"] = pda.get("touch")
                g["delivered"] = pda.get("delivered")
                g["htf_cisd"] = pda.get("htf_cisd")
                g["touch_semantics"] = pda.get("touch_semantics")
                g["cisd_bar"] = pda.get("cisd_bar")
                g["delivery_distance_atr"] = pda.get("delivery_distance_atr")
    req = [g for g in good if g["necessary"]]
    if any(g["status"] == "FAIL" for g in req):
        gs_decision = "FAIL"
    elif any(g["status"] == "N/A" for g in req):
        gs_decision = "INCOMPLETE"
    else:
        gs_decision = "PASS"
    quality_flags = [
        g["id"] for g in good
        if not g["necessary"] and (g["status"] != "PASS" or g.get("note") in {"touch_only", "reverse_cisd"})
    ]
    good_report = {"decision": gs_decision, "criteria": good,
                   "failed": [g["id"] for g in req if g["status"] == "FAIL"],
                   "incomplete": [g["id"] for g in req if g["status"] == "N/A"],
                   "quality_flags": quality_flags}
    req = [g for g in good if g["necessary"]]
    plan_status = plan.get("plan_status", "conditional") if plan else None
    strict_decision, hard_bad = decide_from_good(good, plan)
    blocker_details = [{"id": g["id"], "status": g["status"],
                        "type": "real_fail" if g["status"] == "FAIL" else "n_a"}
                       for g in hard_bad]

    if mode == "shadow":
        old_decision = "TRADE" if plan and plan_status in {"triggered", "conditional"} else "NO_TRADE"
        shadow = {
            "old_decision": old_decision,
            "new_decision": strict_decision,
            "diff": [g["id"] for g in hard_bad],
            "provisional": [r.rule_id for r in rules if r.provisional],
            "would_block": strict_decision != old_decision,
        }
        decision = strict_decision
    else:
        shadow = None
        decision = strict_decision
    return {
        "decision": decision,
        "strict_decision": strict_decision,
        "rule_mode": mode,
        "shadow": shadow,
        "good_setup": good_report,
        "plan": plan_info,
        "last_time": str(last["date"]),

        "last_close": price,
        "atr": round(atr, 6),
        "blocking_failures": [g["id"] for g in hard_bad],
        "blockers": blocker_details,
        "quality_flags": quality_flags,
        "htf_stub": htf_stub,
        "rules": [r.as_dict() for r in rules],
    }
