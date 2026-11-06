"""SMT 跨品种背离检测 CLI（自动拉相关品种）。

配对规则（用户约定）：
  ETH     -> BTC（拉比特币）
  其他山寨 -> ETH（拉以太坊作市场基准）

用法：
  python scripts/smt_analyze.py --csv data/eth_15m.csv --symbol ETHUSDT
  python scripts/smt_analyze.py --symbol SOLUSDT            # 自动拉主品种 + 相关品种

流程：resolve_pair -> fetch_binance_klines(相关品种) -> detect_smt -> 输出 JSON。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ict_trader.ict_extras import resolve_pair, fetch_binance_klines  # noqa: E402
from ict_trader.enhancements import detect_smt  # noqa: E402


def main() -> None:
    p = argparse.ArgumentParser(description="SMT cross-asset divergence")
    p.add_argument("--csv", default=None, help="primary symbol OHLCV CSV (omit to auto-fetch)")
    p.add_argument("--symbol", required=True, help="e.g. ETHUSDT / SOLUSDT / BTCUSDT")
    p.add_argument("--interval", default="15m")
    p.add_argument("--sensitivity", default="Normal", choices=["Sensitive", "Normal", "Strict"])
    p.add_argument("--json-out", default=None)
    args = p.parse_args()

    sym = args.symbol.upper()
    pair = resolve_pair(sym)
    if not pair:
        print(json.dumps({"error": f"no correlated pair for {sym}"}, ensure_ascii=False))
        return

    # 主品种数据
    if args.csv:
        df = pd.read_csv(args.csv, parse_dates=["date"]).sort_values("date").reset_index(drop=True)
    else:
        df = fetch_binance_klines(sym, args.interval, 1000)
        if df is None:
            print(json.dumps({"error": f"failed to fetch {sym}"}, ensure_ascii=False))
            return

    # 相关品种数据
    cmp_df = fetch_binance_klines(pair, args.interval, 1000)
    if cmp_df is None:
        print(json.dumps({"error": f"failed to fetch pair {pair}"}, ensure_ascii=False))
        return

    smts = detect_smt(df, cmp_df, sensitivity=args.sensitivity, symbol=sym)
    out = {
        "symbol": sym,
        "pair": pair,
        "sensitivity": args.sensitivity,
        "smt_count": len(smts),
        "smts": [
            {"direction": s.direction, "start": int(s.start), "end": int(s.end),
             "price": round(float(s.price), 6), "cmp_price": round(float(s.cmp_price), 6)}
            for s in smts
        ],
    }
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(out, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
