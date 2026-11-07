"""筹码分布（CYQ）/ 成交量分布（Volume Profile）。

从 InStock 的筹码分布算法吸收，用于把「key level = 流动性」这条用「筹码密集度」
精确化：筹码/成交量密集区 = 天然支撑阻力 = 流动性密集区。

两种实现：
  * ``cyq_chip_distribution``   —— InStock 三角分布 + 换手率衰减（需 turnover 列，
    A股用；加密/港股无换手率时回退到成交量分布）。
  * ``volume_profile``          —— 成交量分布（加密/港股的「筹码分布」等价物，
    只需 volume）。

两者都返回 (prices, density) 两个等长数组；``chip_density_at`` 计算某价位
±tol 范围内的筹码/成交量占比（0-1），用于给 key level 打「流动性密集度」分。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

__all__ = ["volume_profile", "cyq_chip_distribution", "chip_density_at"]


def volume_profile(df: pd.DataFrame, window: int = 210, bins: int = 150,
                   ) -> tuple[list[float], list[float]]:
    """成交量分布：把窗口内每根 K 线的成交量按价格区间堆叠。

    加密/港股的「筹码分布」等价物（只需 volume 列）。
    返回 (prices, density)，density 已归一化到总和 1。
    """
    sub = df.tail(window)
    if len(sub) == 0:
        return [], []
    lo = float(sub["low"].min())
    hi = float(sub["high"].max())
    acc = max(0.01, (hi - lo) / (bins - 1))
    prices = [lo + acc * i for i in range(bins)]
    density = np.zeros(bins, dtype=float)
    vol = sub["volume"].astype(float).to_numpy()
    low = sub["low"].astype(float).to_numpy()
    high = sub["high"].astype(float).to_numpy()
    for i in range(len(sub)):
        L = int((low[i] - lo) / acc)
        H = int((high[i] - lo) / acc + 0.99)
        L = max(0, min(L, bins - 1))
        H = max(0, min(H, bins - 1))
        n = max(H - L + 1, 1)
        v = vol[i] / n
        density[L:H + 1] += v
    total = density.sum()
    if total > 0:
        density = density / total
    return prices, density.tolist()


def cyq_chip_distribution(df: pd.DataFrame, window: int = 210, bins: int = 150,
                          ) -> tuple[list[float], list[float]]:
    """InStock 筹码分布：三角分布 + 换手率衰减（turnover 列）。

    无 turnover 列时回退到 volume_profile（加密/港股）。
    """
    sub = df.tail(window)
    if len(sub) == 0:
        return [], []
    if "turnover" not in sub.columns:
        return volume_profile(df, window, bins)

    lo = float(sub["low"].min())
    hi = float(sub["high"].max())
    acc = max(0.01, (hi - lo) / (bins - 1))
    prices = [lo + acc * i for i in range(bins)]
    xdata = np.zeros(bins, dtype=float)

    open_ = sub["open"].astype(float).to_numpy()
    close = sub["close"].astype(float).to_numpy()
    high = sub["high"].astype(float).to_numpy()
    low = sub["low"].astype(float).to_numpy()
    turnover = sub["turnover"].astype(float).to_numpy()

    for i in range(len(sub)):
        avg = (open_[i] + close[i] + high[i] + low[i]) / 4
        rate = min(1.0, turnover[i] / 100.0)
        xdata *= (1 - rate)
        H = int((high[i] - lo) / acc)
        L = int((low[i] - lo) / acc + 0.99)
        G = (bins - 1) if high[i] == low[i] else 2.0 / (high[i] - low[i])
        if high[i] == low[i]:  # 一字板
            j = int((avg - lo) / acc)
            j = max(0, min(j, bins - 1))
            xdata[j] += G * rate / 2
        else:
            for j in range(max(0, L), min(H, bins - 1) + 1):
                cur = lo + acc * j
                if cur <= avg:
                    if abs(avg - low[i]) < 1e-8:
                        xdata[j] += G * rate
                    else:
                        xdata[j] += (cur - low[i]) / (avg - low[i]) * G * rate
                else:
                    if abs(high[i] - avg) < 1e-8:
                        xdata[j] += G * rate
                    else:
                        xdata[j] += (high[i] - cur) / (high[i] - avg) * G * rate
    total = xdata.sum()
    if total > 0:
        xdata = xdata / total
    return prices, xdata.tolist()


def chip_density_at(prices: list[float], density: list[float],
                    price: float, tol: float) -> float:
    """某价位 ±tol 范围内的筹码/成交量占比（0-1）。"""
    if not prices or len(prices) != len(density):
        return 0.0
    total = sum(density)
    if total <= 0:
        return 0.0
    nearby = sum(d for p, d in zip(prices, density) if abs(p - price) <= tol)
    return float(nearby / total)
