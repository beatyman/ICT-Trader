#!/usr/bin/env python3
"""多周期结构化分析器 CLI：CSV → 输入 JSON → 输出结构化多空点位 JSON（纯 JSON）。"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ict_trader.structured_plan import structured_plan, build_input_from_data  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description="multi-timeframe structured plan")
    ap.add_argument("--csv-15m", required=True)
    ap.add_argument("--csv-4h", required=True)
    ap.add_argument("--csv-1h", default=None, help="1h OHLCV CSV (chanlun 1h)")
    ap.add_argument("--csv-daily", required=True)
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--market", default="crypto")
    ap.add_argument("--json-in", default=None, help="直接给输入 JSON（跳过特征提取）")
    ap.add_argument("--json-out", default=None)
    args = ap.parse_args()

    if args.json_in:
        inp = json.loads(Path(args.json_in).read_text(encoding="utf-8"))
    else:
        df_15m = pd.read_csv(args.csv_15m, parse_dates=["date"])
        df_4h = pd.read_csv(args.csv_4h, parse_dates=["date"])
        df_1h = pd.read_csv(args.csv_1h, parse_dates=["date"]) if args.csv_1h else None
        df_daily = pd.read_csv(args.csv_daily, parse_dates=["date"])
        inp = build_input_from_data(df_15m, df_4h, df_daily, args.symbol, args.market, df_1h)

    out = structured_plan(inp)
    txt = json.dumps(out, ensure_ascii=False, indent=2)
    if args.json_out:
        Path(args.json_out).write_text(txt, encoding="utf-8")
    print(txt)


if __name__ == "__main__":
    main()
