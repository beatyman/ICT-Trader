"""自动分析工具：5 分钟循环（live 单次 / backtest）+ 10000 美元模拟账户。

用法：
  # 单次分析（live 循环的一拍）
  python scripts/auto_analyze.py live --csv data/btc_5m.csv --daily-csv data/btc_daily.csv \
      --csv-15m data/btc_15m.csv --csv-4h data/btc_4h.csv --market crypto

  # 回测（10000 美元模拟账户）
  python scripts/auto_analyze.py backtest --csv data/btc_15m.csv --daily-csv data/btc_daily.csv \
      --market crypto --capital 10000 --risk-pct 0.01 --json-out output/auto_btc.json

流程：背景层(Wyckoff+Ichimoku) → 主策略(ICT) → 融合层 → 评分层 → 记录/反馈。
无未来函数：每个决策点只用该 bar 及之前的数据。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ict_trader.background import (wyckoff_background, ichimoku_background,  # noqa: E402
                                   chanlun_background)
from ict_trader.main_strategy import ict_signal  # noqa: E402
from ict_trader.fusion import fuse  # noqa: E402
from ict_trader.scoring import score  # noqa: E402
from ict_trader.feedback import TradeRecord, feedback_score, attribution, discover_problems  # noqa: E402


def _infer_freq(df: pd.DataFrame) -> str:
    """从时间间隔推断 K 线周期。"""
    if len(df) < 2:
        return "15m"
    secs = pd.Series(pd.to_datetime(df["date"])).diff().median().total_seconds()
    if secs < 60:
        return "1m"
    if secs < 300:
        return "5m"
    if secs < 900:
        return "15m"
    if secs < 1800:
        return "30m"
    if secs < 3600:
        return "1h"
    if secs < 14400:
        return "4h"
    return "1d"


def analyze_once(df_exec: pd.DataFrame, df_daily: pd.DataFrame, df_htf: pd.DataFrame,
                 market: str, symbol: str, timestamp: str) -> dict:
    """跑完整流水线一次，返回评分 JSON。"""
    ict = ict_signal(df_exec, df_daily, market)
    wy = wyckoff_background(df_htf)
    ichi = ichimoku_background(df_htf)
    # 缠论：HTF（日线）决定方向，LTF（执行周期）做入场择时
    chan_htf = chanlun_background(df_daily, freq="1d")
    chan_ltf = chanlun_background(df_exec, freq=_infer_freq(df_exec))
    fused = fuse(ict, wy, ichi, chan_htf, chan_ltf)
    return score(ict, wy, ichi, fused, symbol=symbol, timestamp=timestamp)


def run_backtest(df: pd.DataFrame, df_daily: pd.DataFrame, df_htf: pd.DataFrame,
                 market: str, symbol: str, capital: float = 10000.0,
                 risk_pct: float = 0.01, warmup: int = 300, step: int = 1) -> dict:
    """10000 美元模拟账户回测（逐 bar 决策，无未来函数）。"""
    close = df["close"].astype(float).to_numpy()
    high = df["high"].astype(float).to_numpy()
    low = df["low"].astype(float).to_numpy()
    n = len(df)
    equity = capital
    eq_curve = np.full(n, np.nan)
    records: list[TradeRecord] = []
    pos = None  # dict

    for i in range(warmup, n - 1, step):
        # 决策：只用 [0:i+1] 数据
        df_win = df.iloc[:i + 1]
        df_d_win = df_daily[df_daily["date"] <= df["date"].iloc[i]]
        df_h_win = df_htf[df_htf["date"] <= df["date"].iloc[i]]
        if len(df_d_win) < 60 or len(df_h_win) < 60:
            continue
        s = analyze_once(df_win, df_d_win, df_h_win, market, symbol,
                         str(df["date"].iloc[i]))

        # 持仓管理（在决策前，用当前 bar 的 high/low）
        if pos is not None:
            d = pos["dir"]
            reason = None
            exit_px = None
            if d == "long":
                if low[i] <= pos["stop"]:
                    exit_px, reason = pos["stop"], "stop"
                elif high[i] >= pos["tp2"]:
                    exit_px, reason = pos["tp2"], "tp2"
                elif high[i] >= pos["tp1"] and not pos["tp1_hit"]:
                    exit_px, reason = pos["tp1"], "tp1"
            else:
                if high[i] >= pos["stop"]:
                    exit_px, reason = pos["stop"], "stop"
                elif low[i] <= pos["tp2"]:
                    exit_px, reason = pos["tp2"], "tp2"
                elif low[i] <= pos["tp1"] and not pos["tp1_hit"]:
                    exit_px, reason = pos["tp1"], "tp1"
            if exit_px is not None:
                frac = 0.5 if reason == "tp1" else 1.0
                pnl = pos["units"] * (exit_px - pos["entry"]) * (1 if d == "long" else -1) * frac
                ret = (exit_px - pos["entry"]) / pos["entry"] * (1 if d == "long" else -1)
                rec = pos["rec"]
                rec.exit_price = exit_px
                rec.exit_reason = reason
                rec.pnl = pnl
                rec.ret_pct = ret
                rec.direction_correct = (d == "long" and exit_px > pos["entry"]) or \
                                        (d == "short" and exit_px < pos["entry"])
                loss_ratio = abs(exit_px - pos["entry"]) / max(abs(pos["entry"] - pos["stop"]), 1e-9)
                rec.feedback = feedback_score(rec, loss_ratio)
                records.append(rec)
                equity += pnl
                if reason == "tp1":
                    pos["tp1_hit"] = True
                    pos["units"] *= 0.5
                    pos["stop"] = pos["entry"]  # 保本
                else:
                    pos = None
        eq_curve[i] = equity

        # 新开仓
        if pos is not None or s.get("action") != "enter":
            continue
        d = s["fused_direction"]
        entry = s["entry"]
        stop = s["stop"]
        tp1 = s["tp1"]
        tp2 = s["tp2"]
        if stop == 0 or entry == 0 or abs(entry - stop) < 1e-9:
            continue
        size_factor = s.get("size_factor", 0.0)
        units = (equity * risk_pct * size_factor) / abs(entry - stop)
        rec = TradeRecord(
            time=s["timestamp"], symbol=symbol, direction=d, entry=entry,
            stop=stop, tp1=tp1, tp2=tp2, invalidation=s.get("invalidation", 0.0),
            confidence=s["fused_confidence"], score=s["score"], size_factor=size_factor,
            setup_type=s["ict"]["setup_type"],
            wyckoff_bias=s["wyckoff_background"]["bias"],
            ichimoku_cloud=s["ichimoku_background"]["cloud_state"],
            session="", penalty_flags=list(s.get("red_flags", [])),
        )
        pos = {"dir": d, "entry": entry, "stop": stop, "tp1": tp1, "tp2": tp2,
               "units": units, "tp1_hit": False, "rec": rec}

    # 强平
    if pos is not None:
        d = pos["dir"]
        pnl = pos["units"] * (close[-1] - pos["entry"]) * (1 if d == "long" else -1)
        rec = pos["rec"]
        rec.exit_price = close[-1]
        rec.exit_reason = "eod"
        rec.pnl = pnl
        rec.ret_pct = (close[-1] - pos["entry"]) / pos["entry"] * (1 if d == "long" else -1)
        rec.direction_correct = (d == "long" and close[-1] > pos["entry"]) or \
                                (d == "short" and close[-1] < pos["entry"])
        rec.feedback = feedback_score(rec, 1.0)
        records.append(rec)
        equity += pnl

    eq = pd.Series(eq_curve, index=df.index).ffill().fillna(capital)
    cummax = eq.cummax()
    max_dd = float(((eq - cummax) / cummax).min())
    pnls = [r.pnl for r in records]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    gp, gl = sum(wins), -sum(losses)
    n_trades = len(records)
    out = {
        "symbol": symbol, "capital": capital, "final_equity": round(equity, 2),
        "net_return": round((equity - capital) / capital, 4),
        "max_drawdown": round(max_dd, 4),
        "return_over_dd": round(((equity - capital) / capital) / abs(max_dd), 3) if max_dd < 0 else 0.0,
        "trades": n_trades,
        "win_rate": round(len(wins) / n_trades, 3) if n_trades else 0.0,
        "profit_factor": round(gp / gl, 3) if gl > 0 else 99.0,
        "expectancy": round(float(np.mean(pnls)), 2) if pnls else 0.0,
        "avg_feedback": round(float(np.mean([r.feedback for r in records])), 3) if records else 0.0,
        "attribution": attribution(records),
        "problems": discover_problems(records),
        "trades_detail": [
            {"time": r.time, "dir": r.direction, "entry": r.entry, "exit": r.exit_price,
             "reason": r.exit_reason, "pnl": round(r.pnl, 2), "feedback": r.feedback}
            for r in records
        ],
    }
    return out


def main() -> None:
    p = argparse.ArgumentParser(description="Auto analyzer (live / backtest)")
    sub = p.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("backtest")
    b.add_argument("--csv", required=True)
    b.add_argument("--daily-csv", required=True)
    b.add_argument("--htf-csv", required=True)   # 4H/日线背景（可传日线或4H）
    b.add_argument("--market", default="crypto")
    b.add_argument("--symbol", default="")
    b.add_argument("--capital", type=float, default=10000.0)
    b.add_argument("--risk-pct", type=float, default=0.01)
    b.add_argument("--warmup", type=int, default=300)
    b.add_argument("--step", type=int, default=1)
    b.add_argument("--limit", type=int, default=0, help="only use the last N bars (0 = all)")
    b.add_argument("--json-out", default=None)
    b.set_defaults(func=lambda a: _cmd_backtest(a))

    l = sub.add_parser("live")
    l.add_argument("--csv", required=True)
    l.add_argument("--daily-csv", required=True)
    l.add_argument("--htf-csv", required=True)
    l.add_argument("--market", default="crypto")
    l.add_argument("--symbol", default="")
    l.add_argument("--json-out", default=None)
    l.set_defaults(func=lambda a: _cmd_live(a))

    args = p.parse_args()
    args.func(args)


def _load(path):
    df = pd.read_csv(path, parse_dates=["date"]).sort_values("date").reset_index(drop=True)
    return df


def _cmd_live(args):
    df = _load(args.csv)
    d = _load(args.daily_csv)
    h = _load(args.htf_csv)
    out = analyze_once(df, d, h, args.market, args.symbol, str(df["date"].iloc[-1]))
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(out, indent=2, ensure_ascii=False))


def _cmd_backtest(args):
    df = _load(args.csv)
    d = _load(args.daily_csv)
    h = _load(args.htf_csv)
    symbol = args.symbol or Path(args.csv).stem
    if args.limit > 0:
        df = df.tail(args.limit).reset_index(drop=True)
    out = run_backtest(df, d, h, args.market, symbol, args.capital, args.risk_pct, args.warmup, args.step)
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({k: v for k, v in out.items() if k not in ("trades_detail", "attribution", "problems")},
                     indent=2, ensure_ascii=False))
    print("\nproblems:", json.dumps(out["problems"], ensure_ascii=False))


if __name__ == "__main__":
    main()
