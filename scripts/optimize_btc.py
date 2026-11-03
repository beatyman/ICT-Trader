#!/usr/bin/env python
"""Learn entry filters on BTCUSDT 15m and validate them out of sample.

The engine is not parameter-tuned blindly: one permissive pass produces the
whole signal pool with its features, every signal is simulated with the same
execution rules, and only then are filter rules measured - first on the TRAIN
slice, then once on the HOLDOUT slice.  A rule that only looks good in sample
is reported as overfit, never as a result.

  python scripts/optimize_btc.py --train-bars 45000 --test-bars 20000 \
      --json-out output/ict/opt_btc.json
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, replace
from itertools import product
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ict_trader.concepts import ICTConfig, analyze, load_csv  # noqa: E402
from ict_trader.setup import SetupConfig, build_setup  # noqa: E402
from ict_trader.strategy import StrategyConfig, simulate_cisd  # noqa: E402


def pool_setup() -> SetupConfig:
    """Exactly the population a user gets from the frozen setup.

    No gate is relaxed here.  An earlier version relaxed grade/confluence/
    sweep/HTF gates to get a bigger pool; the winning rule then could not be
    reproduced by any ``quality_profile`` (51 in-sample hits in the pool vs 21
    through the profile), which means the measured win rate belonged to the
    pool definition, not to the rule.  Candidates must be searched on the same
    population the profile will be applied to.
    """
    return SetupConfig(market="crypto", allow_short=True,
                       quality_profile="standard", rr=2.5)


def strategy(rr: float, entry_mode: str) -> StrategyConfig:
    """BTC perpetual: taker 0.05% + 1 bps slippage, no stamp duty."""
    return StrategyConfig(
        rr=rr, entry_mode=entry_mode, min_grade=0, min_confluence=0,
        allow_long=True, allow_short=True, commission_bps=5.0, slippage_bps=1.0,
        max_bars=48, min_rr=0.0, max_trades_per_day=6, max_concurrent=8,
    )


def pool_with_features(df: pd.DataFrame, cfg: SetupConfig) -> dict[int, dict]:
    res = analyze(df, ICTConfig(mode="historical", show_sessions=False,
                                show_htf_cisd=False, cisd_all=True, cisd_min_grade=0))
    signals = build_setup(df, res, cfg)
    atr = pd.Series(np.maximum.reduce([
        df["high"].to_numpy(float) - df["low"].to_numpy(float),
        np.abs(df["high"].to_numpy(float) - df["close"].shift(1).to_numpy(float)),
        np.abs(df["low"].to_numpy(float) - df["close"].shift(1).to_numpy(float)),
    ])).rolling(14, min_periods=1).mean().to_numpy()
    feats: dict[int, dict] = {}
    for c in signals:
        q = dict(c.quality_parts or {})
        hour = int(pd.Timestamp(df["date"].iloc[c.end]).hour)
        feats[int(c.end)] = {
            "direction": c.direction,
            "grade": int(c.grade),
            "confluence": int(sum(bool(v) for v in c.checks.values())),
            "hour": hour,
            "session": ("asia" if hour < 7 else "london" if hour < 12
                        else "ny" if hour < 21 else "late"),
            "atr_pct": float(atr[c.end] / df["close"].iloc[c.end]),
            "risk_atr": abs(float(c.level) - float(c.sl)) / max(float(atr[c.end]), 1e-12),
            **{k: q.get(k) for k in (
                "sweep_strength", "sweep_reclaim_bars", "sweep_penetration_atr",
                "disp_body_atr", "disp_body_ratio", "disp_body_multiple",
                "d1_score", "h4_score", "m15_score", "pd", "ote",
                "inducement_candidates", "fvg_fresh", "fvg_size_atr",
                "trend_strength", "atr_pct", "vol_rank", "regime_trend",
                "cisd_swept", "cisd_pda", "origin", "htf_pda", "pda_source",
            )},
        }
    return feats, res, signals


def simulate_rows(df, res, signals, rr: float, entry_mode: str) -> pd.DataFrame:
    bt = simulate_cisd(df, replace(res, cisds=signals), strategy(rr, entry_mode), "BTCUSDT")
    rows = []
    for t in bt.trades:
        rows.append({
            "signal_time": str(t.signal_time), "direction": t.direction,
            "r": float(t.r_multiple), "bars": int(t.bars), "reason": t.reason,
            "grade": int(t.grade), "confluence": int(t.confluence),
            "mfe_r": float(t.mfe_r), "mae_r": float(t.mae_r),
            "entry": float(t.entry), "sl": float(t.sl), "tp": float(t.tp),
            "rr_planned": abs(float(t.tp) - float(t.entry)) / max(abs(float(t.entry) - float(t.sl)), 1e-12),
        })
    return pd.DataFrame(rows)


def stats(r: np.ndarray) -> dict:
    if len(r) == 0:
        return {"n": 0, "win_rate": 0.0, "avg_r": 0.0, "total_r": 0.0, "profit_factor": 0.0}
    wins = r[r > 0]
    losses = r[r <= 0]
    return {
        "n": int(len(r)),
        "win_rate": round(float((r > 0).mean()), 4),
        "avg_r": round(float(r.mean()), 4),
        "total_r": round(float(r.sum()), 2),
        "profit_factor": round(float(wins.sum() / abs(losses.sum())), 3) if losses.sum() != 0 else None,
    }


def build_frame(df: pd.DataFrame, feats: dict[int, dict], trades: pd.DataFrame) -> pd.DataFrame:
    """Join trades to features on the bar index of the signal."""
    date_to_idx = {pd.Timestamp(d): i for i, d in enumerate(df["date"])}
    rows = []
    for _, t in trades.iterrows():
        ts = pd.Timestamp(t["signal_time"])
        pos = date_to_idx.get(ts)
        if pos is None or pos not in feats:
            continue
        rows.append({**t.to_dict(), **feats[pos]})
    return pd.DataFrame(rows)


def scan_feature(frame: pd.DataFrame, column: str, r_col: str = "r") -> pd.DataFrame:
    """Win rate and average R per feature bin (train slice only)."""
    if column not in frame:
        return pd.DataFrame()
    s = frame[column]
    if s.dtype.kind in "if" and s.notna().sum() > 6:
        try:
            bins = pd.qcut(s, 4, duplicates="drop")
        except ValueError:
            return pd.DataFrame()
    else:
        bins = s
    out = []
    for key, grp in frame.groupby(bins, observed=True):
        st = stats(grp[r_col].to_numpy(float))
        out.append({"feature": column, "bin": str(key), **st})
    return pd.DataFrame(out)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--csv", default="output/ict/btcusdt_15m_full.csv")
    ap.add_argument("--daily-csv", default="output/ict/btcusdt_1d_full.csv")
    ap.add_argument("--train-bars", type=int, default=45000)
    ap.add_argument("--test-bars", type=int, default=20000)
    ap.add_argument("--json-out", default="output/ict/opt_btc.json")
    args = ap.parse_args()

    full = load_csv(args.csv)
    daily = load_csv(args.daily_csv)
    n = len(full)
    split = n - args.test_bars
    train = full.iloc[max(0, split - args.train_bars):split].reset_index(drop=True)
    test = full.iloc[split:].reset_index(drop=True)

    report: dict = {"train": {"from": str(train["date"].iloc[0]), "to": str(train["date"].iloc[-1]),
                              "bars": len(train)},
                    "test": {"from": str(test["date"].iloc[0]), "to": str(test["date"].iloc[-1]),
                             "bars": len(test)},
                    "grid": []}

    for name, part in (("train", train), ("test", test)):
        cfg = pool_setup()
        feats, res, signals = pool_with_features(part, cfg)
        print(f"[{name}] pool signals {len(signals)}", flush=True)
        for rr, entry_mode in product((2.0, 2.5, 3.0), ("next_open", "fvg_ce", "retest_key")):
            trades = simulate_rows(part, res, signals, rr, entry_mode)
            frame = build_frame(part, feats, trades)
            report[f"{name}_jobs"] = report.get(f"{name}_jobs", [])
            st = stats(frame["r"].to_numpy(float)) if len(frame) else stats(np.array([]))
            row = {"rr": rr, "entry_mode": entry_mode, **st}
            print(f"  [{name}] rr={rr} {entry_mode:10s} {st}", flush=True)
            report["grid"].append({"slice": name, **row})
            if rr == 2.5 and entry_mode == "next_open":
                frame.to_csv(f"output/ict/opt_{name}_trades.csv", index=False)
                report[f"{name}_feature_scan"] = []
                for col in ("grade", "confluence", "session", "direction", "regime_trend",
                            "sweep_strength", "disp_body_atr", "d1_score", "h4_score",
                            "risk_atr", "atr_pct", "pd", "ote", "inducement_candidates",
                            "vol_rank", "trend_strength", "sweep_penetration_atr",
                            "sweep_reclaim_bars", "disp_body_multiple", "fvg_size_atr"):
                    sc = scan_feature(frame, col)
                    if len(sc):
                        report[f"{name}_feature_scan"] += sc.to_dict("records")

    Path(args.json_out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.json_out).write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(f"saved {args.json_out}")


if __name__ == "__main__":
    main()
