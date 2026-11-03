#!/usr/bin/env python
"""Entry vs exit: where do the R come from, and where are they given back?

The engine journals ``mfe_r`` / ``mae_r`` on every trade but never uses them.
This script turns them into a diagnosis:

  * MFE capture  - of all favourable excursion that was offered, how much did
    the exit rules actually keep?
  * give-back    - how many losers were once 1R / 1.5R / 2R in profit?
  * exit reasons - how much of the book is closed by the time stop, not by a
    decision?

then sweeps the exit knobs that already exist (time stop, breakeven, ATR trail)
on the nine-symbol pool.  Entries are held fixed: the same signals are replayed
under every exit rule, so any difference is the exit only.

  python scripts/exit_study.py --timeframes 1h --json-out output/ict/exit_study.json
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ict_trader.concepts import ICTConfig, analyze, load_csv  # noqa: E402
from ict_trader.setup import SetupConfig, build_setup  # noqa: E402
from ict_trader.stats import mean_interval, wilson_interval  # noqa: E402
from ict_trader.strategy import StrategyConfig, simulate_cisd  # noqa: E402

SYMBOLS = ["BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "ADA", "AVAX", "LINK"]
COST = dict(commission_bps=10.0, slippage_bps=2.0)
RR = 2.5

ENTRY_VARIANTS = {
    "frozen": {},
    "trend_aligned": {"regime_trend_gate": "aligned"},
}

# Exit rules.  Everything here already exists in StrategyConfig; nothing new.
EXITS: dict[str, dict] = {
    "base_b48": {},                                   # rr 2.5, time stop 48 bars
    "b24": {"max_bars": 24},
    "b96": {"max_bars": 96},
    "b200": {"max_bars": 200},
    "b500": {"max_bars": 500},
    "be_1R": {"breakeven_at_r": 1.0},
    "be_1.5R": {"breakeven_at_r": 1.5},
    "be_1R_b200": {"breakeven_at_r": 1.0, "max_bars": 200},
    "trail_1R_1ATR": {"trail_activate_r": 1.0, "trail_atr_mult": 1.0},
    "trail_1.5R_2ATR": {"trail_activate_r": 1.5, "trail_atr_mult": 2.0},
    "trail_2R_1ATR": {"trail_activate_r": 2.0, "trail_atr_mult": 1.0},
    "trail_1R_1ATR_b200": {"trail_activate_r": 1.0, "trail_atr_mult": 1.0, "max_bars": 200},
    # partial scale-out (new): bank half at 1R, let the rest run to the 2.5R
    # target with a break-even stop.  This is the only shape that can hold both
    # a >50% win rate and a >2R winner.
    "t1half_1R_be_b200": {"t1_at_r": 1.0, "t1_fraction": 0.5, "t1_move_be": True,
                          "max_bars": 200},
    "t1half_1R_be": {"t1_at_r": 1.0, "t1_fraction": 0.5, "t1_move_be": True},
    "t1third_1R_be_b200": {"t1_at_r": 1.0, "t1_fraction": 0.33, "t1_move_be": True,
                           "max_bars": 200},
    "t1half_1.5R_be_b200": {"t1_at_r": 1.5, "t1_fraction": 0.5, "t1_move_be": True,
                            "max_bars": 200},
    "t1half_1R_trail_b200": {"t1_at_r": 1.0, "t1_fraction": 0.5, "t1_move_be": True,
                             "trail_activate_r": 1.5, "trail_atr_mult": 1.0,
                             "max_bars": 200},
}


def run(df: pd.DataFrame, res, signals, exit_cfg: dict, symbol: str,
        rr: float = RR) -> pd.DataFrame:
    base = dict(rr=rr, min_grade=0, min_confluence=0, allow_short=True,
                max_bars=48, min_rr=0.0, max_trades_per_day=6, max_concurrent=8,
                **COST)
    base.update(exit_cfg)
    strat = StrategyConfig(**base)
    bt = simulate_cisd(df, replace(res, cisds=signals), strat, symbol)
    return pd.DataFrame([{
        "symbol": symbol, "r": float(t.r_multiple), "reason": t.reason,
        "bars": int(t.bars), "mfe_r": float(t.mfe_r), "mae_r": float(t.mae_r),
        "direction": t.direction, "signal_date": pd.Timestamp(t.signal_time),
    } for t in bt.trades])


def summarise(r: np.ndarray) -> dict:
    n = len(r)
    if n == 0:
        return {"n": 0, "win_rate": 0.0, "win_ci95": [0.0, 1.0], "avg_r": 0.0,
                "avg_r_ci95": [0.0, 0.0], "total_r": 0.0, "profit_factor": None}
    wins = int((r > 0).sum())
    pos, neg = r[r > 0].sum(), abs(r[r <= 0].sum())
    return {
        "n": n, "win_rate": round(wins / n, 4),
        "win_ci95": [round(x, 4) for x in wilson_interval(wins, n)],
        "avg_r": round(float(r.mean()), 4),
        "avg_r_ci95": [round(x, 4) for x in mean_interval([float(x) for x in r])],
        "total_r": round(float(r.sum()), 2),
        "profit_factor": round(float(pos / neg), 3) if neg > 0 else None,
    }


def diagnose(trades: pd.DataFrame) -> dict:
    """Where did the R go?  Uses only journaled excursions."""
    if trades.empty:
        return {}
    offered = float(trades["mfe_r"].clip(lower=0).sum())
    kept = float(trades["r"].clip(lower=0).sum())
    win_r = trades.loc[trades["r"] > 0, "r"]
    loss_r = trades.loc[trades["r"] <= 0, "r"]
    mfe = trades["mfe_r"].to_numpy(float)
    n = len(trades)
    return {
        # What the market actually offered.  If only X% of trades ever reach a
        # level, a target above it is not a target, it is a lottery ticket.
        "mfe_mean_r": round(float(mfe.mean()), 3),
        "mfe_p50_r": round(float(np.median(mfe)), 3),
        "pct_reach_1R": round(float((mfe >= 1.0).mean()), 3),
        "pct_reach_1.5R": round(float((mfe >= 1.5).mean()), 3),
        "pct_reach_2R": round(float((mfe >= 2.0).mean()), 3),
        "pct_reach_2.5R": round(float((mfe >= 2.5).mean()), 3),
        "pct_reach_3R": round(float((mfe >= 3.0).mean()), 3),
        "avg_win_r": round(float(win_r.mean()), 3) if len(win_r) else None,
        "avg_loss_r": round(float(loss_r.mean()), 3) if len(loss_r) else None,
        # the RR actually realised, not the RR planned: this is the number the
        # "win > 50% and RR > 2" goal is about.
        "realised_rr": (round(float(win_r.mean() / abs(loss_r.mean())), 3)
                        if len(win_r) and len(loss_r) and loss_r.mean() != 0 else None),
        "exit_reasons": dict(Counter(trades["reason"])),
        "mfe_offered_r": round(offered, 2),
        "profit_kept_r": round(kept, 2),
        "mfe_capture": round(kept / offered, 3) if offered > 0 else None,
        "losers": int((trades["r"] <= 0).sum()),
        "losers_that_were_up_1R": int(((trades["r"] <= 0) & (trades["mfe_r"] >= 1.0)).sum()),
        "losers_that_were_up_1.5R": int(((trades["r"] <= 0) & (trades["mfe_r"] >= 1.5)).sum()),
        "losers_that_were_up_2R": int(((trades["r"] <= 0) & (trades["mfe_r"] >= 2.0)).sum()),
        "avg_bars": round(float(trades["bars"].mean()), 1),
        "time_exits": int((trades["reason"] == "time").sum()),
        "time_exits_still_1R_up": int(((trades["reason"] == "time") & (trades["r"] > 0.5)).sum()),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dir", default="output/ict/gate")
    ap.add_argument("--symbols", nargs="+", default=SYMBOLS)
    ap.add_argument("--timeframes", nargs="+", default=["1h"])
    ap.add_argument("--json-out", default="output/ict/exit_study.json")
    ap.add_argument("--rr-sweep", action="store_true",
                    help="also sweep the fixed target (the MFE table says where "
                         "the optimal target sits)")
    args = ap.parse_args()

    # dataset -> (df, res, {variant: signals})
    cache = []
    for tf in args.timeframes:
        for sym in args.symbols:
            path = Path(args.dir) / f"{sym.lower()}_{tf}.csv"
            if not path.exists():
                print(f"missing {path}", file=sys.stderr)
                continue
            df = load_csv(path)
            res = analyze(df, ICTConfig(mode="historical", show_sessions=False,
                                        show_htf_cisd=False, cisd_all=True,
                                        cisd_min_grade=0))
            sigs = {}
            for vname, vcfg in ENTRY_VARIANTS.items():
                sigs[vname] = build_setup(df, res, SetupConfig(
                    market="crypto", allow_short=True, rr=RR, **vcfg))
            cache.append((tf, sym, df, res, sigs))
            print(f"{sym:5} {tf:3} " + " ".join(f"{k}={len(v):3d}"
                                                for k, v in sigs.items()), flush=True)

    out: dict = {"diagnosis": {}, "exits": {}, "datasets": len(cache)}
    for vname in ENTRY_VARIANTS:
        print(f"\n=== entry variant: {vname}")
        base_trades = None
        for ename, ecfg in EXITS.items():
            frames = [run(df, res, sigs[vname], ecfg, sym)
                      for tf, sym, df, res, sigs in cache if sigs[vname]]
            frames = [f for f in frames if len(f)]
            if not frames:
                continue
            pool = pd.concat(frames, ignore_index=True)
            s = summarise(pool["r"].to_numpy(float))
            d = diagnose(pool)
            out["exits"][f"{vname}|{ename}"] = {**s, **ecfg}
            out.setdefault("diagnosis_all", {})[f"{vname}|{ename}"] = d
            if ename == "base_b48":
                base_trades = pool
                out["diagnosis"][vname] = d
            print(f"{ename:20} n={s['n']:3d} win={s['win_rate']:.3f} "
                  f"avgR={s['avg_r']:+.3f} totR={s['total_r']:+7.1f} PF={str(s['profit_factor']):>5} "
                  f"capture={d['mfe_capture']} time_exits={d['time_exits']:2d}")
        if base_trades is not None:
            d = out["diagnosis"][vname]
            print(f"  baseline diagnosis: exit reasons {d['exit_reasons']}")
            print(f"  MFE offered {d['mfe_offered_r']}R, kept {d['profit_kept_r']}R "
                  f"({d['mfe_capture']:.0%} capture)")
            print(f"  losers {d['losers']} of which were up >=1R: {d['losers_that_were_up_1R']}, "
                  f">=1.5R: {d['losers_that_were_up_1.5R']}, >=2R: {d['losers_that_were_up_2R']}")

    if args.rr_sweep:
        print("\n=== target sweep (same entries, RR varied, 500-bar hold)")
        out["rr_sweep"] = {}
        for rr in (0.75, 1.0, 1.25, 1.5, 2.0, 2.5, 3.0, 4.0):
            for vname, vcfg in ENTRY_VARIANTS.items():
                frames = []
                for tf, sym, df, res, _ in cache:
                    sigs = build_setup(df, res, SetupConfig(
                        market="crypto", allow_short=True, rr=rr, **vcfg))
                    f = run(df, res, sigs, {"max_bars": 500}, sym, rr=rr)
                    if len(f):
                        frames.append(f)
                if not frames:
                    continue
                pool = pd.concat(frames, ignore_index=True)
                s = summarise(pool["r"].to_numpy(float))
                d = diagnose(pool)
                out["rr_sweep"][f"{vname}|rr{rr}"] = {**s, "realised_rr": d["realised_rr"],
                                                      "mfe_pct_2.5R": d["pct_reach_2.5R"]}
                print(f"{vname:14} rr={rr:<5} n={s['n']:3d} win={s['win_rate']:.3f} "
                      f"avgR={s['avg_r']:+.3f} PF={str(s['profit_factor']):>5} "
                      f"realRR={d['realised_rr']} (avg win {d['avg_win_r']} / "
                      f"loss {d['avg_loss_r']})")

    Path(args.json_out).write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    print(f"\nsaved {args.json_out}")


if __name__ == "__main__":
    main()
