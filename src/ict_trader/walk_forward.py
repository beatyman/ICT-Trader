"""Purged walk-forward evaluation for an already generated trade list.

A trade's outcome is only known at exit.  A training trade that exits inside
the embargo before the test window is purged, so its label cannot leak into the
test period.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

from .strategy import _metrics, portfolio_curve


def _as_trade(row) -> SimpleNamespace:
    return SimpleNamespace(
        entry_time=pd.Timestamp(row.entry_time),
        exit_time=pd.Timestamp(row.exit_time),
        r_multiple=float(row.r_multiple),
        bars=int(row.bars),
        direction=row.direction,
    )


def purged_walk_forward(trades, folds: int = 5, embargo_days: int = 5,
                        risk_pct: float = 0.01, max_concurrent: int = 5) -> list[dict]:
    if not trades or folds < 2:
        return []
    start = min(t.entry_time for t in trades)
    end = max(t.exit_time for t in trades)
    boundaries = pd.date_range(start, end, periods=folds + 1)
    embargo = pd.Timedelta(days=embargo_days)
    out: list[dict] = []
    for i in range(folds):
        test_start = boundaries[i]
        test_end = boundaries[i + 1]
        test_start_eff = test_start + embargo
        train = [t for t in trades
                 if t.entry_time < test_start and t.exit_time < (test_start - embargo)]
        test = [t for t in trades
                if test_start_eff <= t.entry_time < test_end]
        _, train_m = portfolio_curve(train, risk_pct, max_concurrent)
        _, test_m = portfolio_curve(test, risk_pct, max_concurrent)
        out.append({
            "fold": i + 1,
            "train_start": str(train[0].entry_time) if train else None,
            "train_end": str(train[-1].exit_time) if train else None,
            "test_start": str(test_start_eff),
            "test_end": str(test_end),
            "train": train_m,
            "test": test_m,
            "purged_train": len([t for t in trades
                                 if t.entry_time < test_start and t.exit_time >= test_start - embargo]),
        })
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--trades", required=True)
    ap.add_argument("--folds", type=int, default=5)
    ap.add_argument("--embargo-days", type=int, default=5)
    ap.add_argument("--out", default="output/ict/walk_forward.json")
    args = ap.parse_args()
    df = pd.read_csv(args.trades)
    df["entry_time"] = pd.to_datetime(df["entry_time"])
    df["exit_time"] = pd.to_datetime(df["exit_time"])
    trades = [_as_trade(r) for r in df.itertuples(index=False)]
    folds = purged_walk_forward(trades, args.folds, args.embargo_days)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(folds, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    rows = []
    for f in folds:
        rows.append({
            "fold": f["fold"],
            "purged_train": f["purged_train"],
            "train_n": f["train"]["trades"],
            "test_n": f["test"]["trades"],
            "train_avgR": round(f["train"]["avg_r"], 3),
            "test_avgR": round(f["test"]["avg_r"], 3),
            "train_PF": round(f["train"]["profit_factor"], 2),
            "test_PF": round(f["test"]["profit_factor"], 2),
            "test_return": round(f["test"]["total_return"], 3),
            "test_dd": round(f["test"]["max_dd"], 3),
        })
    print(pd.DataFrame(rows).to_string(index=False))
    print(f"walk-forward: {args.out}")


if __name__ == "__main__":
    main()

