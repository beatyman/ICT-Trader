"""一目均衡表策略回测引擎、绩效指标与参数扫描。

策略流程（每根已收盘 K 线，无未来函数）::

    入场（多头）  三役好轉 = TK 金叉 + 迟行多头 + 价格上破云区
                  （默认过滤：云内 / 薄云 / 逆云弱信号不开仓）
    止损（多头）  Kijun 下方 或 云区远端（云底）下方，取较近/较远可配
    移动止损      跟随 Kijun 或 云区边缘上移（可配）
    出场          反向 TK 交叉 / 价格跌破 Kijun / 移动止损触发
    仓位          单笔风险 %  ÷ 止损距离（乘以信号分级系数）

绩效指标：交易数、胜率、盈亏比、期望值、最大回撤、夏普、年化、平均持仓。
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
import math

import numpy as np
import pandas as pd

from .indicators import ichimoku
from .signals import signals as build_signals

__all__ = [
    "Trade", "StrategyConfig", "run_backtest", "compute_metrics",
    "scan_params", "TIMEFRAME_RANK",
]


TIMEFRAME_RANK = {"15m": 1, "1h": 2, "4h": 3, "1d": 4, "1w": 5}


@dataclass
class Trade:
    entry_time: object
    exit_time: object
    direction: str          # "long" / "short"
    entry: float
    exit: float
    stop: float
    units: float
    pnl: float
    ret_pct: float          # 单笔收益（含方向）
    grade: str
    bars: int
    exit_reason: str


@dataclass
class StrategyConfig:
    risk_pct: float = 0.01          # 单笔风险（权益 %）
    stop_mode: str = "near"         # near=较近 / far=较远
    trail_mode: str = "kijun"       # kijun / cloud / none
    exit_reverse_tk: bool = True    # 反向 TK 交叉出场
    exit_close_through_kijun: bool = True  # 收盘穿越基准线出场
    min_cloud_atr: float = 0.0      # 薄云阈值（×ATR，0=不过滤）
    allow_in_cloud: bool = False    # 允许云内开仓（默认禁止）
    weak_coef: float = 0.0          # 弱信号仓位系数（0 或 0.25）
    min_timeframe_rank: int = 3     # 默认 ≥4H
    start_capital: float = 100_000.0


def _atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    h, l, c = df["high"], df["low"], df["close"]
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    return tr.rolling(period).mean()


def run_backtest(
    df: pd.DataFrame,
    cfg: StrategyConfig,
    tenkan_period: int = 9,
    kijun_period: int = 26,
    senkou_b_period: int = 52,
    offset_mode: str = "PLATFORM",
) -> tuple[list[Trade], pd.Series, dict]:
    """运行回测，返回 (trades, equity_curve, metrics)。"""
    ind = ichimoku(df, tenkan_period, kijun_period, senkou_b_period, offset_mode)
    sig = build_signals(df, ind, kijun_period, weak_coef=cfg.weak_coef)
    atr = _atr(df)

    close = df["close"].astype(float).to_numpy()
    high = df["high"].astype(float).to_numpy()
    low = df["low"].astype(float).to_numpy()
    kijun = ind["kijun"].to_numpy()
    tenkan = ind["tenkan"].to_numpy()
    cloud_top = sig["cloud_top"].to_numpy()
    cloud_bottom = sig["cloud_bottom"].to_numpy()
    cloud_thick = sig["cloud_thick"].to_numpy()
    atr_v = atr.to_numpy()
    three_bull = sig["three_bull"].to_numpy()
    three_bear = sig["three_bear"].to_numpy()
    tk_golden = sig["tk_golden"].to_numpy()
    tk_death = sig["tk_death"].to_numpy()
    above_cloud = sig["above_cloud"].to_numpy()
    below_cloud = sig["below_cloud"].to_numpy()
    in_cloud = sig["in_cloud"].to_numpy()
    cloud_bull = sig["cloud_bull"].to_numpy()
    grade = sig["grade"].to_numpy()
    coef = sig["coef"].to_numpy()

    n = len(df)
    equity = cfg.start_capital
    equity_curve = np.empty(n)
    equity_curve[:] = np.nan

    trades: list[Trade] = []
    pos = None  # dict: direction, entry, stop, units, grade, entry_i, coef

    for i in range(n):
        # 1) 先处理持仓的出场与移动止损（用当前已收盘 bar 的信息）
        if pos is not None:
            d = pos["direction"]
            stop = pos["stop"]
            reason = None
            # 移动止损：跟随 Kijun / 云区边缘
            if cfg.trail_mode != "none" and not np.isnan(kijun[i]):
                if cfg.trail_mode == "kijun":
                    ref = kijun[i]
                else:  # cloud edge
                    ref = cloud_bottom[i] if d == "long" else cloud_top[i]
                if d == "long":
                    stop = max(stop, ref)
                else:
                    stop = min(stop, ref)
            # 反向 TK 交叉
            if cfg.exit_reverse_tk:
                if d == "long" and tk_death[i]:
                    reason = "reverse_tk"
                elif d == "short" and tk_golden[i]:
                    reason = "reverse_tk"
            # 收盘穿越基准线
            if reason is None and cfg.exit_close_through_kijun and not np.isnan(kijun[i]):
                if d == "long" and close[i] < kijun[i]:
                    reason = "close_through_kijun"
                elif d == "short" and close[i] > kijun[i]:
                    reason = "close_through_kijun"
            # 止损 / 移动止损
            hit = False
            if reason is None:
                if d == "long" and low[i] <= stop:
                    hit = True
                elif d == "short" and high[i] >= stop:
                    hit = True
                if hit:
                    reason = "stop"
            if reason is not None:
                exit_px = stop if reason == "stop" else close[i]
                pnl = pos["units"] * (exit_px - pos["entry"]) * (1 if d == "long" else -1)
                ret = (exit_px - pos["entry"]) / pos["entry"] * (1 if d == "long" else -1)
                trades.append(Trade(
                    entry_time=df.index[pos["entry_i"]], exit_time=df.index[i],
                    direction=d, entry=pos["entry"], exit=exit_px, stop=stop,
                    units=pos["units"], pnl=pnl, ret_pct=ret,
                    grade=pos["grade"], bars=i - pos["entry_i"], exit_reason=reason,
                ))
                equity += pnl
                pos = None
        equity_curve[i] = equity

        # 2) 处理入场（仅在无持仓时）
        if pos is not None:
            continue
        # 三役好轉 / 三役逆轉
        want_long = three_bull[i]
        want_short = three_bear[i]
        if not (want_long or want_short):
            continue
        d = "long" if want_long else "short"
        # 过滤：云内（默认禁止）
        if in_cloud[i] and not cfg.allow_in_cloud:
            continue
        # 过滤：薄云（云厚度 < 阈值 × ATR）
        if cfg.min_cloud_atr > 0 and not np.isnan(atr_v[i]):
            if cloud_thick[i] < cfg.min_cloud_atr * atr_v[i]:
                continue
        # 信号分级：弱信号默认系数 0（不开仓）
        g = grade[i]
        c = coef[i]
        if c <= 0:
            continue
        # 止损：Kijun 或云区远端
        if d == "long":
            k_stop = kijun[i]
            c_stop = cloud_bottom[i]
            if np.isnan(k_stop):
                k_stop = c_stop
            if np.isnan(c_stop):
                c_stop = k_stop
            stop = max(k_stop, c_stop) if cfg.stop_mode == "near" else min(k_stop, c_stop)
        else:
            k_stop = kijun[i]
            c_stop = cloud_top[i]
            if np.isnan(k_stop):
                k_stop = c_stop
            if np.isnan(c_stop):
                c_stop = k_stop
            stop = min(k_stop, c_stop) if cfg.stop_mode == "near" else max(k_stop, c_stop)
        stop_dist = abs(close[i] - stop)
        if stop_dist <= 0:
            continue
        risk_amount = equity * cfg.risk_pct
        units = risk_amount / stop_dist * c
        pos = {"direction": d, "entry": close[i], "stop": stop, "units": units,
               "grade": g, "entry_i": i, "coef": c}

    # 收尾：强平最后一个持仓
    if pos is not None:
        d = pos["direction"]
        exit_px = close[n - 1]
        pnl = pos["units"] * (exit_px - pos["entry"]) * (1 if d == "long" else -1)
        ret = (exit_px - pos["entry"]) / pos["entry"] * (1 if d == "long" else -1)
        trades.append(Trade(
            entry_time=df.index[pos["entry_i"]], exit_time=df.index[n - 1],
            direction=d, entry=pos["entry"], exit=exit_px, stop=pos["stop"],
            units=pos["units"], pnl=pnl, ret_pct=ret, grade=pos["grade"],
            bars=(n - 1) - pos["entry_i"], exit_reason="eod",
        ))
        equity += pnl

    eq = pd.Series(equity_curve, index=df.index).fillna(cfg.start_capital)
    m = compute_metrics(trades, eq, df)
    return trades, eq, m


def compute_metrics(trades: list[Trade], equity: pd.Series, df: pd.DataFrame) -> dict:
    """从交易列表 + 权益曲线计算绩效指标。"""
    n = len(trades)
    if n == 0:
        return {"trades": 0, "win_rate": 0.0, "profit_factor": 0.0,
                "expectancy": 0.0, "max_drawdown": 0.0, "sharpe": 0.0,
                "annualized": 0.0, "avg_holding_bars": 0.0, "total_return": 0.0}

    pnls = np.array([t.pnl for t in trades], dtype=float)
    wins = pnls[pnls > 0]
    losses = pnls[pnls < 0]
    win_rate = len(wins) / n
    gross_profit = wins.sum()
    gross_loss = -losses.sum()
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")
    expectancy = pnls.mean()

    # 权益曲线回撤
    cummax = equity.cummax()
    dd = (equity - cummax) / cummax
    max_dd = float(dd.min())

    # 日收益 → 夏普 / 年化
    daily = equity.resample("1D").last().ffill().pct_change().dropna()
    sharpe = float(daily.mean() / daily.std() * math.sqrt(252)) if len(daily) > 1 and daily.std() > 0 else 0.0
    total_ret = float(equity.iloc[-1] / equity.iloc[0] - 1)
    days = max((df.index[-1] - df.index[0]).days, 1)
    annualized = float((1 + total_ret) ** (365.0 / days) - 1)

    avg_hold = float(np.mean([t.bars for t in trades]))

    return {
        "trades": n,
        "win_rate": round(win_rate, 4),
        "profit_factor": round(profit_factor, 4) if profit_factor != float("inf") else 99.0,
        "expectancy": round(float(expectancy), 2),
        "max_drawdown": round(max_dd, 4),
        "sharpe": round(sharpe, 3),
        "annualized": round(annualized, 4),
        "avg_holding_bars": round(avg_hold, 1),
        "total_return": round(total_ret, 4),
    }


def _grade_breakdown(trades: list[Trade]) -> dict:
    """信号分级与实际盈亏的关系。"""
    out = {}
    for g in ("strong", "medium", "weak"):
        gs = [t for t in trades if t.grade == g]
        if gs:
            out[g] = {
                "trades": len(gs),
                "win_rate": round(sum(1 for t in gs if t.pnl > 0) / len(gs), 3),
                "avg_ret_pct": round(float(np.mean([t.ret_pct for t in gs])), 5),
                "total_pnl": round(float(sum(t.pnl for t in gs)), 2),
            }
    return out


def scan_params(
    df: pd.DataFrame,
    param_sets: list[tuple[int, int, int]],
    cfg: StrategyConfig,
    in_sample_frac: float = 0.7,
) -> pd.DataFrame:
    """参数扫描 + 样本内/外分割。

    每个参数组在样本内（前 in_sample_frac）和样本外（后 1-in_sample_frac）
    各跑一遍，输出两段绩效。结果可复现（固定配置、无随机性）。
    """
    split = int(len(df) * in_sample_frac)
    rows = []
    for (tp, kp, sbp) in param_sets:
        for mode in ("PLATFORM", "HOSODA"):
            for tag, sub in (("in", df.iloc[:split]), ("out", df.iloc[split:])):
                if len(sub) < 60:
                    continue
                tr, eq, m = run_backtest(sub, cfg, tp, kp, sbp, mode)
                row = {"tenkan": tp, "kijun": kp, "senkou_b": sbp,
                       "offset": mode, "segment": tag, **m}
                rows.append(row)
    return pd.DataFrame(rows)
