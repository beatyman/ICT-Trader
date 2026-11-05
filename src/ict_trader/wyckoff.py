"""威科夫（Wyckoff）吸筹 / 派发结构检测器。

依据 tradingwyckoff.com 的方法论（Ruben Villahermosa《The Wyckoff
Methodology in Depth》）与 LunqFX 状态机，在已收盘 K 线上逐个事件地构建
Wyckoff 示意图（非重绘，无未来函数）。

事件序列（吸筹 / 派发）::

    吸筹：PS → SC → AR → ST → Spring → SOS → LPS
    派发：PSY → BC → AR → ST → UT(UTAD) → SOW → LPSY

阶段 A–E::

    A  停止动作（SC/BC + AR 定出区间）
    B  构建原因（ST 次级测试）
    C  测试（Spring / UTAD）
    D  离开区间（SOS / SOW）
    E  延续（LPS / LPSY）

目标（因果法则）：T1 = 突破边 + 1×区间，T2 = +2×区间；
失效 = 收盘越过高潮边 failDep×区间 或 突破边反向。

三条法则：供需（Supply/Demand）、因果（Cause/Effect）、
努力与结果（Effort vs Result）。
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

__all__ = ["WyckoffState", "detect_wyckoff", "pivot_high", "pivot_low", "atr"]


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    h, l, c = df["high"], df["low"], df["close"]
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    return tr.rolling(period).mean()


def pivot_high(high: np.ndarray, left: int, right: int) -> np.ndarray:
    """摆动高点：high[i] 是 [i-left, i+right] 内的最大值，否则 NaN。"""
    n = len(high)
    out = np.full(n, np.nan)
    for i in range(left, n - right):
        if high[i] == np.max(high[i - left: i + right + 1]):
            out[i] = high[i]
    return out


def pivot_low(low: np.ndarray, left: int, right: int) -> np.ndarray:
    """摆动低点：low[i] 是 [i-left, i+right] 内的最小值，否则 NaN。"""
    n = len(low)
    out = np.full(n, np.nan)
    for i in range(left, n - right):
        if low[i] == np.min(low[i - left: i + right + 1]):
            out[i] = low[i]
    return out


@dataclass
class WyckoffState:
    stage: int = 0                # 0 搜索 · 1 A · 2 B · 3 C · 4 D · 5 E
    direction: int = 0            # 1 吸筹 · -1 派发
    phase: str = ""               # "A".."E"
    start_bar: int = -1
    climax_extreme: float = float("nan")
    climax_bar: int = -1
    ar_price: float = float("nan")
    ar_bar: int = -1
    range_top: float = float("nan")
    range_bottom: float = float("nan")
    range_mid: float = float("nan")
    range_height: float = float("nan")
    creek_ice: float = float("nan")       # 突破边（吸筹 creek / 派发 ice）
    climax_side: float = float("nan")     # 高潮边（吸筹支撑 / 派发阻力）
    st_confirmed: bool = False
    spring_confirmed: bool = False
    spring_extreme: float = float("nan")
    spring_bar: int = -1
    sos_bar: int = -1
    lps_confirmed: bool = False
    t1: float = float("nan")
    t2: float = float("nan")
    invalidation: float = float("nan")
    failed: bool = False
    completed: bool = False
    events: list = field(default_factory=list)   # (bar, price, label)


def _ema_warm(df: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
    """ATR 数组与 20 根均量（用于高潮判定）。"""
    a = atr(df, 14).to_numpy()
    vol = df["volume"].astype(float).to_numpy()
    avgv = pd.Series(vol).rolling(20).mean().to_numpy()
    return a, avgv


def detect_wyckoff(
    df: pd.DataFrame,
    trend_len: int = 50,
    climax_range_atr: float = 1.2,
    climax_vol_mult: float = 1.5,
    pivot_len: int = 3,
    ar_min_atr: float = 2.0,
    max_range_atr: float = 10.0,
    min_range_bars: int = 15,
    st_tol: float = 0.35,
    spring_max_bars: int = 3,
    fail_depth: float = 0.30,
    max_range_bars: int = 300,
    max_leg_bars: int = 300,
) -> tuple[WyckoffState, list[int]]:
    """检测 Wyckoff 结构，返回 (state, phases)。

    ``phases`` 是与 ``df`` 对齐的每根 bar 的阶段标签（0 无 / 1..5 对应 A..E）。
    所有判定只使用已收盘 bar，无未来函数。
    """
    n = len(df)
    high = df["high"].astype(float).to_numpy()
    low = df["low"].astype(float).to_numpy()
    close = df["close"].astype(float).to_numpy()
    open_ = df["open"].astype(float).to_numpy()
    a, avgv = _ema_warm(df)

    ph = pivot_high(high, pivot_len, pivot_len)
    pl = pivot_low(low, pivot_len, pivot_len)
    phases = np.zeros(n, dtype=int)

    st = WyckoffState()
    # 待确认的 spring/UTAD 渗透状态
    pend = False
    pend_n = 0
    pend_ext = float("nan")
    pend_ext_bar = -1
    hit_max = 0

    for i in range(n):
        age = i - st.start_bar
        d = st.direction
        early = age < min_range_bars
        # 高潮边与突破边（按方向）
        wick_climax = low[i] if d == 1 else high[i]
        fav = high[i] if d == 1 else low[i]

        # ---- D / E：目标、失效、LPS/LPSY ----
        if st.stage >= 4:
            if not np.isnan(st.t2) and (fav - st.t2) * d >= 0:
                hit_max = 2
            elif hit_max < 1 and (fav - st.t1) * d >= 0:
                hit_max = 1
            if hit_max == 2:
                st.completed = True
            elif not np.isnan(st.invalidation) and (st.invalidation - close[i]) * d > 0:
                st.failed = True
            elif i - st.sos_bar > max_leg_bars:
                st.failed = True
            if st.stage == 4 and not st.completed and not st.failed:
                pv = pl[i] if d == 1 else ph[i]
                if not np.isnan(pv) and (i - pivot_len) > st.sos_bar and (pv - st.range_mid) * d > 0:
                    st.stage = 5
                    st.lps_confirmed = True
                    st.events.append((i, pv, "LPS" if d == 1 else "LPSY"))
                    st.phase = "E"

        # ---- B / C：ST、Spring/UTAD、SOS/SOW ----
        elif st.stage in (2, 3):
            # ST（次级测试）
            if st.stage == 2 and not pend and not st.st_confirmed:
                pv = pl[i] if d == 1 else ph[i]
                if (not np.isnan(pv) and (i - pivot_len) > st.ar_bar
                        and 0 <= (pv - st.climax_side) * d <= st_tol * st.range_height):
                    st.st_confirmed = True
                    st.events.append((i - pivot_len, pv, "ST"))
                    st.phase = "B"
            # 突破边（SOS/SOW）
            if (close[i] - st.creek_ice) * d > 0:
                if early:
                    st.failed = True
                    st.events.append((i, close[i], "DROP"))
                else:
                    st.stage = 4
                    st.sos_bar = i
                    st.t1 = st.creek_ice + d * st.range_height
                    st.t2 = st.creek_ice + d * 2 * st.range_height
                    st.invalidation = st.spring_extreme if st.spring_confirmed else st.climax_side
                    st.events.append((i, fav, "SOS" if d == 1 else "SOW"))
                    st.phase = "D"
            elif st.stage == 2:
                # Spring / UTAD 渗透检测
                if pend:
                    pend_n += 1
                    if (pend_ext - wick_climax) * d > 0:
                        pend_ext = wick_climax
                        pend_ext_bar = i
                    if (close[i] - st.climax_side) * d >= 0:
                        pend = False
                        if not early:
                            st.spring_confirmed = True
                            st.spring_extreme = pend_ext
                            st.spring_bar = pend_ext_bar
                    elif (st.climax_side - close[i]) * d > fail_depth * st.range_height or pend_n > spring_max_bars:
                        if early:
                            st.failed = True
                        else:
                            st.failed = True
                elif (st.climax_side - wick_climax) * d > 0:
                    if (close[i] - st.climax_side) * d >= 0:
                        if not early:
                            st.spring_confirmed = True
                            st.spring_extreme = wick_climax
                            st.spring_bar = i
                    elif (st.climax_side - close[i]) * d > fail_depth * st.range_height:
                        st.failed = True
                    else:
                        pend = True
                        pend_n = 1
                        pend_ext = wick_climax
                        pend_ext_bar = i
                if st.spring_confirmed:
                    st.stage = 3
                    st.events.append((st.spring_bar, st.spring_extreme,
                                      "SPRING" if d == 1 else "UTAD"))
                    st.phase = "C"
                elif i - st.start_bar > max_range_bars and not st.failed:
                    st.failed = True
            else:  # stage 3，已 spring，等 SOS 或失效
                if (st.spring_extreme - close[i]) * d > 0:
                    st.failed = True
                elif i - st.spring_bar > max_range_bars:
                    st.failed = True

        # ---- A：高潮延续，直到 AR 形成 ----
        elif st.stage == 1:
            if (st.climax_extreme - wick_climax) * d > 0:
                st.climax_extreme = wick_climax
                st.climax_bar = i
            pv = ph[i] if d == 1 else pl[i]
            if (not np.isnan(pv) and (i - pivot_len) > st.climax_bar
                    and (pv - st.climax_extreme) * d >= ar_min_atr * a[i]):
                if abs(pv - st.climax_extreme) > max_range_atr * a[i]:
                    st.failed = True
                else:
                    st.stage = 2
                    st.ar_price = pv
                    st.ar_bar = i - pivot_len
                    st.range_top = max(st.climax_extreme, pv)
                    st.range_bottom = min(st.climax_extreme, pv)
                    st.range_mid = (st.range_top + st.range_bottom) / 2
                    st.range_height = st.range_top - st.range_bottom
                    st.climax_side = st.climax_extreme
                    st.creek_ice = pv
                    st.events.append((st.ar_bar, pv, "AR"))
                    st.phase = "A"
            elif i - st.climax_bar > trend_len:
                st.failed = True

        # ---- 0：搜索高潮 ----
        if st.stage == 0:
            rng_ok = (not np.isnan(a[i])) and (high[i] - low[i]) >= climax_range_atr * a[i]
            vol_ok = np.isnan(avgv[i]) or avgv[i] <= 0 or (df["volume"].iloc[i] >= climax_vol_mult * avgv[i])
            new_lo = low[i] <= np.min(low[max(0, i - trend_len + 1): i + 1])
            new_hi = high[i] >= np.max(high[max(0, i - trend_len + 1): i + 1])
            sc = new_lo and rng_ok and vol_ok
            bc = new_hi and rng_ok and vol_ok
            if sc or bc:
                st.direction = 1 if (sc and (not bc or close[i] < open_[i])) else -1
                st.stage = 1
                st.climax_extreme = low[i] if st.direction == 1 else high[i]
                st.climax_bar = i
                st.start_bar = i
                st.st_confirmed = False
                st.spring_confirmed = False
                st.lps_confirmed = False
                st.failed = False
                st.completed = False
                pend = False
                pend_n = 0
                hit_max = 0
                st.events = []  # 新周期：清空上一结构的事件
                st.events.append((i, st.climax_extreme, "SC" if st.direction == 1 else "BC"))

        # ---- 结束 / 重置 ----
        if st.failed and st.stage >= 2:
            st.events.append((i, close[i], "FAILED"))
            st.stage = 0
            st.direction = 0
            st.failed = False
            pend = False
            pend_n = 0

        # 记录阶段标签
        phases[i] = st.stage if st.direction != 0 else 0

    return st, phases.tolist()
