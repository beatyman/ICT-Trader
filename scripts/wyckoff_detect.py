"""威科夫结构检测 CLI。

用法::

    python scripts/wyckoff_detect.py --csv data/btc_15m.csv \
        --trend-len 50 --pivot-len 3 --json-out output/wyckoff.json

输出当前 Wyckoff 状态（阶段 A–E、方向、区间、目标 T1/T2、失效位）
与事件序列。非重绘，只用已收盘 K 线。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ict_trader.wyckoff import detect_wyckoff  # noqa: E402

PHASE_NAME = {0: "-", 1: "A", 2: "B", 3: "C", 4: "D", 5: "E"}
DIR_NAME = {1: "accumulation", -1: "distribution", 0: "none"}


def main() -> None:
    p = argparse.ArgumentParser(description="Wyckoff structure detector")
    p.add_argument("--csv", required=True)
    p.add_argument("--trend-len", type=int, default=50)
    p.add_argument("--climax-range-atr", type=float, default=1.2)
    p.add_argument("--climax-vol-mult", type=float, default=1.5)
    p.add_argument("--pivot-len", type=int, default=3)
    p.add_argument("--ar-min-atr", type=float, default=2.0)
    p.add_argument("--max-range-atr", type=float, default=10.0)
    p.add_argument("--min-range-bars", type=int, default=15)
    p.add_argument("--st-tol", type=float, default=0.35)
    p.add_argument("--spring-max-bars", type=int, default=3)
    p.add_argument("--fail-depth", type=float, default=0.30)
    p.add_argument("--max-range-bars", type=int, default=300)
    p.add_argument("--max-leg-bars", type=int, default=300)
    p.add_argument("--json-out", default=None)
    args = p.parse_args()

    df = pd.read_csv(args.csv, parse_dates=["date"]).sort_values("date").reset_index(drop=True)

    st, phases = detect_wyckoff(
        df, trend_len=args.trend_len,
        climax_range_atr=args.climax_range_atr,
        climax_vol_mult=args.climax_vol_mult,
        pivot_len=args.pivot_len, ar_min_atr=args.ar_min_atr,
        max_range_atr=args.max_range_atr, min_range_bars=args.min_range_bars,
        st_tol=args.st_tol, spring_max_bars=args.spring_max_bars,
        fail_depth=args.fail_depth, max_range_bars=args.max_range_bars,
        max_leg_bars=args.max_leg_bars,
    )

    out = {
        "stage": st.stage,
        "phase": st.phase,
        "direction": DIR_NAME[st.direction],
        "range": [None if pd.isna(st.range_bottom) else round(st.range_bottom, 6),
                  None if pd.isna(st.range_top) else round(st.range_top, 6)],
        "range_height": None if pd.isna(st.range_height) else round(st.range_height, 6),
        "climax": None if pd.isna(st.climax_extreme) else round(st.climax_extreme, 6),
        "creek_ice": None if pd.isna(st.creek_ice) else round(st.creek_ice, 6),
        "spring": st.spring_confirmed,
        "sos_bar": st.sos_bar,
        "lps": st.lps_confirmed,
        "t1": None if pd.isna(st.t1) else round(st.t1, 6),
        "t2": None if pd.isna(st.t2) else round(st.t2, 6),
        "invalidation": None if pd.isna(st.invalidation) else round(st.invalidation, 6),
        "events": [
            {"bar": int(b), "time": str(df["date"].iloc[b]) if 0 <= b < len(df) else None,
             "price": round(float(px), 6), "label": lbl}
            for b, px, lbl in st.events
        ],
    }
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
