"""一目均衡表回测与参数扫描 CLI。

用法::

    python scripts/ichimoku_backtest.py backtest --csv data/btc_daily.csv \
        --tenkan 9 --kijun 26 --senkou-b 52 --offset PLATFORM \
        --timeframe 1d --risk-pct 0.01 --json-out output/ichimoku_metrics.json

    python scripts/ichimoku_backtest.py scan --csv data/btc_daily.csv \
        --timeframe 1d --json-out output/ichimoku_scan.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ichimoku import StrategyConfig, run_backtest, scan_params, TIMEFRAME_RANK  # noqa: E402


def _load(path: str) -> pd.DataFrame:
    df = pd.read_csv(path, parse_dates=["date"])
    df = df.sort_values("date").reset_index(drop=True)
    df.index = df["date"]
    return df


def _timeframe_rank(args) -> int:
    rank = TIMEFRAME_RANK.get(args.timeframe, 3)
    if args.allow_short_tf:
        return 1
    return rank


def cmd_backtest(args) -> None:
    df = _load(args.csv)
    cfg = StrategyConfig(
        risk_pct=args.risk_pct,
        stop_mode=args.stop_mode,
        trail_mode=args.trail_mode,
        min_cloud_atr=args.min_cloud_atr,
        allow_in_cloud=args.allow_in_cloud,
        weak_coef=args.weak_coef,
        min_timeframe_rank=_timeframe_rank(args),
    )
    if cfg.min_timeframe_rank < 3 and not args.allow_short_tf:
        print("refusing short timeframe (default >= 4h); pass --allow-short-tf to override")
        return
    trades, eq, m = run_backtest(df, cfg, args.tenkan, args.kijun, args.senkou_b, args.offset)
    out = {"params": {"tenkan": args.tenkan, "kijun": args.kijun,
                      "senkou_b": args.senkou_b, "offset": args.offset},
           "timeframe": args.timeframe,
           "metrics": m,
           "trades": len(trades)}
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    print(json.dumps(m, indent=2))


def cmd_scan(args) -> None:
    df = _load(args.csv)
    cfg = StrategyConfig(risk_pct=args.risk_pct, stop_mode=args.stop_mode,
                         trail_mode=args.trail_mode, min_cloud_atr=args.min_cloud_atr,
                         allow_in_cloud=args.allow_in_cloud, weak_coef=args.weak_coef)
    sets = [(9, 26, 52), (7, 22, 44), (8, 22, 44), (20, 60, 120)]
    res = scan_params(df, sets, cfg, in_sample_frac=args.in_frac)
    if args.json_out:
        Path(args.json_out).write_text(res.to_json(orient="records", indent=2), encoding="utf-8")
    print(res.to_string(index=False))


def main() -> None:
    p = argparse.ArgumentParser(description="Ichimoku backtest & scan")
    sub = p.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("backtest")
    b.add_argument("--csv", required=True)
    b.add_argument("--tenkan", type=int, default=9)
    b.add_argument("--kijun", type=int, default=26)
    b.add_argument("--senkou-b", type=int, default=52)
    b.add_argument("--offset", default="PLATFORM", choices=["PLATFORM", "HOSODA"])
    b.add_argument("--timeframe", default="1d")
    b.add_argument("--risk-pct", type=float, default=0.01)
    b.add_argument("--stop-mode", default="near", choices=["near", "far"])
    b.add_argument("--trail-mode", default="kijun", choices=["kijun", "cloud", "none"])
    b.add_argument("--min-cloud-atr", type=float, default=0.0)
    b.add_argument("--allow-in-cloud", action="store_true")
    b.add_argument("--weak-coef", type=float, default=0.0)
    b.add_argument("--allow-short-tf", action="store_true")
    b.add_argument("--json-out", default=None)
    b.set_defaults(func=cmd_backtest)

    s = sub.add_parser("scan")
    s.add_argument("--csv", required=True)
    s.add_argument("--timeframe", default="1d")
    s.add_argument("--risk-pct", type=float, default=0.01)
    s.add_argument("--stop-mode", default="near", choices=["near", "far"])
    s.add_argument("--trail-mode", default="kijun", choices=["kijun", "cloud", "none"])
    s.add_argument("--min-cloud-atr", type=float, default=0.0)
    s.add_argument("--allow-in-cloud", action="store_true")
    s.add_argument("--weak-coef", type=float, default=0.0)
    s.add_argument("--in-frac", type=float, default=0.7)
    s.add_argument("--json-out", default=None)
    s.set_defaults(func=cmd_scan)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
