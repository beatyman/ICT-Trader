"""三引擎融合分析 CLI（Wyckoff + Ichimoku + ICT）。

用法::

    python scripts/fusion_analyze.py --csv data/btc_15m.csv \
        --daily-csv data/btc_daily.csv --market crypto

流程：分别跑 Wyckoff（15m）/ Ichimoku（日线）/ ICT（15m+日线），
映射成 FusionInput，执行融合决策并输出标准化计划。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ict_trader.wyckoff import detect_wyckoff  # noqa: E402
from ict_trader.confluence import FusionInput, render, decide  # noqa: E402
from ichimoku.indicators import ichimoku as ichi_ind  # noqa: E402
from ichimoku.signals import signals as ichi_sig  # noqa: E402


def run_engines(df: pd.DataFrame, daily: pd.DataFrame, market: str) -> FusionInput:
    """跑三引擎，映射成 FusionInput。"""
    # ---- Wyckoff (15m) ----
    st, _ = detect_wyckoff(df)
    phase = "neutral"
    if st.direction == 1:
        phase = "accumulation"
    elif st.direction == -1:
        phase = "distribution"
    wy_valid = st.direction != 0 and not st.failed
    key_level = st.invalidation if not np.isnan(st.invalidation) else st.climax_side
    if np.isnan(key_level):
        key_level = st.range_bottom if st.direction == 1 else st.range_top

    # ---- Ichimoku (daily) ----
    d = daily.copy()
    d.index = pd.to_datetime(d["date"])
    ind = ichi_ind(d, 9, 26, 52, "PLATFORM")
    sig = ichi_sig(d, ind, 26)
    last = -1
    if bool(sig["below_cloud"].iloc[last]):
        ichi_filter = "short"
    elif bool(sig["above_cloud"].iloc[last]):
        ichi_filter = "long"
    else:
        ichi_filter = "wait"
    cloud_bottom = float(sig["cloud_bottom"].iloc[last])
    cloud_top = float(sig["cloud_top"].iloc[last])

    # ---- ICT ----
    from ict_trader.rules import audit
    a = audit(df, daily=daily, market=market)
    by_id = {r["rule_id"]: r for r in a["rules"]}
    ict_dir = "wait"
    r503 = by_id.get("L5-03")
    if r503 and isinstance(r503.get("value"), dict):
        d_ = r503["value"].get("direction")
        ict_dir = "long" if d_ == "long" else "short" if d_ == "short" else "wait"
    price_in_premium = False
    r506 = by_id.get("L5-06")
    if r506 and isinstance(r506.get("value"), dict):
        price_in_premium = r506["value"].get("zone") == "premium"

    return FusionInput(
        current_price=float(df["close"].iloc[-1]),
        wyckoff_phase=phase, wyckoff_valid=wy_valid, key_level=key_level,
        ichimoku_filter=ichi_filter, cloud_bottom=cloud_bottom, cloud_top=cloud_top,
        ict_direction=ict_dir, price_in_premium=price_in_premium,
        wants_premium_for_short=True,
    )


def main() -> None:
    p = argparse.ArgumentParser(description="Three-engine fusion analysis")
    p.add_argument("--csv", required=True)
    p.add_argument("--daily-csv", required=True)
    p.add_argument("--market", default="crypto")
    p.add_argument("--json-out", default=None)
    args = p.parse_args()

    df = pd.read_csv(args.csv, parse_dates=["date"]).sort_values("date").reset_index(drop=True)
    daily = pd.read_csv(args.daily_csv, parse_dates=["date"]).sort_values("date").reset_index(drop=True)

    inp = run_engines(df, daily, args.market)
    plan = decide(inp)

    out = {
        "current_price": round(inp.current_price, 2),
        "input": {
            "wyckoff": {"phase": inp.wyckoff_phase, "valid": inp.wyckoff_valid,
                        "key_level": round(inp.key_level, 2)},
            "ichimoku": {"filter": inp.ichimoku_filter, "cloud_bottom": round(inp.cloud_bottom, 2),
                         "cloud_top": round(inp.cloud_top, 2)},
            "ict": {"direction": inp.ict_direction, "price_in_premium": inp.price_in_premium},
        },
        "plan": {
            "direction": plan.direction, "lean": plan.lean, "position_state": plan.position_state,
            "entry": [plan.entry_low, plan.entry_high], "stop": plan.stop,
            "target1": plan.target1, "target2": plan.target2,
            "invalidations": plan.invalidations, "confidence": plan.confidence,
            "layers": plan.layers,
        },
    }
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    print(render(inp))


if __name__ == "__main__":
    main()
