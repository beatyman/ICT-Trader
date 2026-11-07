"""缠论（缠中说禅）分析层，基于 czsc 库，叠加在 ICT 之上。

用 czsc 做「结构识别」（K线去包含 → 分型 → 笔 → 中枢，这是 czsc 的强项、
自己写不完整），我们在这之上派生「买卖点 + 方向 + 中枢位置」的缠论层：

  * 笔（BI）       —— 相邻顶底分型之间的一段走势
  * 中枢（ZS）     —— 至少 3 笔重叠的盘整区（zd 下沿 / zg 上沿）
  * 三买/三卖     —— 突破中枢后回踩不进中枢（趋势延续）
  * 一买/一卖     —— 趋势末端背驰（MACD 背离）反转

输出（只做辅助方向/点位判定，不单独发入场）：
  bias: long / short / wait
  position: above / inside / below 中枢
  last_zs: (zd, zg)

注意：czsc 1.0.2 已移除 signals.cxt 信号函数，买卖点由结构 + MACD 背驰派生。
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

__all__ = ["ChanlunResult", "analyze_chanlun", "df_to_bars", "FREQ_MAP"]

FREQ_MAP = {"1m": "F1", "5m": "F5", "15m": "F15", "30m": "F30",
            "1h": "F60", "4h": "F240", "1d": "D", "1w": "W"}


@dataclass
class ChanlunResult:
    bi_count: int = 0
    zs_count: int = 0
    last_bi_direction: str = "none"        # up / down / none
    last_bi_high: float = float("nan")
    last_bi_low: float = float("nan")
    last_zs_zd: float | None = None        # 中枢下沿
    last_zs_zg: float | None = None        # 中枢上沿
    price: float = float("nan")
    position: str = "none"                 # above / inside / below / none
    bias: str = "wait"                     # long / short / wait
    divergence: str = "none"               # bull(一买) / bear(一卖) / none（MACD 背驰）
    reasons: list = field(default_factory=list)


def df_to_bars(df: pd.DataFrame, symbol: str = "SYM", freq: str = "1d") -> list:
    """OHLCV DataFrame -> czsc RawBar 列表（按时间正序）。"""
    from czsc import RawBar, Freq
    f = getattr(Freq, FREQ_MAP.get(freq, "D"))
    bars = []
    df = df.sort_values("date").reset_index(drop=True)
    for i, r in df.iterrows():
        dt = pd.Timestamp(r["date"]).to_pydatetime()
        bars.append(RawBar(
            symbol=symbol, dt=dt, freq=f,
            open=float(r["open"]), close=float(r["close"]),
            high=float(r["high"]), low=float(r["low"]),
            vol=float(r.get("volume", r.get("vol", 0.0))),
            amount=float(r.get("amount", 0.0)), id=i,
        ))
    return bars


def _macd_divergence(df: pd.DataFrame) -> str:
    """MACD 背驰：价格创新低但 MACD 更高（一买）/ 价格创新高但 MACD 更低（一卖）。"""
    if len(df) < 60:
        return "none"
    close = df["close"].astype(float)
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    dif = ema12 - ema26
    dea = dif.ewm(span=9, adjust=False).mean()
    hist = dif - dea
    # 最近两个「段」的极值对比（简化：最近 20 根 vs 前 20 根）
    c = close.to_numpy()
    h = hist.to_numpy()
    seg = 20
    if len(c) < seg * 2:
        return "none"
    # 一买：价格创新低，MACD 柱不创新低（背驰）
    if c[-seg:].min() <= c[-2 * seg:-seg].min() and h[-seg:].min() > h[-2 * seg:-seg].min():
        return "bull"
    # 一卖：价格创新高，MACD 柱不创新高（背驰）
    if c[-seg:].max() >= c[-2 * seg:-seg].max() and h[-seg:].max() < h[-2 * seg:-seg].max():
        return "bear"
    return "none"


def analyze_chanlun(df: pd.DataFrame, symbol: str = "SYM", freq: str = "1d") -> ChanlunResult:
    """跑 czsc，派生缠论结构 + 方向 + 中枢位置 + 买卖点。"""
    res = ChanlunResult()
    if len(df) < 30:
        res.reasons.append("insufficient bars (<30)")
        return res
    from czsc import CZSC
    bars = df_to_bars(df, symbol, freq)
    c = CZSC(bars)

    res.bi_count = len(c.bi_list)
    res.zs_count = len(c.zs_list)
    res.price = float(df["close"].iloc[-1])

    # 最后一笔方向
    if c.bi_list:
        last = c.bi_list[-1]
        res.last_bi_direction = "up" if str(last.direction).lower().startswith("up") else "down"
        res.last_bi_high = float(last.high)
        res.last_bi_low = float(last.low)

    # 最近有效中枢
    if c.zs_list:
        zs = c.zs_list[-1]
        if getattr(zs, "is_valid", True):
            res.last_zs_zd = float(zs.zd)
            res.last_zs_zg = float(zs.zg)
        else:
            res.reasons.append("last zhongshu invalid")

    # 位置：中枢上/内/下
    if res.last_zs_zd is not None and res.last_zs_zg is not None:
        if res.price > res.last_zs_zg:
            res.position = "above"
        elif res.price < res.last_zs_zd:
            res.position = "below"
        else:
            res.position = "inside"
    else:
        res.position = "none"

    # MACD 背驰（一买/一卖）
    res.divergence = _macd_divergence(df)

    # 方向（缠论层）：中枢位置 + 笔方向 + 背驰
    if res.divergence == "bull":
        res.bias = "long"
        res.reasons.append("MACD bull divergence (一买)")
    elif res.divergence == "bear":
        res.bias = "short"
        res.reasons.append("MACD bear divergence (一卖)")
    elif res.position == "inside":
        res.bias = "wait"
        res.reasons.append("price inside zhongshu, wait")
    elif res.position == "above" and res.last_bi_direction == "up":
        res.bias = "long"
        res.reasons.append("above zhongshu + up bi (三买)")
    elif res.position == "below" and res.last_bi_direction == "down":
        res.bias = "short"
        res.reasons.append("below zhongshu + down bi (三卖)")
    else:
        res.bias = "long" if res.last_bi_direction == "up" else "short"
        res.reasons.append(f"follow last bi {res.last_bi_direction}")
    return res
