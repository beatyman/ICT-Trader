"""Baseline vs Kronos-combined backtest scaffold.

This file wires the pieces together but keeps every heavy dependency
optional:

* ICT plans come from :func:`ict_plan_provider` (plug the real engine here).
* Kronos forecasts are read from the offline cache CSV (never computed here).
* :mod:`injector` applies the soft gate in three modes.

Nothing in this file imports Kronos, so the comparison runs even when the
model is absent (using the adapter's mock cache for a smoke test).
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from injector import batch_filter, filter_plan_with_kronos


# --------------------------------------------------------------------- #
# data + plan provider
# --------------------------------------------------------------------- #
def load_15m(csv: str) -> pd.DataFrame:
    df = pd.read_csv(csv, parse_dates=["date"])
    return df


def ict_plan_provider(df: pd.DataFrame) -> list[dict]:
    """Return the engine's conditional/triggered plans.

    TODO(experiment): call the real ICT engine here, e.g.

        from ict_trader.rules import audit
        report = audit(df, daily=daily, market="crypto")
        # -> collect per-bar setup candidates with direction/entry/sl/tp

    For the scaffold we return an empty list; run_backtest is driven by the
    caller-provided ``--plans-json`` until the engine hook is wired.
    """
    return []


# --------------------------------------------------------------------- #
# simulation
# --------------------------------------------------------------------- #
def simulate_trade(df: pd.DataFrame, plan: dict, start_i: int, max_bars: int = 200) -> dict | None:
    """Resolve one trade: did TP or SL hit first in the realised path?

    Returns a dict with ``result`` in {'win','loss'} and ``r`` (realised R)
    or None if neither side was reached before the data ends.
    """
    d = "long" if plan["direction"] in {"bull", "long"} else "short"
    entry = float(plan["entry"])
    sl = float(plan["sl"])
    tp = float(plan["tp"])
    risk = abs(entry - sl)
    if risk == 0:
        return None

    for j in range(start_i, min(len(df), start_i + max_bars)):
        bar = df.iloc[j]
        if d == "long":
            if bar.low <= sl:
                return {"result": "loss", "r": -1.0}
            if bar.high >= tp:
                return {"result": "win", "r": abs(tp - entry) / risk}
        else:
            if bar.high >= sl:
                return {"result": "loss", "r": -1.0}
            if bar.low <= tp:
                return {"result": "win", "r": abs(tp - entry) / risk}
    return None


def compute_metrics(trades: list[dict]) -> dict:
    if not trades:
        return {"trades": 0, "win_rate": float("nan"), "net_rr": float("nan"),
                "avg_r": float("nan"), "rejected": 0}
    wins = [t for t in trades if t["result"] == "win"]
    losses = [t for t in trades if t["result"] == "loss"]
    gross_loss = abs(sum(t["r"] for t in losses))
    gross_profit = sum(t["r"] for t in wins)
    return {
        "trades": len(trades),
        "win_rate": len(wins) / len(trades),
        "net_rr": (gross_profit / gross_loss) if gross_loss > 0 else float("inf"),
        "avg_r": sum(t["r"] for t in trades) / len(trades),
        "rejected": sum(1 for t in trades if t.get("rejected")),
    }


# --------------------------------------------------------------------- #
# run
# --------------------------------------------------------------------- #
def run(df: pd.DataFrame, plans: list[dict], forecasts: pd.DataFrame) -> pd.DataFrame:
    """Compare baseline (pure ICT) vs the three Kronos modes."""
    fmap = {str(r["as_of"]): r for r in forecasts.to_dict("records")}
    idx_of = {str(p.get("last_time")): i for i, p in enumerate(plans)}

    modes = {
        "baseline_ict": None,
        "A_judas_filter": "judas",
        "B_momentum_boost": "momentum",
        "C_target_tune": "target",
    }
    rows = []
    for name, mode in modes.items():
        sim = []
        for p in plans:
            start_i = int(p.get("bar_index", idx_of.get(str(p.get("last_time")), 0)))
            if mode is None:
                pp = p
            else:
                f = fmap.get(str(p.get("last_time")))
                if f is None:
                    continue
                pp = filter_plan_with_kronos(p, f, mode=mode)
                if pp["plan_status"].startswith("REJECTED"):
                    sim.append({"result": "rejected", "r": 0.0, "rejected": True})
                    continue
            res = simulate_trade(df, pp, start_i)
            if res is None:
                continue
            res["rejected"] = False
            sim.append(res)
        m = compute_metrics(sim)
        m["mode"] = name
        rows.append(m)

    return pd.DataFrame(rows)[["mode", "trades", "win_rate", "net_rr", "avg_r", "rejected"]]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", required=True, help="15m OHLCV")
    ap.add_argument("--plans-json", default=None, help="list of ICT plan dicts")
    ap.add_argument("--forecasts-csv", default=None, help="Kronos adapter cache CSV")
    args = ap.parse_args()

    df = load_15m(args.csv)
    plans = []
    if args.plans_json:
        import json
        plans = json.loads(Path(args.plans_json).read_text(encoding="utf-8"))
    else:
        plans = ict_plan_provider(df)

    forecasts = pd.DataFrame()
    if args.forecasts_csv:
        forecasts = pd.read_csv(args.forecasts_csv)

    if not plans:
        print("no plans: wire ict_plan_provider or pass --plans-json")
        return

    table = run(df, plans, forecasts)
    print(table.to_string(index=False))


if __name__ == "__main__":
    main()
