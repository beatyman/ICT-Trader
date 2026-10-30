"""Generate historical ICT plan candidates by reusing the engine's auto plan.

Each candidate is derived from the structure up to a given ``as_of`` timestamp,
so there is no future leakage.  The plan dict is the same shape the injector
expects: direction / entry / sl / tp / plan_status / swept / last_time.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

import pandas as pd  # noqa: E402

from ict_trader.concepts import ICTConfig, analyze, load_csv  # noqa: E402
from plot_ict_signal import _auto_plan  # noqa: E402

_CFG = ICTConfig(
    mode="historical", show_sessions=False, show_htf_cisd=False,
    cisd_all=True, cisd_min_grade=0,
)


def generate_ict_plans(
    df_15m: pd.DataFrame,
    asofs: list[str],
    lookback: int = 300,
    sweep_window: int = 20,
) -> list[dict]:
    """One plan candidate per ``asof`` timestamp (aligned to forecast rows)."""
    df = df_15m.reset_index(drop=True)
    dates = pd.to_datetime(df["date"])
    plans: list[dict] = []

    for asof in asofs:
        ts = pd.Timestamp(asof)
        pos = dates.searchsorted(ts, side="right") - 1
        if pos < lookback:
            continue
        analysis = df.iloc[pos - lookback : pos + 1].reset_index(drop=True)
        result = analyze(analysis, _CFG)
        atr = float(
            (analysis["high"] - analysis["low"]).rolling(14, min_periods=1).mean().iloc[-1]
        ) or 1.0
        plan = _auto_plan(analysis, result, atr, min_gross_rr=2.5)
        if plan is None:
            continue

        n = len(analysis)
        swept = any(n - 1 - s.sweep_bar <= sweep_window for s in result.sweeps)
        plan.update({
            "swept": swept,
            "plan_status": "conditional",
            "last_time": str(df["date"].iloc[pos]),
            "bar_index": int(pos),          # global index into df_15m (for sim)
        })
        plans.append(plan)

    return plans


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", required=True)
    ap.add_argument("--forecasts-csv", required=True)
    ap.add_argument("--out", default="cache/ict_plans.json")
    args = ap.parse_args()

    df = load_csv(args.csv)
    fc = pd.read_csv(args.forecasts_csv)
    asofs = fc["as_of"].astype(str).tolist()
    plans = generate_ict_plans(df, asofs)
    import json
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(plans, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"plans={len(plans)} -> {args.out}")
