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
            value=None, reason="") -> RuleResult:
    if ok is None:
        status = "N/A"
    else:
        status = "PASS" if ok else "FAIL"
    return RuleResult(rule_id, layer, name, status, blocking, value, reason)


def _htf_score(df: pd.DataFrame, tf: str) -> tuple[int, dict]:
    h = resample_htf(df, tf)
    if len(h) and h["date"].iloc[-1] > df["date"].iloc[-1]:
        h = h.iloc[:-1]
    h = add_indicators(h)
    r = h.iloc[-1]
    cloud_lo = min(float(r.span_a), float(r.span_b))
    cloud_hi = max(float(r.span_a), float(r.span_b))
    checks = {
        "above_cloud": bool(r.close > cloud_hi) if r.close > cloud_hi else
                       (bool(r.close < cloud_lo) if r.close < cloud_lo else False),
        "cloud_side": "bull" if r.close > cloud_hi else ("bear" if r.close < cloud_lo else "inside"),
        "tk": bool(r.tenkan > r.kijun),
        "macd": bool(r.macd > r.macd_signal),
    }
    score = int(checks["cloud_side"] != "inside") + int(checks["tk"]) + int(checks["macd"])
    return score, checks


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


def audit(df: pd.DataFrame, daily: pd.DataFrame | None = None,
          market: str = "crypto", plan: dict | None = None,
          smt_df: pd.DataFrame | None = None) -> dict:
    """Run the L0-L5 checks and return a JSON-serialisable report."""
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
    bos = [e for e in recent_events if e.kind == "BOS"]
    choch = [e for e in recent_events if e.kind in {"CHoCH", "MSS"}]
    rules.append(_status("L1-03", "L1", "BOS", bool(bos), value=len(bos)))
    rules.append(_status("L1-04", "L1", "CHoCH/MSS", bool(choch), value=len(choch)))
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
    recent_sweeps = [s for s in res.sweeps if s.sweep_bar >= n - 20]
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
    rules.append(_status("L2-07", "L2", "DOL", bool(above or below),
                         blocking=True, value={"above": [round(k.price, 4) for k in above[:3]],
                                               "below": [round(k.price, 4) for k in below[:3]]},
                         reason="no unswept opposite pool" if not (above or below) else ""))

    # ---- L3 ---------------------------------------------------------
    fvgs = [f for f in res.fvgs if f.active]
    obs = [o for o in res.order_blocks if o.active]
    rules.append(_status("L3-01", "L3", "FVG", bool(fvgs), value=len(fvgs)))
    rules.append(_status("L3-02", "L3", "OB", bool(obs), value=len(obs)))
    rules.append(_status("L3-03", "L3", "Breaker", None, blocking=False,
                         reason="breaker classification not implemented"))
    cisds = [c for c in res.cisds if c.end >= n - 20]
    rules.append(_status("L3-04", "L3", "CISD", bool(cisds), blocking=True,
                         value=len(cisds), reason="no CISD within 20 bars" if not cisds else ""))
    sequence_ok = False
    if recent_sweeps and cisds:
        last_sweep = max(recent_sweeps, key=lambda s: s.sweep_bar)
        last_cisd = max(cisds, key=lambda c: c.end)
        sequence_ok = last_cisd.end > last_sweep.sweep_bar
    entry_ok = bool(sequence_ok and (fvgs or obs))
    rules.append(_status("L3-05", "L3", "entry model", entry_ok, blocking=True,
                         value={"sweep": bool(recent_sweeps), "cisd": bool(cisds),
                                "sequence": sequence_ok, "poi": bool(fvgs or obs)}))
    rules.append(_status("L3-06", "L3", "OTE", None, blocking=False,
                         reason="optional; no dealing range in this audit"))

    # ---- L4 ---------------------------------------------------------
    gross, net = _plan_net_rr(plan)
    rules.append(_status("L4-01", "L4", "stop", None if not plan else abs(plan["entry"] - plan["sl"]) >= .3 * atr,
                         blocking=False, value=None if not plan else abs(plan["entry"] - plan["sl"])))
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
    d_score, d_checks = (_htf_score(df, "1d") if daily is None else
                         (lambda d: (sum([d.close > max(d.span_a, d.span_b),
                                          d.tenkan > d.kijun, d.macd > d.macd_signal]), {}))(add_indicators(daily).iloc[-1]))
    h_score, h_checks = _htf_score(df, "1h")
    f_score, f_checks = _htf_score(df, "4h")
    htf_ok = d_score >= 2 and f_score >= 2
    rules.append(_status("L5-03", "L5", "HTF alignment", htf_ok, blocking=True,
                         value={"1d": d_score, "1h": h_score, "4h": f_score},
                         reason="1D/4H not aligned" if not htf_ok else ""))
    rules.append(_status("L5-04", "L5", "session sweep", None, blocking=False,
                         reason="no session pool data"))
    rules.append(_status("L5-05", "L5", "SMT filter", None, blocking=False,
                         reason="no second symbol"))

    blocking_fail = [r for r in rules if r.blocking and r.status != "PASS"]
    decision = "TRADE" if plan and not blocking_fail else "NO_TRADE"

    # ---- GS chain ----------------------------------------------------
    by_id = {r.rule_id: r for r in rules}

    def gs(gid, name, sources, necessary, note=""):
        statuses = [by_id[s].status for s in sources if s in by_id]
        if not statuses:
            st = "N/A"
        elif "FAIL" in statuses:
            st = "FAIL"
        elif all(x == "PASS" for x in statuses):
            st = "PASS"
        else:
            st = "N/A"
        return {"id": gid, "name": name, "status": st, "necessary": necessary,
                "sources": sources, "note": note}

    good = [
        gs("GS-01", "HTF alignment", ["L5-03"], True),
        gs("GS-02", "clear DOL", ["L2-07"], True),
        gs("GS-03", "liquidity sweep", ["L2-04"], True),
        gs("GS-04", "displacement/CISD", ["L3-04"], True),
        gs("GS-05", "structure confirmation", ["L1-04"], True),
        gs("GS-06", "FVG/OB/CE entry", ["L3-01", "L3-02"], True, "either POI"),
        gs("GS-07", "structural stop", ["L4-01"], True),
        gs("GS-08", "net RR >= 2.5", ["L4-03"], True),
        gs("GS-09", "killzone/session", ["L5-01"], False),
        gs("GS-10", "HTF PDA delivery", [], False, "not automated"),
        gs("GS-11", "volume/RVOL", [], False, "not automated"),
        gs("GS-12", "SMT", ["L2-06"], False),
        gs("GS-13", "OTE", ["L3-06"], False),
        gs("GS-14", "Breaker", ["L3-03"], False),
        gs("GS-15", "fresh FVG/CE", ["L3-01"], False, "fresh flag not scored"),
    ]
    req = [g for g in good if g["necessary"]]
    if any(g["status"] == "FAIL" for g in req):
        gs_decision = "FAIL"
    elif any(g["status"] == "N/A" for g in req):
        gs_decision = "INCOMPLETE"
    else:
        gs_decision = "PASS"
    good_report = {"decision": gs_decision, "criteria": good,
                   "failed": [g["id"] for g in req if g["status"] == "FAIL"],
                   "incomplete": [g["id"] for g in req if g["status"] == "N/A"]}
    return {
        "decision": decision,
        "good_setup": good_report,
        "last_time": str(last["date"]),

        "last_close": price,
        "atr": round(atr, 6),
        "blocking_failures": [r.rule_id for r in blocking_fail],
        "rules": [r.as_dict() for r in rules],
    }
