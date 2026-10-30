"""Baseline (pure ICT) vs Kronos-combined backtest.

Pipeline:

    df_15m ──► ICT plans (ict_plans.generate_ict_plans, one per as_of)
    forecasts CSV (Kronos, generated on GPU) ──► aligned by as_of
    injector (strict/judas/momentum/target) ──► filtered plan
    simulate_trade ──► win/loss + realised R
    compute_metrics ──► comparison table
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

from ict_plans import generate_ict_plans
from injector import filter_plan_with_kronos


# --------------------------------------------------------------------- #
def simulate_trade(df: pd.DataFrame, plan: dict, start_i: int, max_bars: int = 200) -> dict | None:
    d = "long" if plan["direction"] in {"bull", "long"} else "short"
    entry = float(plan["entry"])
    sl = float(plan["sl"])
    tp = float(plan["tp"])
    risk = abs(entry - sl)
    if risk <= 0:
        return None

    for j in range(start_i + 1, min(len(df), start_i + max_bars)):
        bar = df.iloc[j]
        if d == "long":
            if bar.low <= sl:
                return {"result": "loss", "r": -1.0, "swept": plan.get("swept", False)}
            if bar.high >= tp:
                return {"result": "win", "r": abs(tp - entry) / risk, "swept": plan.get("swept", False)}
        else:
            if bar.high >= sl:
                return {"result": "loss", "r": -1.0, "swept": plan.get("swept", False)}
            if bar.low <= tp:
                return {"result": "win", "r": abs(tp - entry) / risk, "swept": plan.get("swept", False)}
    return None


def compute_metrics(trades: list[dict]) -> dict:
    closed = [t for t in trades if t.get("result") in ("win", "loss")]
    if not closed:
        return {"trades": 0, "win_rate": float("nan"), "net_rr": float("nan"),
                "avg_r": float("nan"), "false_breakout_pct": float("nan"), "rejected": len(trades)}
    wins = [t for t in closed if t["result"] == "win"]
    losses = [t for t in closed if t["result"] == "loss"]
    gross_loss = abs(sum(t["r"] for t in losses))
    gross_profit = sum(t["r"] for t in wins)
    false_brk = [t for t in closed if t.get("swept") and t["result"] == "loss"]
    return {
        "trades": len(closed),
        "win_rate": len(wins) / len(closed),
        "net_rr": (gross_profit / gross_loss) if gross_loss > 0 else float("inf"),
        "avg_r": sum(t["r"] for t in closed) / len(closed),
        "false_breakout_pct": len(false_brk) / len(closed),
        "rejected": len(trades) - len(closed),
    }


# --------------------------------------------------------------------- #
def run(df: pd.DataFrame, plans: list[dict], forecasts: pd.DataFrame) -> pd.DataFrame:
    fmap = {str(r["as_of"]): r for r in forecasts.to_dict("records")}

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
            pp = p
            if mode is not None:
                f = fmap.get(str(p.get("last_time")))
                if f is None:
                    continue
                pp = filter_plan_with_kronos(p, f, mode=mode)
                if pp["plan_status"].startswith("REJECTED"):
                    sim.append({"result": "rejected", "r": 0.0})
                    continue
            res = simulate_trade(df, pp, int(p["bar_index"]))
            if res is not None:
                sim.append(res)
        m = compute_metrics(sim)
        m["mode"] = name
        rows.append(m)

    cols = ["mode", "trades", "win_rate", "net_rr", "avg_r", "false_breakout_pct", "rejected"]
    return pd.DataFrame(rows)[cols]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", required=True, help="15m OHLCV")
    ap.add_argument("--forecasts-csv", required=True, help="Kronos forecast cache CSV")
    args = ap.parse_args()

    df = pd.read_csv(args.csv, parse_dates=["date"])
    forecasts = pd.read_csv(args.forecasts_csv)
    asofs = forecasts["as_of"].astype(str).tolist()

    print(f"bars={len(df)}  forecasts={len(forecasts)}  generating ICT plans...")
    plans = generate_ict_plans(df, asofs)
    print(f"ICT plans={len(plans)}")

    table = run(df, plans, forecasts)
    print(table.round(3).to_string(index=False))


if __name__ == "__main__":
    main()
