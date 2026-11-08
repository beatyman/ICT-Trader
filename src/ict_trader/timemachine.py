"""Time Machine（诚实胜率回放）—— 从 day-trader-skill 吸收。

在下单前，用标的自己的历史 bar 回放这个 setup，算出「先到止盈 / 先到止损 / 都不到」
的诚实概率，再回看历史同样 setup 的真实胜负。不是预测，是「用最近 N 天的真实波动
给这个计划照镜子」。

核心逻辑（简化自 trader-timemachine，适配加密 24/7 无固定交易时段）：
  1. 把每根历史 bar 转成「相对前收的比率」(open/high/low/close / prev_close)。
  2. Monte Carlo：从比率池随机采样 n_bars 根，逐根检查先触止损还是先触止盈。
  3. Historical：扫历史每个窗口，用同样的入场→止盈/止损规则回放真实结果。
  4. Verdict：用「先到止盈 / (先到止盈+先到止损)」对照 break-even（1R=50%, 2R=33%,
     3R=25%），给出「coin flip / leans toward target / strongly toward target」。
"""
from __future__ import annotations

import random

import numpy as np
import pandas as pd

__all__ = ["time_machine"]


def _break_even(rr: float) -> float:
    """盈亏平衡所需的「先到止盈占比」。1R=50%, 2R=33.3%, 3R=25%。"""
    return 1.0 / (1.0 + rr)


def _verdict(tp_first: float, sl_first: float, rr: float) -> str:
    hit = tp_first + sl_first
    if hit <= 0:
        return "insufficient data"
    tp_ratio = tp_first / hit
    be = _break_even(rr)
    diff = (tp_ratio - be) * 100  # 百分点
    if abs(diff) < 5:
        return "close to a coin flip" if abs(rr - 1.0) < 0.05 else "close to break-even"
    if diff >= 15:
        return "strongly toward the target"
    if diff <= -15:
        return "strongly toward the stop"
    return "leans toward the target" if diff > 0 else "leans toward the stop"


def time_machine(df: pd.DataFrame, entry: float, sl: float, tp: float,
                 direction: str = "long", n_bars: int = 16,
                 replays: int = 2000, seed: int = 19) -> dict:
    """回放 setup 的诚实胜率。返回 {tp_first_pct, sl_first_pct, neither_pct, rr,
    average_r, history, verdict, seed}。"""
    df = df.sort_values("date").reset_index(drop=True)
    close = df["close"].astype(float).to_numpy()
    high = df["high"].astype(float).to_numpy()
    low = df["low"].astype(float).to_numpy()
    n = len(df)
    if n < 40:
        return {"verdict": "insufficient data (<40 bars)", "seed": seed}

    risk = abs(entry - sl)
    rr = abs(tp - entry) / risk if risk > 0 else 1.0

    # 1. 每根 bar 相对前收的比率
    ratios = []
    for i in range(1, n):
        pc = close[i - 1]
        if pc <= 0:
            continue
        ratios.append((close[i] / pc, high[i] / pc, low[i] / pc))

    # 2. Monte Carlo 回放
    rng = random.Random(seed)
    tp_first = sl_first = neither = 0
    for _ in range(replays):
        cur = float(entry)
        hit = "neither"
        for _ in range(n_bars):
            cr, hr, lr = rng.choice(ratios)
            h = cur * hr
            l = cur * lr
            if direction == "long":
                if l <= sl:
                    hit = "stop"
                    break
                if h >= tp:
                    hit = "target"
                    break
            else:
                if h >= sl:
                    hit = "stop"
                    break
                if l <= tp:
                    hit = "target"
                    break
            cur *= cr
        if hit == "target":
            tp_first += 1
        elif hit == "stop":
            sl_first += 1
        else:
            neither += 1

    # 3. Historical：扫历史每个窗口，用「相对 setup」（入场→止损/止盈的距离）回放
    sl_dist = abs(entry - sl)
    tp_dist = abs(entry - tp)
    hist_tp = hist_sl = hist_neither = 0
    for i in range(n - n_bars):
        seg = df.iloc[i:i + n_bars + 1]
        p0 = float(seg["close"].iloc[0])
        if p0 <= 0:
            continue
        if direction == "long":
            _sl, _tp = p0 - sl_dist, p0 + tp_dist
        else:
            _sl, _tp = p0 + sl_dist, p0 - tp_dist
        hit = "neither"
        for j in range(1, len(seg)):
            h = float(seg["high"].iloc[j])
            l = float(seg["low"].iloc[j])
            if direction == "long":
                if l <= _sl:
                    hit = "stop"
                    break
                if h >= _tp:
                    hit = "target"
                    break
            else:
                if h >= _sl:
                    hit = "stop"
                    break
                if l <= _tp:
                    hit = "target"
                    break
        if hit == "target":
            hist_tp += 1
        elif hit == "stop":
            hist_sl += 1
        else:
            hist_neither += 1

    tp_pct = round(tp_first / replays * 100, 1)
    sl_pct = round(sl_first / replays * 100, 1)
    neither_pct = round(neither / replays * 100, 1)
    hist_total = hist_tp + hist_sl + hist_neither
    hist_line = (f"same rule on {hist_total} past windows fired: "
                 f"{hist_tp} target, {hist_sl} stop, {hist_neither} neither"
                 if hist_total else "no history")

    return {
        "replays": replays,
        "seed": seed,
        "tp_first_pct": tp_pct,
        "sl_first_pct": sl_pct,
        "neither_pct": neither_pct,
        "rr": round(rr, 2),
        "history": hist_line,
        "hist_tp": hist_tp,
        "hist_sl": hist_sl,
        "hist_neither": hist_neither,
        "verdict": _verdict(tp_first, sl_first, rr),
    }
