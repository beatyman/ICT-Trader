"""威科夫方法可回测策略原型（量化代理）。

重要：Wyckoff 包含主观判读（事件是「被后续验证的假设」、量能/宽幅是相对描述），
不能 100% 自动化。本模块实现的是**可量化代理**：

    区间检测    -> 波动收缩 + 横盘（detect_wyckoff 的状态机）
    高潮候选    -> 高量 + 宽幅 + 拒绝/收回（SC/BC）
    AR/ST 代理  -> 区间上下沿测试 + 量能递减
    Spring/UTAD -> 假突破区间极值后快速收回
    SOS/SOW     -> 放量突破区间边界并收在外侧
    LPS/LPSY    -> 突破后回测边界不破

策略（每条信号）：背景过滤（只做与区间方向一致的信号）+ Stop order 代理/
收盘确认入场 + 三档止损 + 按风险%仓位 + TP1/TP2 部分止盈与保本。

无未来函数：所有判定只用已收盘 bar；入场最迟在信号 bar 收盘（或下一 bar 突破）。
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .wyckoff import detect_wyckoff, WyckoffSignal

__all__ = ["WyckoffStrategyConfig", "Trade", "run_backtest", "compute_metrics"]


@dataclass
class WyckoffStrategyConfig:
    risk_pct: float = 0.01            # 单笔风险（资金 %）
    stop_mode: str = "scenario"       # trigger / scenario / structural
    entry_mode: str = "stop_order"    # stop_order（意图 K 线破极值）/ close
    min_kind: str = "sos"             # 只交易 sos/sow 及之后（默认跳过 spring/utad 激进）
    trade_spring: bool = False        # 是否允许 spring/utad 直接入场（激进）
    partial_at_tp1: float = 0.5       # TP1 平仓比例
    breakeven_at: float = 0.5         # 达到投影 50% 后移止损到保本
    context_filter: bool = True       # 背景过滤（已由方向内建，这里保留开关）
    start_capital: float = 100_000.0


@dataclass
class Trade:
    entry_time: object
    exit_time: object
    direction: str
    entry: float
    exit: float
    stop: float
    units: float
    pnl: float
    ret_pct: float
    kind: str
    bars: int
    exit_reason: str


_KIND_RANK = {"spring": 1, "utad": 1, "sos": 2, "sow": 2, "lps": 3, "lpsy": 3}


def _signal_ok(sig: WyckoffSignal, cfg: WyckoffStrategyConfig) -> bool:
    if not cfg.trade_spring and sig.kind in ("spring", "utad"):
        return False
    if _KIND_RANK.get(sig.kind, 9) < _KIND_RANK.get(cfg.min_kind, 0):
        return False
    return True


def _stop_price(sig: WyckoffSignal, df: pd.DataFrame, cfg: WyckoffStrategyConfig) -> float:
    """三档止损（原文）：trigger / scenario / structural。"""
    d = 1 if sig.direction == "long" else -1
    if cfg.stop_mode == "trigger":
        # 意图 K 线反向极值
        if sig.direction == "long":
            return float(df["low"].iloc[sig.bar])
        return float(df["high"].iloc[sig.bar])
    if cfg.stop_mode == "scenario":
        # Spring 低点 / UTAD 高点（若未震仓则用高潮边）
        if np.isnan(sig.spring_extreme):
            return sig.climax_side
        return sig.spring_extreme
    # structural：结构另一侧
    return sig.range_bottom if d == 1 else sig.range_top


def run_backtest(
    df: pd.DataFrame,
    cfg: WyckoffStrategyConfig,
    **wyckoff_kwargs,
) -> tuple[list[Trade], pd.Series, dict]:
    """运行回测，返回 (trades, equity_curve, metrics)。"""
    st, _ = detect_wyckoff(df, **wyckoff_kwargs)
    signals = [s for s in st.signals if _signal_ok(s, cfg)]

    close = df["close"].astype(float).to_numpy()
    high = df["high"].astype(float).to_numpy()
    low = df["low"].astype(float).to_numpy()
    n = len(df)

    # 按 bar 索引信号
    sig_by_bar = {}
    for s in signals:
        sig_by_bar.setdefault(s.bar, []).append(s)

    equity = cfg.start_capital
    eq_curve = np.full(n, np.nan)
    trades: list[Trade] = []
    pos = None  # dict

    for i in range(n):
        # 1) 持仓管理
        if pos is not None:
            d = 1 if pos["direction"] == "long" else -1
            # 保本移动：达到投影 50% 后止损移到入场价
            if cfg.breakeven_at > 0:
                moved = (close[i] - pos["entry"]) * d
                half_dist = (pos["tp1"] - pos["entry"]) * d * cfg.breakeven_at
                if moved >= half_dist:
                    pos["stop"] = pos["entry"]
            exit_px = None
            reason = None
            if pos["direction"] == "long":
                if low[i] <= pos["stop"]:
                    exit_px, reason = pos["stop"], "stop"
                elif high[i] >= pos["tp1"] and pos["tp1_hit"] is False:
                    exit_px, reason = pos["tp1"], "tp1"
                    pos["tp1_hit"] = True
                elif high[i] >= pos["tp2"]:
                    exit_px, reason = pos["tp2"], "tp2"
            else:
                if high[i] >= pos["stop"]:
                    exit_px, reason = pos["stop"], "stop"
                elif low[i] <= pos["tp1"] and pos["tp1_hit"] is False:
                    exit_px, reason = pos["tp1"], "tp1"
                    pos["tp1_hit"] = True
                elif low[i] <= pos["tp2"]:
                    exit_px, reason = pos["tp2"], "tp2"
            if exit_px is not None:
                frac = cfg.partial_at_tp1 if reason == "tp1" else 1.0
                pnl = pos["units"] * (exit_px - pos["entry"]) * d * frac
                ret = (exit_px - pos["entry"]) / pos["entry"] * d
                trades.append(Trade(
                    entry_time=df.index[pos["entry_i"]], exit_time=df.index[i],
                    direction=pos["direction"], entry=pos["entry"], exit=exit_px,
                    stop=pos["stop"], units=pos["units"] * frac, pnl=pnl,
                    ret_pct=ret, kind=pos["kind"], bars=i - pos["entry_i"],
                    exit_reason=reason,
                ))
                equity += pnl
                if reason == "tp1":
                    pos["units"] *= (1 - cfg.partial_at_tp1)  # 剩余仓位继续
                else:
                    pos = None
        eq_curve[i] = equity

        # 2) 入场
        if pos is not None:
            continue
        for s in sig_by_bar.get(i, []):
            d = 1 if s.direction == "long" else -1
            # 入场价：Stop order 代理（意图 K 线破极值）或收盘确认
            if cfg.entry_mode == "close":
                entry = close[i]
                enter_bar = i
            else:
                entry = high[i] if s.direction == "long" else low[i]
                enter_bar = i + 1  # 下一根触发（实际由后续 bar 判断）
            stop = _stop_price(s, df, cfg)
            stop_dist = abs(entry - stop)
            if stop_dist <= 0:
                continue
            # Stop order 代理：下一根是否触发
            if cfg.entry_mode == "stop_order":
                if enter_bar >= n:
                    continue
                if s.direction == "long" and high[enter_bar] < entry:
                    continue  # 未突破
                if s.direction == "short" and low[enter_bar] > entry:
                    continue
            units = (equity * cfg.risk_pct) / stop_dist
            # 目标：T1=突破边+1×区间，T2=+2×区间（因果投影）
            rng = abs(s.range_top - s.range_bottom)
            tp1 = s.creek_ice + d * rng
            tp2 = s.creek_ice + d * 2 * rng
            pos = {"direction": s.direction, "entry": entry, "stop": stop,
                   "units": units, "kind": s.kind, "entry_i": enter_bar,
                   "tp1": tp1, "tp2": tp2, "tp1_hit": False}
            break

    # 强平
    if pos is not None:
        d = 1 if pos["direction"] == "long" else -1
        pnl = pos["units"] * (close[n - 1] - pos["entry"]) * d
        ret = (close[n - 1] - pos["entry"]) / pos["entry"] * d
        trades.append(Trade(
            entry_time=df.index[pos["entry_i"]], exit_time=df.index[n - 1],
            direction=pos["direction"], entry=pos["entry"], exit=close[n - 1],
            stop=pos["stop"], units=pos["units"], pnl=pnl, ret_pct=ret,
            kind=pos["kind"], bars=(n - 1) - pos["entry_i"], exit_reason="eod",
        ))
        equity += pnl

    eq = pd.Series(eq_curve, index=df.index).fillna(cfg.start_capital)
    m = compute_metrics(trades, eq, df)
    return trades, eq, m


def compute_metrics(trades: list[Trade], equity: pd.Series, df: pd.DataFrame) -> dict:
    n = len(trades)
    if n == 0:
        return {"trades": 0, "win_rate": 0.0, "profit_factor": 0.0,
                "expectancy": 0.0, "max_drawdown": 0.0, "sharpe": 0.0,
                "annualized": 0.0, "avg_holding_bars": 0.0, "total_return": 0.0}
    pnls = np.array([t.pnl for t in trades])
    wins = pnls[pnls > 0]
    losses = pnls[pnls < 0]
    win_rate = len(wins) / n
    gross_profit = wins.sum()
    gross_loss = -losses.sum()
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else 99.0
    expectancy = pnls.mean()
    cummax = equity.cummax()
    max_dd = float(((equity - cummax) / cummax).min())
    # 日频收益（优先 DatetimeIndex，否则用 date 列）
    eq = equity.copy()
    if not isinstance(eq.index, pd.DatetimeIndex) and "date" in df.columns:
        eq.index = pd.to_datetime(df["date"])
    if isinstance(eq.index, pd.DatetimeIndex):
        daily = eq.resample("1D").last().ffill().pct_change().dropna()
        days = max((eq.index[-1] - eq.index[0]).days, 1)
    else:
        daily = eq.pct_change().dropna()
        days = len(df)
    sharpe = float(daily.mean() / daily.std() * np.sqrt(252)) if len(daily) > 1 and daily.std() > 0 else 0.0
    total_ret = float(eq.iloc[-1] / eq.iloc[0] - 1)
    annualized = float((1 + total_ret) ** (365.0 / days) - 1)
    return {
        "trades": n, "win_rate": round(win_rate, 4),
        "profit_factor": round(profit_factor, 4), "expectancy": round(float(expectancy), 2),
        "max_drawdown": round(max_dd, 4), "sharpe": round(sharpe, 3),
        "annualized": round(annualized, 4), "avg_holding_bars": round(float(np.mean([t.bars for t in trades])), 1),
        "total_return": round(total_ret, 4),
    }
