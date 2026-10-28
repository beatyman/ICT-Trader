"""Command line interface for ICT Trader."""
from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

from .concepts import ICTConfig, analyze, atr as atr_series, load_csv, plot, signals_frame
from .setup import SetupConfig, build_setup, run_one
from .walk_forward import purged_walk_forward


def _write_json(path: str | Path, obj) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def _ict_config(args) -> ICTConfig:
    return ICTConfig(
        mode="historical" if args.historical else "present",
        swing_len=args.swing_len,
        show_htf_cisd=not args.no_htf,
        show_sessions=not args.no_sessions,
        show_sweeps=not args.no_sweeps,
        sweep_reclaim_bars=args.sweep_reclaim_bars,
        cisd_all=args.all_cisd,
        cisd_max=args.cisd_max,
        fvg_min_atr=args.fvg_min_atr,
        fvg_min_body=args.fvg_min_body,
    )


def cmd_analyze(args) -> None:
    df = load_csv(args.csv)
    cfg = _ict_config(args)
    res = analyze(df, cfg)
    out = plot(res, args.out, title=f"ICT - {args.symbol}", bars=args.bars)
    sig = signals_frame(res)
    sig.to_csv(args.signals, index=False, encoding="utf-8-sig")
    summary = {
        "symbol": args.symbol,
        "bars": len(df),
        "last_time": str(df["date"].iloc[-1]),
        "last_close": float(df["close"].iloc[-1]),
        "key_levels": len(res.key_levels),
        "sweeps": len(res.sweeps),
        "cisds": len(res.cisds),
        "chart": str(out),
        "signals": args.signals,
    }
    _write_json(args.json_out, summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


def _setup_config(args) -> SetupConfig:
    return SetupConfig(
        market=args.market,
        min_grade=args.min_grade,
        min_confluence=args.min_confluence,
        min_1d_score=args.min_1d_score,
        min_4h_score=args.min_4h_score,
        min_15m_score=args.min_15m_score,
        require_sweep=not args.allow_no_sweep,
        sweep_reclaim_bars=args.sweep_reclaim_bars,
        target_mode=args.target_mode,
        min_setup_score=args.min_setup_score,
        min_disp_body_ratio=args.min_disp_body,
        min_disp_atr=args.min_disp_atr,
        min_disp_multiple=args.min_disp_multiple,
        rr=args.rr,
        max_bars=args.max_bars,
        breakeven_at_r=args.breakeven_at_r,
        t_plus_1=args.t_plus_1,
        sell_tax_bps=args.sell_tax_bps,
        fvg_min_atr=args.fvg_min_atr,
        fvg_min_body=args.fvg_min_body,
        entry_mode=args.entry_mode,
        retest_max_bars=args.retest_max_bars,
        retest_tolerance_atr=args.retest_tolerance_atr,
        trail_activate_r=args.trail_activate_r,
        trail_atr_mult=args.trail_atr_mult,
        max_daily_loss_r=args.max_daily_loss_r,
    )


def cmd_plan(args) -> None:
    df = load_csv(args.csv)
    daily = load_csv(args.daily_csv) if args.daily_csv else None
    ict = ICTConfig(mode="historical", show_sessions=False, show_htf_cisd=False,
                    cisd_all=True, cisd_min_grade=0,
                    fvg_min_atr=args.fvg_min_atr, fvg_min_body=args.fvg_min_body,
                    sweep_reclaim_bars=args.sweep_reclaim_bars)
    res = analyze(df, ict)
    sigs = build_setup(df, res, _setup_config(args), daily=daily)
    # A plan is actionable only if the setup is recent; a years-old signal is
    # history, not a trade plan.
    max_age = args.max_age_bars
    recent = [s for s in sigs if len(df) - 1 - s.end <= max_age]
    latest = recent[-1] if recent else None
    price = float(df["close"].iloc[-1])
    last_atr = float(atr_series(df).iloc[-1])
    if not (last_atr > 0):
        last_atr = max(abs(price) * 0.005, 1e-9)

    target = None
    entry_distance_atr = None
    rejected = None
    if latest is not None:
        risk = abs(latest.level - latest.sl)
        target = latest.target or (
            latest.level + (2 * risk if latest.direction == "bull" else -2 * risk)
        )
        entry_distance_atr = abs(price - latest.level) / last_atr
        max_dist = args.max_entry_distance_atr * last_atr
        if latest.direction == "bull":
            price_ok = latest.sl < price < target
        else:
            price_ok = target < price < latest.sl
        if not price_ok:
            rejected = "price_outside_entry_risk"
        elif abs(price - latest.level) > max_dist:
            rejected = "price_too_far_from_entry"
        if rejected:
            latest = None

    card = {
        "symbol": args.symbol,
        "last_time": str(df["date"].iloc[-1]),
        "last_close": price,
        "decision": "TRADE" if latest else "NO_TRADE",
        "setup": None,
        "rejected": rejected,
        "entry_distance_atr": entry_distance_atr,
        "recent_sweeps": [
            {"direction": s.direction, "name": s.name, "level": s.level,
             "time": str(df["date"].iloc[s.reclaim_bar])}
            for s in res.sweeps[-5:]
        ],
    }
    if latest is not None:
        card["setup"] = {
            "direction": latest.direction,
            "level": latest.level,
            "entry_reference": latest.level,
            "stop": latest.sl,
            "target": target,
            "r_multiple": abs(target - latest.level) / max(abs(latest.level - latest.sl), 1e-12),
            "grade": latest.grade,
            "quality": latest.quality,
            "checks": latest.checks,
            "signal_time": str(df["date"].iloc[latest.end]),
            "entry_distance_atr": entry_distance_atr,
        }
    _write_json(args.json_out, card)
    print(json.dumps(card, ensure_ascii=False, indent=2))


def cmd_backtest(args) -> None:
    cfg = _setup_config(args)
    trades, metrics = run_one(args.csv, cfg, daily_path=args.daily_csv)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame([asdict(t) for t in trades]).to_csv(args.out, index=False, encoding="utf-8-sig")
    _write_json(args.metrics, {"metrics": metrics, "trades": len(trades)})
    print(json.dumps({"trades": len(trades), **metrics}, ensure_ascii=False, indent=2))
    print(f"trades: {args.out}")


def cmd_walk_forward(args) -> None:
    df = pd.read_csv(args.trades)
    df["entry_time"] = pd.to_datetime(df["entry_time"])
    df["exit_time"] = pd.to_datetime(df["exit_time"])
    trades = [SimpleNamespace(entry_time=r.entry_time, exit_time=r.exit_time,
                              r_multiple=float(r.r_multiple), bars=int(r.bars),
                              direction=r.direction)
              for r in df.itertuples(index=False)]
    folds = purged_walk_forward(trades, args.folds, args.embargo_days)
    _write_json(args.out, folds)
    rows = [{"fold": f["fold"], "train": f["train"]["trades"], "test": f["test"]["trades"],
             "train_avgR": round(f["train"]["avg_r"], 3),
             "test_avgR": round(f["test"]["avg_r"], 3),
             "test_PF": round(f["test"]["profit_factor"], 2)} for f in folds]
    print(pd.DataFrame(rows).to_string(index=False))


def _add_data_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--csv", required=True, help="OHLCV CSV: date,open,high,low,close,volume")
    p.add_argument("--symbol", default="UNKNOWN")
    p.add_argument("--bars", type=int, default=260)
    p.add_argument("--out", default="output/chart.png")
    p.add_argument("--signals", default="output/signals.csv")
    p.add_argument("--json-out", default="output/analysis.json")
    p.add_argument("--historical", action="store_true")
    p.add_argument("--swing-len", type=int, default=5)
    p.add_argument("--cisd-max", type=int, default=2)
    p.add_argument("--all-cisd", action="store_true")
    p.add_argument("--no-htf", action="store_true")
    p.add_argument("--no-sessions", action="store_true")
    p.add_argument("--no-sweeps", action="store_true")
    p.add_argument("--sweep-reclaim-bars", type=int, default=3)
    p.add_argument("--fvg-min-atr", type=float, default=0.0)
    p.add_argument("--fvg-min-body", type=float, default=0.0)


def _add_setup_args(p: argparse.ArgumentParser, data: bool = True) -> None:
    if data:
        p.add_argument("--csv", required=True)
        p.add_argument("--daily-csv", default=None)
        p.add_argument("--symbol", default="UNKNOWN")
    p.add_argument("--market", choices=["cn", "hk", "crypto", "other"], default="cn")
    p.add_argument("--min-grade", type=int, default=3)
    p.add_argument("--min-confluence", type=int, default=2)
    p.add_argument("--min-1d-score", type=int, default=3)
    p.add_argument("--min-4h-score", type=int, default=2)
    p.add_argument("--min-15m-score", type=int, default=0)
    p.add_argument("--allow-no-sweep", action="store_true")
    p.add_argument("--sweep-reclaim-bars", type=int, default=3,
                   help="bars allowed for price to close back through a swept level")
    p.add_argument("--target-mode", choices=["fixed", "liquidity", "hybrid"], default="fixed",
                   help="fixed R, nearest opposing liquidity pool, or liquidity with fixed fallback")
    p.add_argument("--min-setup-score", type=float, default=0.0)
    p.add_argument("--min-disp-body", type=float, default=0.0)
    p.add_argument("--min-disp-atr", type=float, default=0.0)
    p.add_argument("--min-disp-multiple", type=float, default=0.0)
    p.add_argument("--rr", type=float, default=2.0)
    p.add_argument("--max-bars", type=int, default=40)
    p.add_argument("--breakeven-at-r", type=float, default=0.0)
    p.add_argument("--t-plus-1", action="store_true")
    p.add_argument("--sell-tax-bps", type=float, default=5.0)
    p.add_argument("--fvg-min-atr", type=float, default=0.25)
    p.add_argument("--fvg-min-body", type=float, default=0.5)
    p.add_argument("--entry-mode", choices=["next_open", "retest_key"], default="next_open",
                   help="enter at next open, or wait for a retest of the CISD key level")
    p.add_argument("--retest-max-bars", type=int, default=20)
    p.add_argument("--retest-tolerance-atr", type=float, default=0.0)
    p.add_argument("--trail-activate-r", type=float, default=0.0,
                   help="activate ATR trailing after this favourable R (0=off)")
    p.add_argument("--trail-atr-mult", type=float, default=0.0,
                   help="trailing distance in entry-bar ATR (0=off)")
    p.add_argument("--max-daily-loss-r", type=float, default=0.0,
                   help="stop new entries after this daily closed R loss (0=off)")


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="ict-trader", description="ICT/SMC analysis, setup and backtest")
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("analyze", help="key levels, sweeps, CISD and chart")
    _add_data_args(a)
    a.set_defaults(func=cmd_analyze)

    p = sub.add_parser("plan", help="latest frozen-setup trade card")
    _add_setup_args(p)
    p.add_argument("--max-age-bars", type=int, default=50,
                   help="only treat a setup this recent as actionable")
    p.add_argument("--max-entry-distance-atr", type=float, default=0.5,
                   help="reject a plan if price has already moved this far from entry")
    p.add_argument("--json-out", default="output/trade_plan.json")
    p.set_defaults(func=cmd_plan)

    b = sub.add_parser("backtest", help="event-driven setup backtest")
    _add_setup_args(b)
    b.add_argument("--out", default="output/trades.csv")
    b.add_argument("--metrics", default="output/metrics.json")
    b.set_defaults(func=cmd_backtest)

    w = sub.add_parser("walk-forward", help="purged walk-forward from a trade CSV")
    w.add_argument("--trades", required=True)
    w.add_argument("--folds", type=int, default=5)
    w.add_argument("--embargo-days", type=int, default=5)
    w.add_argument("--out", default="output/walk_forward.json")
    w.set_defaults(func=cmd_walk_forward)

    args = ap.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()

