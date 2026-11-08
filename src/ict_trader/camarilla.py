"""Camarilla 枢轴 + VWAP —— 从 Camarilla++ 吸收。

补上辅助层「经典日内 S/R 层级 + 机构均价」：
  · camarilla_pivots：H1-H6 / L1-L6 / Pivot / BC / TC
    （H3/H4/L3/L4 = 反转区，H5/L5 = 极值，H6/L6 = 终极极值）
  · vwap：成交量加权均价（机构锚点）
"""
from __future__ import annotations

import numpy as np
import pandas as pd

__all__ = ["camarilla_pivots", "vwap"]


def camarilla_pivots(high: float, low: float, close: float) -> dict[str, float]:
    """经典 Camarilla 方程。输入单根 bar 的 high/low/close，返回 13 个层级。"""
    rng = high - low
    h5 = (high / low) * close if low > 0 else close
    h4 = close + rng * 1.1 / 2
    h3 = close + rng * 1.1 / 4
    h2 = close + rng * 1.1 / 6
    h1 = close + rng * 1.1 / 12
    h6 = h5 + 1.168 * (h5 - h4)

    l1 = close - rng * 1.1 / 12
    l2 = close - rng * 1.1 / 6
    l3 = close - rng * 1.1 / 4
    l4 = close - rng * 1.1 / 2
    l5 = close - (h5 - close)
    l6 = close - (h6 - close)

    pivot = (high + low + close) / 3.0
    bc = (high + low) / 2.0
    tc = pivot - bc + pivot   # = 2*pivot - bc

    return {
        "H6": round(h6, 6), "H5": round(h5, 6), "H4": round(h4, 6),
        "H3": round(h3, 6), "H2": round(h2, 6), "H1": round(h1, 6),
        "L1": round(l1, 6), "L2": round(l2, 6), "L3": round(l3, 6),
        "L4": round(l4, 6), "L5": round(l5, 6), "L6": round(l6, 6),
        "pivot": round(pivot, 6), "bc": round(bc, 6), "tc": round(tc, 6),
    }


def vwap(df: pd.DataFrame) -> pd.Series:
    """成交量加权均价（VWAP）。典型价格 = (high+low+close)/3。"""
    typical = (df["high"] + df["low"] + df["close"]) / 3.0
    vol = df["volume"].astype(float)
    vol = vol.replace(0, np.nan)
    cum_pv = (typical * vol).cumsum()
    cum_v = vol.cumsum()
    out = cum_pv / cum_v
    return out.fillna(typical)
