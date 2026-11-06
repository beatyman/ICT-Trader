"""补足两个参考脚本（LuxAlgo ICT Concepts + CISD Model+）的策略缺口。

缺口 1：SMT 跨品种背离的自动配对（resolve_pair，来自 CISD Model+）。
缺口 2：BPR（Balance Price Range，均衡区，来自 LuxAlgo ICT Concepts）。
缺口 3：VI（Volume Imbalance，量能不平衡，来自 LuxAlgo ICT Concepts）。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

__all__ = ["resolve_pair", "detect_bpr", "detect_vimbalances"]


# CISD Model+ resolvePair() 的自动配对映射（crypto + 主要期货/外汇）
_PAIR_MAP = {
    "BTCUSDT": "ETHUSDT", "ETHUSDT": "BTCUSDT",
    "BTCUSD": "ETHUSD", "ETHUSD": "BTCUSD",
    "BTC": "ETH", "ETH": "BTC",
    "ES": "NQ", "NQ": "ES", "YM": "ES", "RTY": "ES",
    "GC": "SI", "SI": "GC", "CL": "RB", "RB": "CL",
    "6E": "6B", "6B": "6E", "6A": "6N", "6N": "6A",
    "XAUUSD": "XAGUSD", "XAGUSD": "XAUUSD",
    "EURUSD": "GBPUSD", "GBPUSD": "EURUSD",
    "AUDUSD": "NZDUSD", "NZDUSD": "AUDUSD",
    "USDJPY": "DXY", "USDCHF": "DXY", "USDCAD": "DXY", "DXY": "USDJPY",
    "EURJPY": "GBPJPY", "GBPJPY": "EURJPY",
    "AUDJPY": "NZDJPY", "NZDJPY": "AUDJPY",
}


def resolve_pair(symbol: str) -> str:
    """自动解析相关品种（SMT 用）。未知品种返回空串。

    归一化：去掉交易所前缀、去掉期货 ``1!`` 后缀、转大写。
    """
    s = (symbol or "").upper()
    # 去掉前缀（如 BINANCE:BTCUSDT / CME_MINI:ES1!）
    if ":" in s:
        s = s.split(":")[-1]
    # 去掉期货连续合约后缀 1!
    if s.endswith("1!"):
        s = s[:-2]
    return _PAIR_MAP.get(s, "")


def detect_bpr(fvgs, price: float | None = None) -> tuple[float, float] | None:
    """BPR（Balance Price Range）：最新 bull FVG 与最新 bear FVG 的重叠区。

    这是「均衡区」，等于 OTE 高质量回踩区。返回 (bottom, top) 或 None。
    ``fvgs`` 为 ICTResult.fvgs（有 .direction / .top / .bottom / .active）。
    """
    bulls = [f for f in fvgs if getattr(f, "direction", None) == "bull" and getattr(f, "active", True)]
    bears = [f for f in fvgs if getattr(f, "direction", None) == "bear" and getattr(f, "active", True)]
    if not bulls or not bears:
        return None
    b = max(bulls, key=lambda f: getattr(f, "created", 0))
    s = max(bears, key=lambda f: getattr(f, "created", 0))
    top = min(float(b.top), float(s.top))
    bottom = max(float(b.bottom), float(s.bottom))
    if bottom >= top:
        return None
    # 当前价必须在 BPR 之外才构成有效回踩区（在区内说明已进入均衡）
    if price is not None and bottom <= price <= top:
        return None
    return (round(bottom, 6), round(top, 6))


def detect_vimbalances(df: pd.DataFrame, vol_mult: float = 1.2) -> pd.DataFrame:
    """VI（Volume Imbalance）：量能不平衡（缺口 + 放量）。

    返回与 df 对齐的 DataFrame，含 vim_bull / vim_bear 布尔列。
    无未来函数：只用当前及过去 bar。
    """
    n = len(df)
    open_ = df["open"].astype(float).to_numpy()
    high = df["high"].astype(float).to_numpy()
    low = df["low"].astype(float).to_numpy()
    close = df["close"].astype(float).to_numpy()
    vol = df["volume"].astype(float).to_numpy() if "volume" in df else np.ones(n)
    vol_sma = pd.Series(vol).rolling(20, min_periods=1).mean().to_numpy()

    vim_bull = np.zeros(n, dtype=bool)
    vim_bear = np.zeros(n, dtype=bool)
    for i in range(1, n):
        mx = max(close[i], open_[i])
        mn = min(close[i], open_[i])
        vol_ok = vol[i] >= vol_sma[i] * vol_mult
        # LuxAlgo vImb_Bl: 开盘 > 前收 且 前高 > 现低 且 现收 > 前收 且 现开 > 前开 且 前高 < 现低(mn)
        bull = (open_[i] > close[i - 1] and high[i - 1] > low[i]
                and close[i] > close[i - 1] and open_[i] > open_[i - 1]
                and high[i - 1] < mn and vol_ok)
        # vImb_Br: 开盘 < 前收 且 前低 < 现高 且 现收 < 前收 且 现开 < 前开 且 前低 > 现高(mx)
        bear = (open_[i] < close[i - 1] and low[i - 1] < high[i]
                and close[i] < close[i - 1] and open_[i] < open_[i - 1]
                and low[i - 1] > mx and vol_ok)
        vim_bull[i] = bool(bull)
        vim_bear[i] = bool(bear)
    return pd.DataFrame({"vim_bull": vim_bull, "vim_bear": vim_bear}, index=df.index)
