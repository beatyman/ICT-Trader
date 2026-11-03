#!/usr/bin/env python
"""Search entry rules on the TRAIN slice, then score the survivors OOS.

Reads two trade frames (same columns) produced by ``scripts/optimize_btc.py``
and searches conjunctions of at most three threshold conditions.  Every rule
keeps its numeric thresholds fixed when applied to the holdout - nothing is
re-quantiled there, which is the only way an out-of-sample number means
anything.  The output states how many rules were tried, because a 3-condition
search over ~30 candidates is a large multiple-testing bill.

  python scripts/rule_search.py --train output/ict/opt_train_trades.csv \
      --test output/ict/opt_test_trades.csv --json-out output/ict/rules.json
"""
from __future__ import annotations

import argparse
import json
from itertools import combinations
from pathlib import Path

import numpy as np
import pandas as pd

# (column, operator, threshold) - thresholds come from the train quantiles,
# never from the holdout.
CANDIDATES = [
    ("disp_body_atr", ">", 1.45),
    ("disp_body_atr", ">", 2.0),
    ("disp_body_atr", ">", 2.5),
    ("sweep_strength", ">=", 3.0),
    ("sweep_strength", ">=", 4.0),
    ("sweep_penetration_atr", ">=", 0.3),
    ("risk_atr", ">", 1.4),
    ("risk_atr", ">", 2.0),
    ("risk_atr", "<=", 3.0),
    ("atr_pct", ">", 0.003),
    ("atr_pct", "<", 0.002),
    ("grade", ">=", 4),
    ("confluence", ">=", 3),
    ("vol_rank", ">=", 0.75),
    ("trend_strength", ">", 0.3),
    ("trend_strength", "<", 0.0),
    ("inducement_candidates", "<=", 0),
    ("disp_body_multiple", ">", 1.5),
]
# Anything computed from the trade's own future is look-ahead and must never
# enter the search: an early version of this file used ``mfe_r > 1`` and
# "found" a 94% win rate on the holdout, because MFE is only known after exit.
POST_TRADE = {"r", "mfe_r", "mae_r", "bars", "reason", "rr_planned",
              "time_to_mae", "time_to_mfe", "exit", "exit_time"}
CATEGORICAL = [
    ("session", ["asia", "london", "ny", "late"]),
    ("regime_trend", ["up", "down", "flat"]),
    ("direction", ["bull", "bear"]),
    ("origin", ["sweep", "pda", "both", "none"]),
]


def mask(df: pd.DataFrame, rule: list[tuple]) -> pd.Series:
    m = pd.Series(True, index=df.index)
    for col, op, val in rule:
        if op == "in":
            m &= df[col].isin(val)
        elif op == "==":
            m &= df[col] == val
        elif op == ">":
            m &= df[col] > val
        elif op == ">=":
            m &= df[col] >= val
        elif op == "<":
            m &= df[col] < val
        elif op == "<=":
            m &= df[col] <= val
    return m


def stats(r: pd.Series) -> dict:
    r = r.to_numpy(float)
    if len(r) == 0:
        return {"n": 0, "win_rate": 0.0, "avg_r": 0.0, "total_r": 0.0, "profit_factor": 0.0}
    w = r[r > 0]
    l = r[r <= 0]
    return {"n": int(len(r)), "win_rate": round(float((r > 0).mean()), 4),
            "avg_r": round(float(r.mean()), 4), "total_r": round(float(r.sum()), 2),
            "profit_factor": round(float(w.sum() / abs(l.sum())), 3) if l.sum() != 0 else None}


def rule_text(rule: list[tuple]) -> str:
    parts = []
    for col, op, val in rule:
        parts.append(f"{col} in {val}" if op == "in" else f"{col} {op} {val}")
    return " and ".join(parts)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--train", default="output/ict/opt_train_trades.csv")
    ap.add_argument("--test", default="output/ict/opt_test_trades.csv")
    ap.add_argument("--min-train-n", type=int, default=40)
    ap.add_argument("--min-win", type=float, default=0.50)
    ap.add_argument("--min-avg-r", type=float, default=0.20)
    ap.add_argument("--json-out", default="output/ict/rules.json")
    args = ap.parse_args()

    train = pd.read_csv(args.train)
    test = pd.read_csv(args.test)
    leaked = {c for c, _, _ in CANDIDATES} & POST_TRADE
    if leaked:
        raise SystemExit(f"look-ahead columns in the search: {sorted(leaked)}")
    atoms: list[list[tuple]] = [[c] for c in CANDIDATES]
    atoms += [[(col, "in", vals)] for col, vals in CATEGORICAL]
    rules: list[list[tuple]] = []
    for size in (1, 2, 3):
        rules += [list(c) for c in combinations(atoms, size)]
    rules = [[a for atom in r for a in atom] for r in rules]

    found = []
    for rule in rules:
        tr_m = mask(train, rule)
        st = stats(train.loc[tr_m, "r"])
        if st["n"] < args.min_train_n or st["win_rate"] < args.min_win or st["avg_r"] < args.min_avg_r:
            continue
        te_m = mask(test, rule)
        te = stats(test.loc[te_m, "r"])
        found.append({"rule": rule_text(rule), "train": st, "test": te})

    found.sort(key=lambda x: (-x["test"]["win_rate"], -x["test"]["avg_r"]))
    base_tr, base_te = stats(train["r"]), stats(test["r"])
    out = {"rules_tested": len(rules), "train_baseline": base_tr, "test_baseline": base_te,
           "survivors": len(found), "top": found[:40]}
    Path(args.json_out).write_text(json.dumps(out, indent=2), encoding="utf-8")

    print(f"tested {len(rules)} rules | train baseline {base_tr}")
    print(f"holdout baseline {base_te}")
    print(f"survivors in train: {len(found)}")
    print(f"{'rule':70s} {'n':>4} {'win':>6} {'avgR':>7} | {'n':>4} {'win':>6} {'avgR':>7} {'PF':>6}")
    for f in found[:15]:
        a, b = f["train"], f["test"]
        print(f"{f['rule'][:70]:70s} {a['n']:>4} {a['win_rate']:>6.3f} {a['avg_r']:>7.3f} | "
              f"{b['n']:>4} {b['win_rate']:>6.3f} {b['avg_r']:>7.3f} {str(b['profit_factor']):>6}")
    print(f"saved {args.json_out}")


if __name__ == "__main__":
    main()
