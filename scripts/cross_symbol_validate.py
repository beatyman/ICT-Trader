#!/usr/bin/env python
"""Validate the BTC-derived rules on symbols that were never used to build them.

The BTC study (docs/BTC_15M_OPTIMIZATION.md) ended with every candidate rule
dead out of sample.  This script asks the only question left: do the rules that
looked good on BTC work on eight other USDT perpetuals at all?  Nothing is
re-tuned here - the thresholds are copied from the BTC run as constants.

  python scripts/cross_symbol_validate.py --dir output/ict/gate \
      --timeframes 1h 15m --json-out output/ict/cross_symbol.json
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ict_trader.concepts import ICTConfig, analyze  # noqa: E402
from ict_trader.concepts import load_csv
from ict_trader.setup import SetupConfig, build_setup  # noqa: E402
from ict_trader.stats import mean_interval, wilson_interval  # noqa: E402
from ict_trader.strategy import StrategyConfig, simulate_cisd  # noqa: E402

SYMBOLS = ["BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "ADA", "AVAX", "LINK"]
COST = dict(commission_bps=10.0, slippage_bps=2.0)   # taker 0.05%/side + 1bps/side
RR = 2.5

# Populations to compare.  ``regime_trend_gate="aligned"`` requires the 20-bar
# EMA slope (in ATR) to agree with the trade direction, i.e. it turns the setup
# into a trend-continuation filter; the R2 gates are the ones that printed 57%
# pooled and 20% before 2026-07-24.
VARIANTS: dict[str, dict] = {
    "V0_frozen": {},
    "V1_trend_aligned": {"regime_trend_gate": "aligned"},
    "V2_gates": {"min_sweep_strength": 3.0, "min_stop_atr": 2.0, "min_grade": 4},
    "V3_gates_trend": {"min_sweep_strength": 3.0, "min_stop_atr": 2.0,
                       "min_grade": 4, "regime_trend_gate": "aligned"},
}
SPLIT = pd.Timestamp("2026-07-24")   # start of the only positive window so far

# Every rule is a predicate over signal-time features only.
RULES: dict[str, str] = {
    "R0_frozen": "r == r",   # no filter (query-able identity, keeps every trade)
    "R1_disp145_stop14": "disp_body_atr >= 1.45 and risk_atr >= 1.40",
    "R2_sweep3_stop20_grade4": "sweep_strength >= 3 and risk_atr > 2.0 and grade >= 4",
    "R3_pen03_stop14_grade4": "sweep_penetration_atr >= 0.3 and risk_atr > 1.4 and grade >= 4",
    "R4_sweep3": "sweep_strength >= 3",
}


def trade_frame(df: pd.DataFrame, symbol: str, res=None, variant: str = "V0_frozen",
                variant_cfg: dict | None = None) -> pd.DataFrame:
    if res is None:
        res = analyze(df, ICTConfig(mode="historical", show_sessions=False,
                                    show_htf_cisd=False, cisd_all=True, cisd_min_grade=0))
    signals = build_setup(df, res, SetupConfig(market="crypto", allow_short=True,
                                               quality_profile="standard", rr=RR,
                                               **(variant_cfg or {})))
    if not signals:
        return pd.DataFrame()
    atr = pd.Series(np.maximum.reduce([
        df["high"].to_numpy(float) - df["low"].to_numpy(float),
        np.abs(df["high"].to_numpy(float) - df["close"].shift(1).to_numpy(float)),
        np.abs(df["low"].to_numpy(float) - df["close"].shift(1).to_numpy(float)),
    ])).rolling(14, min_periods=1).mean().to_numpy()
    strat = StrategyConfig(rr=RR, min_grade=0, min_confluence=0, allow_short=True,
                           max_bars=48, min_rr=0.0, max_trades_per_day=6,
                           max_concurrent=8, **COST)
    bt = simulate_cisd(df, replace(res, cisds=signals), strat, symbol)
    by_end = {int(c.end): c for c in signals}
    pos = {pd.Timestamp(d): i for i, d in enumerate(df["date"])}
    rows = []
    for t in bt.trades:
        i = pos.get(pd.Timestamp(t.signal_time))
        c = by_end.get(i) if i is not None else None
        if c is None:
            continue
        q = dict(c.quality_parts or {})
        hour = int(pd.Timestamp(t.signal_time).hour)
        rows.append({
            "variant": variant, "symbol": symbol, "r": float(t.r_multiple),
            "direction": t.direction,
            "signal_date": pd.Timestamp(t.signal_time),
            "grade": int(c.grade), "confluence": int(sum(bool(v) for v in c.checks.values())),
            "hour": hour,
            "risk_atr": abs(float(c.level) - float(c.sl)) / max(float(atr[i]), 1e-12),
            "disp_body_atr": float(q.get("disp_body_atr") or 0.0),
            "sweep_strength": float(q.get("sweep_strength") or 0.0),
            "sweep_penetration_atr": float(q.get("sweep_penetration_atr") or 0.0),
            "disp_body_multiple": float(q.get("disp_body_multiple") or 0.0),
        })
    return pd.DataFrame(rows)


def summarise(r: np.ndarray) -> dict:
    n = len(r)
    if n == 0:
        return {"n": 0, "win_rate": 0.0, "win_ci95": [0.0, 1.0], "avg_r": 0.0,
                "avg_r_ci95": [0.0, 0.0], "total_r": 0.0, "profit_factor": None}
    wins = int((r > 0).sum())
    pos, neg = r[r > 0].sum(), abs(r[r <= 0].sum())
    return {
        "n": n,
        "win_rate": round(wins / n, 4),
        "win_ci95": [round(x, 4) for x in wilson_interval(wins, n)],
        "avg_r": round(float(r.mean()), 4),
        "avg_r_ci95": [round(x, 4) for x in mean_interval([float(x) for x in r])],
        "total_r": round(float(r.sum()), 2),
        "profit_factor": round(float(pos / neg), 3) if neg > 0 else None,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dir", default="output/ict/gate")
    ap.add_argument("--symbols", nargs="+", default=SYMBOLS)
    ap.add_argument("--timeframes", nargs="+", default=["1h", "15m"])
    ap.add_argument("--json-out", default="output/ict/cross_symbol.json")
    args = ap.parse_args()

    frames = []
    meta = {}
    for tf in args.timeframes:
        for sym in args.symbols:
            path = Path(args.dir) / f"{sym.lower()}_{tf}.csv"
            if not path.exists():
                print(f"missing {path}", file=sys.stderr)
                continue
            df = load_csv(path)
            # One analysis pass, every variant built on top of it.
            res = analyze(df, ICTConfig(mode="historical", show_sessions=False,
                                        show_htf_cisd=False, cisd_all=True, cisd_min_grade=0))
            counts = {}
            for name, cfg in VARIANTS.items():
                fr = trade_frame(df, sym, res=res, variant=name, variant_cfg=cfg)
                counts[name] = len(fr)
                if len(fr):
                    frames.append(fr.assign(tf=tf))
            meta[f"{sym}_{tf}"] = {"bars": len(df), "from": str(df["date"].iloc[0]),
                                   "to": str(df["date"].iloc[-1]), "trades": counts}
            print(f"{sym:5} {tf:3} bars={len(df):6d} " +
                  " ".join(f"{k.split('_')[0]}={v:3d}" for k, v in counts.items()), flush=True)

    if not frames:
        raise SystemExit("no trades collected")
    pool = pd.concat(frames, ignore_index=True)
    pool.to_csv("output/ict/cross_symbol_trades.csv", index=False)

    out = {"data": meta, "rules": {}, "per_symbol": {}}
    # Variant comparison, split at the only positive window found so far.
    print(f"\n{'variant':18} {'period':22} {'n':>4} {'win':>6} {'win95':>16} "
          f"{'avgR':>7} {'avgR95':>18} {'PF':>6}")
    for name in VARIANTS:
        sub = pool[pool["variant"] == name]
        for label, sel in (("ALL", sub),
                           ("before 2026-07-24", sub[sub["signal_date"] < SPLIT]),
                           ("from 2026-07-24", sub[sub["signal_date"] >= SPLIT])):
            s = summarise(sel["r"].to_numpy(float))
            out["rules"][f"{name}|{label}"] = s
            print(f"{name:18} {label:22} {s['n']:>4} {s['win_rate']:>6.3f} "
                  f"{str(s['win_ci95']):>16} {s['avg_r']:>+7.3f} "
                  f"{str(s['avg_r_ci95']):>18} {str(s['profit_factor']):>6}")

    print(f"\n{'rule':26} {'tf':4} {'n':>4} {'win':>6} {'win95':>16} {'avgR':>7} {'avgR95':>18} {'PF':>6}")
    for name, expr in RULES.items():
        for tf in ["ALL"] + args.timeframes:
            sub = pool[pool["variant"] == "V0_frozen"]
            if tf != "ALL":
                sub = sub[sub["tf"] == tf]
            sel = sub.query(expr) if len(sub) else sub
            s = summarise(sel["r"].to_numpy(float)) if len(sel) else summarise(np.array([]))
            out["rules"][f"{name}|{tf}"] = s
            print(f"{name:26} {tf:4} {s['n']:>4} {s['win_rate']:>6.3f} "
                  f"{str(s['win_ci95']):>16} {s['avg_r']:>+7.3f} {str(s['avg_r_ci95']):>18} "
                  f"{str(s['profit_factor']):>6}")
        # per-symbol breakdown for the pooled population of this rule
        for sym in args.symbols:
            sel = pool[pool["symbol"] == sym].query(expr)
            if len(sel):
                out["per_symbol"][f"{name}|{sym}"] = summarise(sel["r"].to_numpy(float))

    Path(args.json_out).write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"\nsaved {args.json_out}  (pooled trades: {len(pool)})")


if __name__ == "__main__":
    main()
