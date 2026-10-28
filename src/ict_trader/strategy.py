"""ICT/CISD strategy + event-driven backtest.

Signal:
  * a confirmed CISD on the base timeframe
  * optional HTF CISD alignment
  * minimum grade and minimum confluence

Execution:
  * enter at the next bar open (no look-ahead)
  * stop at the CISD invalidation level
  * target at the measured level or a fixed R multiple
  * conservative same-bar rule: if both SL and TP are touched, SL wins
  * fixed-fractional risk, commission and slippage

The module is deliberately data-source agnostic: feed it a CSV or a Futu frame.
"""
from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd

from .concepts import ICTConfig, ICTResult, analyze, fetch_futu, load_csv


@dataclass
class StrategyConfig:
    risk_pct: float = 0.01          # fraction of equity risked per trade
    rr: float = 2.0                 # fallback target in R
    max_bars: int = 40              # time stop
    min_grade: int = 4              # 3=C, 4=B-, 5=B+, 6=A, 7=A+
    min_confluence: int = 3         # sweep / PDA / volume / session / target / SMT
    require_htf: bool = False       # require HTF CISD same direction
    t_plus_1: bool = False          # A-share T+1: cannot exit on entry day
    sell_tax_bps: float = 0.0       # A-share stamp duty on sell
    allow_long: bool = True
    allow_short: bool = True
    commission_bps: float = 5.0
    slippage_bps: float = 2.0
    entry_delay: int = 1            # bars after confirmation
    max_trades_per_day: int = 4
    max_concurrent: int = 5
    min_rr: float = 1.2             # skip signals with a worse measured target
    breakeven_at_r: float = 0.0     # move SL to entry at this favourable R
    min_displacement_atr: float = 0.0


@dataclass
class Trade:
    symbol: str
    direction: str
    signal_time: pd.Timestamp
    entry_time: pd.Timestamp
    exit_time: pd.Timestamp
    entry: float
    exit: float
    sl: float
    tp: float
    r_multiple: float
    reason: str
    grade: int
    confluence: int
    bars: int
    checks: dict[str, bool] = field(default_factory=dict)
    mae_r: float = 0.0
    mfe_r: float = 0.0
    time_to_mae: int = 0
    time_to_mfe: int = 0
    be_triggered: bool = False
    t1_locked: bool = False
    sell_tax: float = 0.0


@dataclass
class BacktestResult:
    symbol: str
    trades: list[Trade]
    equity: pd.Series
    metrics: dict


def _confluence(c: "CISD") -> int:  # type: ignore[name-defined]
    return sum(1 for k in ("sweep", "pda", "volume", "session", "targets", "smt") if c.checks.get(k))


def _latest_htf(result: ICTResult, t: int) -> str | None:
    candidates = [c for c in result.htf_cisds if c.end <= t]
    if not candidates:
        return None
    return max(candidates, key=lambda c: c.end).direction


def simulate_cisd(
    df: pd.DataFrame,
    result: ICTResult,
    config: StrategyConfig | None = None,
    symbol: str = "",
) -> BacktestResult:
    cfg = config or StrategyConfig()
    n = len(df)
    open_ = df["open"].to_numpy(float)
    high = df["high"].to_numpy(float)
    low = df["low"].to_numpy(float)
    close = df["close"].to_numpy(float)
    dates = pd.to_datetime(df["date"]).reset_index(drop=True)

    trades: list[Trade] = []
    equity = 1.0
    eq_points: list[tuple[pd.Timestamp, float]] = [(dates.iloc[0], equity)]
    day_counts: dict[object, int] = {}
    busy_until = -1

    # result.cisds is already time-ordered; use only information at c.end
    for c in result.cisds:
        t = c.end
        if t + cfg.entry_delay >= n:
            continue
        if t + cfg.entry_delay <= busy_until:
            continue
        if c.direction == "bull" and not cfg.allow_long:
            continue
        if c.direction == "bear" and not cfg.allow_short:
            continue
        if c.grade < cfg.min_grade:
            continue
        conf = _confluence(c)
        if conf < cfg.min_confluence:
            continue
        if cfg.require_htf:
            htf_dir = _latest_htf(result, t)
            if htf_dir is None or htf_dir != c.direction:
                continue

        risk = abs(c.level - c.sl)
        if risk <= 0:
            continue
        tp = c.target
        if tp is None or abs(tp - c.level) / risk < cfg.min_rr:
            tp = c.level + cfg.rr * risk if c.direction == "bull" else c.level - cfg.rr * risk

        entry_idx = t + cfg.entry_delay
        day = dates.iloc[entry_idx].date()
        if day_counts.get(day, 0) >= cfg.max_trades_per_day:
            continue
        day_counts[day] = day_counts.get(day, 0) + 1

        raw_entry = open_[entry_idx]
        slip = raw_entry * cfg.slippage_bps / 10000.0
        entry = raw_entry + slip if c.direction == "bull" else raw_entry - slip
        risk = abs(entry - c.sl)
        if risk <= 0:
            continue

        # A-share T+1: the first bar on a later trading day is the earliest exit
        entry_day = dates.iloc[entry_idx].date()
        first_exit_idx = entry_idx + 1
        if cfg.t_plus_1:
            first_exit_idx = n - 1
            for k in range(entry_idx + 1, n):
                if dates.iloc[k].date() != entry_day:
                    first_exit_idx = k
                    break

        # gap through stop/target at the entry bar
        exit_price = None
        exit_idx = None
        reason = ""
        t1_locked = False
        if c.direction == "bull":
            if open_[entry_idx] <= c.sl:
                exit_price = open_[entry_idx]
                exit_idx = entry_idx
                reason = "gap_sl"
            elif open_[entry_idx] >= tp:
                exit_price = open_[entry_idx]
                exit_idx = entry_idx
                reason = "gap_tp"
        else:
            if open_[entry_idx] >= c.sl:
                exit_price = open_[entry_idx]
                exit_idx = entry_idx
                reason = "gap_sl"
            elif open_[entry_idx] <= tp:
                exit_price = open_[entry_idx]
                exit_idx = entry_idx
                reason = "gap_tp"

        # A-share T+1: if the entry gaps through the stop, the trade cannot be
        # exited the same day, so the realistic action is to skip it.
        if cfg.t_plus_1 and reason == "gap_sl":
            continue

        mae = 0.0
        mfe = 0.0
        mae_idx = entry_idx
        mfe_idx = entry_idx
        be_armed = False
        if exit_idx is None:
            end_idx = min(n - 1, entry_idx + cfg.max_bars)
            sl_current = c.sl
            for k in range(entry_idx, end_idx + 1):
                # excursions are journaled even on a T+1 locked day
                if c.direction == "bull":
                    fav = high[k] - entry
                    adv = low[k] - entry
                else:
                    fav = entry - low[k]
                    adv = entry - high[k]
                if fav > mfe:
                    mfe, mfe_idx = fav, k
                if adv < mae:
                    mae, mae_idx = adv, k

                # cannot exit on the entry day for A shares
                if cfg.t_plus_1 and k < first_exit_idx:
                    continue

                # first tradable bar of a new day: gap can fill stop/target
                if cfg.t_plus_1 and k == first_exit_idx:
                    if c.direction == "bull":
                        if open_[k] <= sl_current:
                            exit_price, exit_idx, reason = open_[k], k, "gap_sl"
                            break
                        if open_[k] >= tp:
                            exit_price, exit_idx, reason = open_[k], k, "gap_tp"
                            break
                    else:
                        if open_[k] >= sl_current:
                            exit_price, exit_idx, reason = open_[k], k, "gap_sl"
                            break
                        if open_[k] <= tp:
                            exit_price, exit_idx, reason = open_[k], k, "gap_tp"
                            break

                if c.direction == "bull":
                    hit_sl = low[k] <= sl_current
                    hit_tp = high[k] >= tp
                else:
                    hit_sl = high[k] >= sl_current
                    hit_tp = low[k] <= tp
                if hit_sl and hit_tp:
                    exit_price, exit_idx, reason = sl_current, k, "sl"
                    break
                if hit_sl:
                    exit_price, exit_idx, reason = sl_current, k, "sl"
                    break
                if hit_tp:
                    exit_price, exit_idx, reason = tp, k, "tp"
                    break
                if cfg.breakeven_at_r > 0 and not be_armed:
                    moved = (high[k] - entry) if c.direction == "bull" else (entry - low[k])
                    if moved >= cfg.breakeven_at_r * risk:
                        sl_current = entry
                        be_armed = True
            if exit_idx is None:
                if cfg.t_plus_1 and first_exit_idx > end_idx:
                    exit_idx = min(first_exit_idx, n - 1)
                    exit_price = open_[exit_idx]
                    reason = "t1_time"
                    t1_locked = True
                else:
                    exit_price, exit_idx, reason = close[end_idx], end_idx, "time"

        assert exit_price is not None and exit_idx is not None
        busy_until = exit_idx
        # exit cost: commission + A-share sell stamp duty
        gross = (exit_price - entry) if c.direction == "bull" else (entry - exit_price)
        tax = exit_price * cfg.sell_tax_bps / 10000.0
        net = gross - (entry + exit_price) * (cfg.commission_bps / 10000.0) / 2 - tax
        r = net / risk
        equity *= (1 + cfg.risk_pct * r)
        trades.append(Trade(
            symbol=symbol,
            direction=c.direction,
            signal_time=dates.iloc[t],
            entry_time=dates.iloc[entry_idx],
            exit_time=dates.iloc[exit_idx],
            entry=float(entry),
            exit=float(exit_price),
            sl=float(c.sl),
            tp=float(tp),
            r_multiple=float(r),
            reason=reason,
            grade=int(c.grade),
            confluence=int(conf),
            bars=int(exit_idx - entry_idx + 1),
            checks=dict(c.checks),
            mae_r=float(mae / risk),
            mfe_r=float(mfe / risk),
            time_to_mae=int(mae_idx - entry_idx),
            time_to_mfe=int(mfe_idx - entry_idx),
            be_triggered=bool(be_armed),
            t1_locked=bool(t1_locked),
            sell_tax=float(tax),
        ))
        eq_points.append((dates.iloc[exit_idx], equity))

    eq = pd.Series([p[1] for p in eq_points], index=pd.DatetimeIndex([p[0] for p in eq_points]))
    metrics = _metrics(trades, eq)
    return BacktestResult(symbol=symbol, trades=trades, equity=eq, metrics=metrics)


def _metrics(trades: list[Trade], equity: pd.Series) -> dict:
    if not trades:
        return {
            "trades": 0, "win_rate": 0.0, "avg_r": 0.0, "total_r": 0.0,
            "profit_factor": 0.0, "max_dd": 0.0, "total_return": 0.0,
            "sharpe": 0.0, "avg_bars": 0.0,
        }
    r = np.array([t.r_multiple for t in trades], dtype=float)
    wins = r[r > 0]
    losses = r[r < 0]
    eq = equity.to_numpy(float)
    peak = np.maximum.accumulate(eq)
    dd = (eq / peak - 1).min() if len(eq) else 0.0
    years = max((equity.index[-1] - equity.index[0]).total_seconds() / (365.25 * 86400), 1 / 365)
    total_return = eq[-1] - 1 if len(eq) else 0.0
    cagr = (eq[-1] ** (1 / years) - 1) if len(eq) and eq[-1] > 0 else 0.0
    tpy = len(trades) / years
    sharpe = (r.mean() / r.std() * math.sqrt(tpy)) if len(r) > 1 and r.std() > 0 else 0.0
    return {
        "trades": len(trades),
        "win_rate": float((r > 0).mean()),
        "avg_r": float(r.mean()),
        "total_r": float(r.sum()),
        "profit_factor": float(wins.sum() / abs(losses.sum())) if len(losses) and losses.sum() != 0 else float("inf"),
        "max_dd": float(dd),
        "total_return": float(total_return),
        "cagr": float(cagr),
        "sharpe": float(sharpe),
        "avg_bars": float(np.mean([t.bars for t in trades])),
    }


def run_one(path: str | Path, cfg: StrategyConfig, ict_cfg: ICTConfig | None = None,
            symbol: str | None = None) -> BacktestResult:
    df = load_csv(path)
    sym = symbol or Path(path).stem
    ict_cfg = ict_cfg or ICTConfig(mode="historical", show_sessions=False, show_htf_cisd=cfg.require_htf,
                                   cisd_all=True, cisd_min_grade=0)
    ict_cfg.cisd_min_displacement_atr = cfg.min_displacement_atr
    if cfg.require_htf and len(df) > 2:
        median_delta = pd.to_datetime(df["date"]).diff().dt.total_seconds().median()
        ict_cfg.htf_tfs = ("1W", "1M") if median_delta and median_delta > 20 * 3600 else ("1h", "4h")
    res = analyze(df, ict_cfg)
    return simulate_cisd(df, res, cfg, sym)


def portfolio_curve(trades: list[Trade], risk_pct: float, max_concurrent: int) -> tuple[pd.Series, dict]:
    """Simple portfolio overlay: limited concurrent positions, fixed risk."""
    trades = sorted(trades, key=lambda t: t.entry_time)
    equity = 1.0
    open_exits: list[pd.Timestamp] = []
    points: list[tuple[pd.Timestamp, float]] = []
    taken_trades: list[Trade] = []
    taken = 0
    skipped = 0
    for t in trades:
        open_exits = [x for x in open_exits if x > t.entry_time]
        if max_concurrent > 0 and len(open_exits) >= max_concurrent:
            skipped += 1
            continue
        equity *= (1 + risk_pct * t.r_multiple)
        open_exits.append(t.exit_time)
        points.append((t.exit_time, equity))
        taken_trades.append(t)
        taken += 1
    eq = pd.Series([p[1] for p in points], index=pd.DatetimeIndex([p[0] for p in points])) if points else pd.Series(dtype=float)
    m = _metrics(taken_trades, eq)
    m["taken"] = taken
    m["skipped_concurrent"] = skipped
    return eq, m


def run_universe(paths: Iterable[str | Path], cfg: StrategyConfig,
                 ict_cfg: ICTConfig | None = None, limit: int | None = None) -> tuple[list[Trade], dict]:
    paths = list(paths)
    if limit:
        paths = paths[:limit]
    all_trades: list[Trade] = []
    per_symbol = []
    for i, p in enumerate(paths, 1):
        try:
            r = run_one(p, cfg, ict_cfg)
        except Exception as exc:  # noqa: BLE001
            print(f"skip {p}: {exc}")
            continue
        all_trades.extend(r.trades)
        per_symbol.append({"symbol": r.symbol, **r.metrics})
        if i % 25 == 0:
            print(f"progress {i}/{len(paths)} trades={len(all_trades)}")
    all_trades.sort(key=lambda t: t.entry_time)
    _, all_metrics = portfolio_curve(all_trades, cfg.risk_pct, 0)
    _, portfolio = portfolio_curve(all_trades, cfg.risk_pct, cfg.max_concurrent)
    return all_trades, {
        "trades": len(all_trades),
        **all_metrics,
        "portfolio": portfolio,
        "per_symbol": per_symbol,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="ICT/CISD strategy backtest")
    ap.add_argument("--csv", help="single CSV")
    ap.add_argument("--dir", help="directory of CSV files")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--out", default="output/ict/trades.csv")
    ap.add_argument("--metrics", default="output/ict/strategy_metrics.json")
    ap.add_argument("--rr", type=float, default=2.0)
    ap.add_argument("--max-bars", type=int, default=40)
    ap.add_argument("--breakeven-at-r", type=float, default=0.0)
    ap.add_argument("--t-plus-1", action="store_true")
    ap.add_argument("--sell-tax-bps", type=float, default=0.0)
    ap.add_argument("--min-grade", type=int, default=4)
    ap.add_argument("--min-confluence", type=int, default=3)
    ap.add_argument("--min-displacement", type=float, default=0.0)
    ap.add_argument("--risk-pct", type=float, default=0.01)
    ap.add_argument("--max-concurrent", type=int, default=5)
    ap.add_argument("--require-htf", action="store_true")
    ap.add_argument("--no-short", action="store_true")
    ap.add_argument("--no-long", action="store_true")
    args = ap.parse_args()

    cfg = StrategyConfig(
        rr=args.rr, max_bars=args.max_bars, min_grade=args.min_grade,
        breakeven_at_r=args.breakeven_at_r,
        t_plus_1=args.t_plus_1, sell_tax_bps=args.sell_tax_bps,
        min_confluence=args.min_confluence, risk_pct=args.risk_pct,
        require_htf=args.require_htf, allow_short=not args.no_short, allow_long=not args.no_long,
        min_displacement_atr=args.min_displacement,
        max_concurrent=args.max_concurrent,
    )
    ict_cfg = ICTConfig(mode="historical", show_sessions=False, show_htf_cisd=args.require_htf,
                        cisd_all=True, cisd_min_grade=0, cisd_max=3)

    if args.csv:
        r = run_one(args.csv, cfg, ict_cfg)
        trades = r.trades
        metrics = {"trades": len(trades), **r.metrics, "per_symbol": [{"symbol": r.symbol, **r.metrics}]}
    elif args.dir:
        paths = sorted(Path(args.dir).glob("*.csv"))
        trades, metrics = run_universe(paths, cfg, ict_cfg, args.limit)
    else:
        raise SystemExit("--csv or --dir required")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    rows = [t.__dict__ for t in trades]
    pd.DataFrame(rows).to_csv(out, index=False, encoding="utf-8-sig")
    Path(args.metrics).parent.mkdir(parents=True, exist_ok=True)
    Path(args.metrics).write_text(json.dumps(metrics, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps(metrics, ensure_ascii=False, indent=2, default=str))
    print(f"trades: {out}")


if __name__ == "__main__":
    main()

