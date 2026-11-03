#!/usr/bin/env python
"""End-to-end stress test of the ICT engine on Binance BTCUSDT perpetual data.

Measurements only - it never trades and never edits the data.  Run it to get
the numbers behind an optimisation review:

  python scripts/e2e_btc_audit.py \
    --csv output/ict/btcusdt_15m_full.csv \
    --daily-csv output/ict/btcusdt_1d_full.csv \
    --json-out output/ict/e2e_btc_audit.json

Sections
  1. snapshot      last closed bar, ATR, HTF score, PO3 state on a true UTC day
  2. po3_year      per-UTC-day PO3 outcome: INVALID vs stuck MANIPULATED
  3. sweep_quality A/B of the sweep reclaim ratio against the forward outcome
  4. fvg_crowding  FVG count / width in ATR / overlap with the current setup
  5. scenarios     A>B>C>D evaluation: dual plans, stale sweeps, look-ahead
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from dataclasses import asdict
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ict_trader.concepts import ICTConfig, analyze, load_csv  # noqa: E402
from ict_trader.po3 import (ACCUMULATING, DISTRIBUTING, INVALID, MANIPULATED,  # noqa: E402
                            detect_po3)
from ict_trader.rules import audit  # noqa: E402
from ict_trader.scenarios import evaluate_scenarios  # noqa: E402


def atr_series(df: pd.DataFrame, n: int = 14) -> np.ndarray:
    h, l, c = df["high"], df["low"], df["close"]
    prev = c.shift(1)
    tr = pd.concat([h - l, (h - prev).abs(), (l - prev).abs()], axis=1).max(axis=1)
    return tr.rolling(n, min_periods=1).mean().to_numpy(float)


def true_day_slices(df: pd.DataFrame) -> list[pd.DataFrame]:
    """UTC 00:00 true days, the PO3 time basis used by the review."""
    day = pd.to_datetime(df["date"]).dt.floor("D")
    return [g.reset_index(drop=True) for _, g in df.groupby(day, sort=True)]


def section_snapshot(df, daily, market, acc_bars) -> dict:
    atr = atr_series(df)
    last_day = true_day_slices(df)[-1]
    po3, _ = detect_po3(last_day, accumulate_bars=min(acc_bars, len(last_day)),
                        index_offset=len(df) - len(last_day))
    report = audit(df, daily=daily, market=market)
    l5 = next((r for r in report["rules"] if r["rule_id"] == "L5-03"), None)
    return {
        "last_bar": str(df["date"].iloc[-1]),
        "last_close": float(df["close"].iloc[-1]),
        "atr15m": round(float(atr[-1]), 4),
        "atr_pct": round(float(atr[-1] / df["close"].iloc[-1]), 6),
        "bars": len(df),
        "from": str(df["date"].iloc[0]),
        "po3_true_day_bars": len(last_day),
        "po3_state": po3.state,
        "po3_sweep_direction": po3.sweep_direction,
        "po3_range": [po3.range_high, po3.range_low],
        "po3_manipulation_extreme": po3.manipulation_extreme,
        "po3_invalid_reason": po3.invalid_reason,
        "audit_decision": report["decision"],
        "blocking_failures": report["blocking_failures"],
        "htf_score_L5_03": None if l5 is None else {"status": l5["status"], "value": l5.get("value")},
    }


def section_po3_year(df: pd.DataFrame, acc_bars: int, mss_window: int = 20) -> dict:
    """Per-UTC-day PO3 outcome, plus how long MANIPULATED is allowed to last."""
    states: Counter = Counter()
    stuck_manipulated = 0
    distributing_no_mss = 0
    manipulated_bars: list[int] = []
    days = true_day_slices(df)
    for day in days:
        if len(day) < acc_bars + 2:
            continue
        st, phases = detect_po3(day, accumulate_bars=acc_bars)
        states[st.state] += 1
        if st.state == MANIPULATED:
            stuck_manipulated += 1
            manipulated_bars.append(len(day) - int(st.sweep_bar) - 1)
        if st.state == DISTRIBUTING:
            # the documented DISTRIBUTING -> INVALID transition needs an MSS
            # inside mss_window bars; detect_po3 never performs this check.
            if len(day) - int(st.distribution_bar) > mss_window:
                distributing_no_mss += 1
    n_days = sum(states.values())
    return {
        "days": n_days,
        "state_counts": dict(states),
        "pct_manipulated_at_day_end": round(stuck_manipulated / n_days, 4) if n_days else 0.0,
        "manipulated_bars_median": float(np.median(manipulated_bars)) if manipulated_bars else 0.0,
        "manipulated_bars_max": int(max(manipulated_bars)) if manipulated_bars else 0,
        "distributing_past_mss_window": distributing_no_mss,
    }


def section_sweep_quality(df: pd.DataFrame, result, bars: int = 40) -> dict:
    """Does a deeper reclaim of the sweep bar predict the reversal?

    For every sweep, the reclaim ratio of the penetration bar is
    (close-low)/(high-low) for a bullish sweep (mirror for bearish).  The
    forward outcome is measured from the reclaim close: +2R before the sweep
    extreme is violated, or the extreme first.
    """
    high = df["high"].to_numpy(float)
    low = df["low"].to_numpy(float)
    close = df["close"].to_numpy(float)
    n = len(df)
    buckets = {"reclaim<50": [0, 0, 0.0], "reclaim>=50": [0, 0, 0.0]}
    for s in result.sweeps:
        t = s.reclaim_bar
        if t + bars >= n or t < 1:
            continue
        rng = high[t] - low[t]
        if rng <= 0:
            continue
        ratio = (close[t] - low[t]) / rng if s.direction == "bull" else (high[t] - close[t]) / rng
        risk = abs(close[t] - s.sweep_price)
        if risk <= 0:
            continue
        target = close[t] + 2 * risk if s.direction == "bull" else close[t] - 2 * risk
        win = None
        for k in range(t + 1, min(n, t + bars + 1)):
            if s.direction == "bull":
                if low[k] <= s.sweep_price:
                    win = False
                    break
                if high[k] >= target:
                    win = True
                    break
            else:
                if high[k] >= s.sweep_price:
                    win = False
                    break
                if low[k] <= target:
                    win = True
                    break
        if win is None:
            continue
        key = "reclaim<50" if ratio < 0.5 else "reclaim>=50"
        buckets[key][0] += 1
        buckets[key][1] += int(win)
        buckets[key][2] += 2.0 if win else -1.0
    out = {}
    for k, (n_, w, r) in buckets.items():
        out[k] = {
            "n": n_,
            "win_rate": round(w / n_, 4) if n_ else 0.0,
            "avg_r_2R": round(r / n_, 4) if n_ else 0.0,
        }
    return out


def section_fvg_crowding(df: pd.DataFrame, result, window_bars: int, atr: np.ndarray) -> dict:
    n = len(df)
    start = max(0, n - window_bars)
    price = float(df["close"].iloc[-1])
    a = float(atr[-1])
    recent = [f for f in result.fvgs if f.created >= start]
    widths = np.array([abs(f.top - f.bottom) for f in recent], dtype=float)
    narrow = int((widths < 0.1 * a).sum()) if len(widths) else 0
    near = [f for f in recent if abs(f.ce - price) <= 0.5 * a]
    ceilings = [round(f.ce, 2) for f in near]
    dup = len(ceilings) - len(set(ceilings))
    return {
        "window_bars": window_bars,
        "fvg_total_engine": len(result.fvgs),
        "fvg_in_window": len(recent),
        "active_in_window": int(sum(1 for f in recent if f.active)),
        "narrower_than_0.1_atr": narrow,
        "near_setup_0.5_atr": len(near),
        "duplicate_ce_levels_near_setup": dup,
        "width_atr_p50": round(float(np.median(widths)) / a, 4) if len(widths) else 0.0,
        "width_atr_p90": round(float(np.percentile(widths, 90)) / a, 4) if len(widths) else 0.0,
    }


def section_scenarios(df: pd.DataFrame, market: str, window_bars: int,
                      acc_bars: int, stride: int, max_windows: int = 250,
                      po3_veto: bool = True, min_risk_atr: float = 0.5,
                      max_sweep_age: int | None = 15) -> dict:
    """Sample windows and evaluate A>B>C>D to find dual plans and stale sweeps."""
    n = len(df)
    cfg = ICTConfig(mode="historical", show_sessions=False, show_htf_cisd=False,
                    cisd_all=True, cisd_min_grade=0)
    rows = []
    dual = 0
    all_rejected = 0
    sweep_age: list[int] = []
    ends = [e for e in range(window_bars, n, stride)][-max_windows:]
    for end in ends:
        hist = df.iloc[: end + 1].tail(max(300, window_bars)).reset_index(drop=True)
        view = hist.tail(window_bars).reset_index(drop=True)
        res = analyze(hist, cfg)
        a = float(atr_series(view)[-1])
        price = float(view["close"].iloc[-1])
        # PO3 on the true UTC day containing the last bar, indexed against hist
        day_start = pd.Timestamp(view["date"].iloc[-1]).floor("D")
        day_view = view[pd.to_datetime(view["date"]) >= day_start].reset_index(drop=True)
        po3_src = day_view if len(day_view) >= acc_bars + 2 else view
        po3, _ = detect_po3(po3_src, accumulate_bars=min(acc_bars, len(po3_src)),
                            mss_events=[(int(e.index), e.direction) for e in res.events],
                            index_offset=len(hist) - len(po3_src))
        rep = audit(hist, daily=None, market=market)
        sid, plan = evaluate_scenarios(rep, po3, res, a, price, at=len(hist) - 1,
                                       po3_veto=po3_veto, min_risk_atr=min_risk_atr,
                                       max_sweep_age=max_sweep_age)
        if res.sweeps:
            sweep_age.append(len(hist) - 1 - res.sweeps[-1].reclaim_bar)
        if sid is None:
            rows.append({"end": str(hist["date"].iloc[-1]), "scenario": None,
                         "po3": po3.state, "status": None, "suppressed": []})
            continue
        if plan.suppressed:
            dual += 1
        if plan.status.startswith("REJECTED"):
            all_rejected += 1
        rows.append({
            "end": str(hist["date"].iloc[-1]),
            "scenario": sid,
            "direction": plan.direction,
            "status": plan.status,
            "net_rr": plan.net_rr,
            "po3": po3.state,
            "suppressed": plan.suppressed,
            "risk_atr": round(abs(plan.entry - plan.sl) / a, 3) if a > 0 else None,
        })
    produced = [r for r in rows if r["scenario"]]
    return {
        "windows": len(rows),
        "windows_with_plan": len(produced),
        "windows_with_suppressed_plans": dual,
        "windows_where_every_candidate_was_rejected": all_rejected,
        "scenario_counts": dict(Counter(r["scenario"] for r in produced)),
        "status_counts": dict(Counter(r["status"] for r in produced)),
        "risk_below_0_5_atr": sum(1 for r in produced
                                  if r.get("risk_atr") is not None and r["risk_atr"] < 0.5),
        "last_sweep_age_bars_median": float(np.median(sweep_age)) if sweep_age else 0.0,
        "samples": rows[-8:],
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--csv", default="output/ict/btcusdt_15m_full.csv")
    ap.add_argument("--daily-csv", default="output/ict/btcusdt_1d_full.csv")
    ap.add_argument("--market", default="crypto")
    ap.add_argument("--window-bars", type=int, default=288, help="3 days of 15m bars")
    ap.add_argument("--acc-bars", type=int, default=20, help="PO3 accumulation bars")
    ap.add_argument("--stride", type=int, default=96, help="scenario sampling stride")
    ap.add_argument("--max-windows", type=int, default=250)
    ap.add_argument("--min-risk-atr", type=float, default=0.5)
    ap.add_argument("--max-sweep-age", type=int, default=15)
    ap.add_argument("--no-po3-veto", action="store_true",
                    help="ablation: let scenarios run when PO3 is INVALID")
    ap.add_argument("--only", default="", help="comma list: snapshot,po3,sweep,fvg,scenarios")
    ap.add_argument("--json-out", default="output/ict/e2e_btc_audit.json")
    args = ap.parse_args()

    df = load_csv(args.csv)
    daily = load_csv(args.daily_csv) if args.daily_csv else None
    atr = atr_series(df)
    only = {s.strip().lower() for s in args.only.split(",") if s.strip()}

    def wanted(name: str) -> bool:
        return not only or name in only

    cfg = ICTConfig(mode="historical", show_sessions=False, show_htf_cisd=False,
                    cisd_all=True, cisd_min_grade=0)
    res = analyze(df, cfg)

    out: dict = {}
    if wanted("snapshot"):
        out["snapshot"] = section_snapshot(df, daily, args.market, args.acc_bars)
    if wanted("po3"):
        out["po3_year"] = section_po3_year(df, args.acc_bars)
    if wanted("sweep"):
        out["sweep_quality"] = section_sweep_quality(df, res)
    if wanted("fvg"):
        out["fvg_crowding"] = section_fvg_crowding(df, res, args.window_bars, atr)
    if wanted("scenarios"):
        out["scenarios"] = section_scenarios(df, args.market, args.window_bars,
                                             args.acc_bars, args.stride, args.max_windows,
                                             po3_veto=not args.no_po3_veto,
                                             min_risk_atr=args.min_risk_atr,
                                             max_sweep_age=args.max_sweep_age)
    path = Path(args.json_out)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    snap = out.get("snapshot") or {}
    if snap:
        print(f"last {snap['last_bar']} close {snap['last_close']:.2f} ATR {snap['atr15m']:.2f} "
              f"({snap['atr_pct'] * 100:.3f}%)")
        print(f"PO3(true day) {snap['po3_state']} | audit {snap['audit_decision']} "
              f"blocking {','.join(snap['blocking_failures']) or '-'}")
    if "po3_year" in out:
        p3 = out["po3_year"]
        print(f"PO3 {p3['days']} days: {p3['state_counts']} stuck-manipulated "
              f"{p3['pct_manipulated_at_day_end'] * 100:.1f}% "
              f"distributing-past-MSS-window {p3['distributing_past_mss_window']}")
    for k, v in (out.get("sweep_quality") or {}).items():
        print(f"sweep {k}: n={v['n']} win={v['win_rate'] * 100:.1f}% avgR={v['avg_r_2R']:+.3f}")
    if "fvg_crowding" in out:
        fv = out["fvg_crowding"]
        print(f"FVG 3d: {fv['fvg_in_window']} (active {fv['active_in_window']}) narrow<0.1ATR "
              f"{fv['narrower_than_0.1_atr']} near-setup {fv['near_setup_0.5_atr']} "
              f"dupCE {fv['duplicate_ce_levels_near_setup']}")
    if "scenarios" in out:
        sc = out["scenarios"]
        print(f"scenarios: {sc['windows']} windows, plans {sc['windows_with_plan']}, "
              f"dual {sc['windows_with_suppressed_plans']}, "
              f"all-rejected {sc['windows_where_every_candidate_was_rejected']}, "
              f"{sc['scenario_counts']} {sc['status_counts']}")
    print(f"saved {path}")


if __name__ == "__main__":
    main()
