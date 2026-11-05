"""一目均衡表（Ichimoku Kinko Hyo）指标计算库。

公式（精确）::

    MID(n)    = (n 根内最高 high + n 根内最低 low) / 2
    Tenkan    = MID(tenkan_period)            # 默认 9
    Kijun     = MID(kijun_period)             # 默认 26
    SenkouA   = (Tenkan + Kijun) / 2
    SenkouB   = MID(senkou_b_period)          # 默认 52

Chikou（迟行线）**不作为输出线**，仅用于信号：
确认 = close[t] 与 close[t - kijun_period] 比较（见 ``signals.py``）。

偏移（只做显示偏移，不参与计算，不改变数值）::

    先行带 A/B：向前 ``offset`` 根（数值写在计算它的 bar 上，
                显示在 offset 根之后）
    迟行线    ：向后 kijun_period 根（仅显示；不参与计算）

偏移模式::

    PLATFORM(26)   offset = kijun_period      （现代平台约定，默认）
    HOSODA(25)     offset = kijun_period - 1  （Hosoda 原版，云区早一根出现）

    lookback = max(tenkan_period, kijun_period, senkou_b_period) - 1

无未来函数：所有 rolling 窗口只看当前及过去；``shift(offset)`` 是纯显示偏移，
数值来源仍是 ``t - offset``（过去），不引用未来。
"""
from __future__ import annotations

import pandas as pd

__all__ = ["midpoint", "span_offset", "ichimoku", "OFFSET_MODES"]


OFFSET_MODES = ("PLATFORM", "HOSODA")


def midpoint(high: pd.Series, low: pd.Series, period: int) -> pd.Series:
    """MID(n) = (n 根最高 high + n 根最低 low) / 2。

    使用 ``rolling(period).max()/min()``，``min_periods=period`` 保证前
    ``period-1`` 根为 NaN（即 lookback 根预热）。
    """
    hh = high.rolling(period).max()
    ll = low.rolling(period).min()
    return (hh + ll) / 2.0


def span_offset(kijun_period: int, offset_mode: str = "PLATFORM") -> int:
    """先行带前移根数。

    PLATFORM: kijun_period（默认 26）；HOSODA: kijun_period - 1（默认 25）。
    """
    if offset_mode == "PLATFORM":
        return int(kijun_period)
    if offset_mode == "HOSODA":
        return int(kijun_period) - 1
    raise ValueError(f"unknown offset_mode {offset_mode!r} (use {OFFSET_MODES})")


def ichimoku(
    df: pd.DataFrame,
    tenkan_period: int = 9,
    kijun_period: int = 26,
    senkou_b_period: int = 52,
    offset_mode: str = "PLATFORM",
) -> pd.DataFrame:
    """计算 Ichimoku 指标，返回对齐 ``df.index`` 的 DataFrame。

    返回列::

        tenkan, kijun       转换线 / 基准线
        senkou_a, senkou_b  先行带 A / B 的「计算值」（未偏移）
        leading_a, leading_b 先行带 A / B 的「显示值」（前移 offset 根）

    ``leading_a[t] == senkou_a[t - offset]``；当前云区判定用
    ``leading_a[t] / leading_b[t]``（等价于 senkou_a[t - offset]），
    而不是 ``senkou_a[t - 2*offset]``。

    ``result.attrs["offset"]`` / ``["lookback"]`` 记录偏移量与预热根数。
    """
    high = df["high"].astype(float)
    low = df["low"].astype(float)

    tenkan = midpoint(high, low, tenkan_period)
    kijun = midpoint(high, low, kijun_period)
    senkou_a = (tenkan + kijun) / 2.0
    senkou_b = midpoint(high, low, senkou_b_period)

    offset = span_offset(kijun_period, offset_mode)
    # 向前显示偏移：leading_a[t] = senkou_a[t - offset]
    leading_a = senkou_a.shift(offset)
    leading_b = senkou_b.shift(offset)

    out = pd.DataFrame(
        {
            "tenkan": tenkan,
            "kijun": kijun,
            "senkou_a": senkou_a,
            "senkou_b": senkou_b,
            "leading_a": leading_a,
            "leading_b": leading_b,
        },
        index=df.index,
    )
    out.attrs["offset"] = offset
    out.attrs["lookback"] = max(tenkan_period, kijun_period, senkou_b_period) - 1
    out.attrs["offset_mode"] = offset_mode
    return out
