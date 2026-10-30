#!/usr/bin/env python
"""Minimal trend-following scanner (MVP).

This is intentionally small: it scans active FVG/OB zones, waits for a
same-direction CISD after the first touch, then emits a candidate.  It does
not claim to be a full ICT sequence engine.
"""
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, asdict
from pathlib import Path

import pandas as pd

from ict_trader.concepts import ICTConfig, analyze, load_csv
from ict_trader.rules import _atr, _htf_scores, audit


@dataclass
class Candidate:
    bar: int
    time: str
    direction: str
    poi_kind: str
    poi_bottom: float
    poi_top: float
    touch_bar: int
    cisd_bar: int
    entry: float
    sl: float
    tp: float
    rr: float
    htf_1d: dict
    htf_4h: dict
    gs: dict


def scan_trend_following(df: pd.DataFrame, daily: pd.DataFrame,
                         max_cisd_gap: int = 20) -> list[Candidate]:
    if len(df) < 100 or daily is None or len(daily) < 30:
        return []
    res = analyze(df, ICTConfig(mode="historical", show_sessions=False,
                                show_htf_cisd=False, cisd_all=True,
                                cisd_min_grade=0))
    atr = _atr(df)
    d1 = _htf_scores(df, "1d") if daily is None else _htf_scores(daily, "1d")
    h4 = _htf_scores(df, "4h")
    out: list[Candidate] = []
    for kind, zones in (("FVG", [z for z in res.fvgs if z.active]),
                        ("OB", [z for z in res.order_blocks if z.active])):
        for z in zones:
            direction = "long" if z.direction == "bull" else "short"
            if d1.get(direction, 0) < 2 or h4.get(direction, 0) < 2:
                continue
            start = max(0, int(z.created))
            bars = df.iloc[start:]
            touched = bars[(bars["low"] <= float(z.top) + .5 * atr) &
                           (bars["high"] >= float(z.bottom) - .5 * atr)]
            if touched.empty:
                continue
            touch_bar = int(touched.index[0])
            cisds = [c for c in res.cisds
                     if c.end >= touch_bar and c.end - touch_bar <= max_cisd_gap
                     and c.direction == z.direction]
            if not cisds:
                continue
            c = min(cisds, key=lambda x: x.end)
            entry = float(df["close"].iloc[c.end])
            risk = max(abs(entry - float(z.bottom if direction == "long" else z.top)), .1 * atr)
            sl = float(z.bottom - .1 * atr) if direction == "long" else float(z.top + .1 * atr)
            tp = entry + 3 * risk if direction == "long" else entry - 3 * risk
            rr = abs(tp - entry) / max(abs(entry - sl), 1e-9)
            out.append(Candidate(
                bar=int(c.end), time=str(df["date"].iloc[c.end]), direction=direction,
                poi_kind=kind, poi_bottom=float(z.bottom), poi_top=float(z.top),
                touch_bar=touch_bar, cisd_bar=int(c.end), entry=entry, sl=sl, tp=tp,
                rr=round(rr, 3),
                htf_1d={k: d1.get(k) for k in ("long", "short", "bar")},
                htf_4h={k: h4.get(k) for k in ("long", "short", "bar")},
                gs={},
            ))
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", required=True)
    ap.add_argument("--daily-csv", required=True)
    ap.add_argument("--tail", type=int, default=5000)
    ap.add_argument("--json-out", default="output/ict/trend_scan.json")
    args = ap.parse_args()
    df = load_csv(args.csv).tail(args.tail).reset_index(drop=True)
    daily = load_csv(args.daily_csv)
    cands = scan_trend_following(df, daily)
    for c in cands:
        a = audit(df, daily=daily, market="crypto",
                  plan={"direction": c.direction, "entry": c.entry,
                        "sl": c.sl, "tp": c.tp, "plan_status": "triggered"})
        c.gs = {"decision": a["decision"], "blockers": a["blocking_failures"]}
    Path(args.json_out).write_text(
        json.dumps([asdict(c) for c in cands], ensure_ascii=False, indent=2, default=str),
        encoding="utf-8")
    print(f"candidates: {len(cands)}")
    for c in cands:
        print(c.time, c.direction, c.poi_kind, c.cisd_bar, c.rr, c.gs)


if __name__ == "__main__":
    main()
