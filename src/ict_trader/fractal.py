"""Fractal Model（分形模型 C1-C4）+ Reclaim Strength（回抽强度）—— 从 lemontree-0/indicators 吸收。

C1/C2/C3/C4 = 连续 4 根 bar：
  C1 = 参考 bar（被扫的极值）
  C2 = 扫单回抽 bar（下穿 C1 low 收回 / 上穿 C1 high 收回）
  C3/C4 = 后续跟随

RS（Reclaim Strength，回抽强度）：
  bullish: (C2 close - C1 low) / C1 range × 100   —— 收回越深越强
  bearish: (C1 high - C2 close) / C1 range × 100
  这是「扫单质量」的量化：比「扫了没扫」多一个强度维度。

Survival（生存率）：C3/C4 是否守住 C2 极值（Reach C4 / Full C4），
是「回抽强度 → 存活概率」的统计优势，可接进 Time Machine 做诚实胜率。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

__all__ = ["detect_fractal_reclaim", "reclaim_stats"]

RS_BUCKETS = [(0, 10), (10, 25), (25, 50), (50, 75), (75, 100), (100, float("inf"))]


def _bucket(rs: float) -> str:
    for lo, hi in RS_BUCKETS:
        if lo <= rs < hi:
            return f"[{lo},{int(hi) if hi != float('inf') else '∞'})"
    return "[100,∞)"


def detect_fractal_reclaim(df: pd.DataFrame, tail: int | None = None) -> list[dict]:
    """检测 C1-C4 分形扫单回抽 setup，返回每个 setup 的 RS / sweep / 生存状态。

    非重绘：只在 C2 close 后确认，C3/C4 用已收盘 bar。
    """
    if len(df) < 4:
        return []
    sub = df.tail(tail) if tail else df
    sub = sub.reset_index(drop=True)
    high = sub["high"].astype(float).to_numpy()
    low = sub["low"].astype(float).to_numpy()
    close = sub["close"].astype(float).to_numpy()
    out: list[dict] = []

    for i in range(3, len(sub)):
        c1h, c1l = high[i - 3], low[i - 3]
        c2h, c2l, c2c = high[i - 2], low[i - 2], close[i - 2]
        c1_range = c1h - c1l
        if c1_range <= 0:
            continue

        direction = None
        rs = 0.0
        if c2l < c1l and c2c > c1l:      # 看涨：下扫 + 收回
            direction = "bull"
            rs = (c2c - c1l) / c1_range * 100
        elif c2h > c1h and c2c < c1h:    # 看跌：上扫 + 收回
            direction = "bear"
            rs = (c1h - c2c) / c1_range * 100
        else:
            continue

        c2_range = c2h - c2l
        sweep = (abs(c2c - (c1l if direction == "bull" else c1h)) / c2_range
                 if c2_range > 0 else 0.0)

        # 生存：C3/C4 是否守住 C2 极值
        # bull 的 C2 极值 = C2 low；bear 的 C2 极值 = C2 high
        c2_ext = c2l if direction == "bull" else c2h
        c3_breach = (low[i - 1] < c2_ext) if direction == "bull" else (high[i - 1] > c2_ext)
        c4_breach = (low[i] < c2_ext) if direction == "bull" else (high[i] > c2_ext)

        out.append({
            "c1_idx": int(i - 3), "c2_idx": int(i - 2), "c4_idx": int(i),
            "direction": direction,
            "rs": round(rs, 2),
            "sweep": round(sweep, 3),
            "reach_c4": bool(not c3_breach),   # C3 内守住 → 活到 C4
            "full_c4": bool(not c3_breach and not c4_breach),  # C3+C4 都守住
        })
    return out


def reclaim_stats(df: pd.DataFrame, tail: int | None = None) -> dict:
    """按 RS 分桶统计生存率（Reach C4 / Full C4 / XC2）。"""
    setups = detect_fractal_reclaim(df, tail=tail)
    if not setups:
        return {"n": 0, "buckets": {}, "base": {}}

    buckets: dict[str, dict] = {}
    for lo, hi in RS_BUCKETS:
        bucket = [s for s in setups if lo <= s["rs"] < hi]
        n = len(bucket)
        if n == 0:
            buckets[_bucket(lo)] = {"n": 0}
            continue
        reach = sum(1 for s in bucket if s["reach_c4"]) / n * 100
        full = sum(1 for s in bucket if s["full_c4"]) / n * 100
        buckets[_bucket(lo)] = {"n": n, "reach_c4_pct": round(reach, 1),
                                "full_c4_pct": round(full, 1)}

    n = len(setups)
    base_reach = sum(1 for s in setups if s["reach_c4"]) / n * 100
    base_full = sum(1 for s in setups if s["full_c4"]) / n * 100

    # Reach Δ：每桶 reach - base reach（百分点）
    for name, b in buckets.items():
        if b.get("n", 0) > 0:
            b["reach_delta_pp"] = round(b["reach_c4_pct"] - base_reach, 1)

    return {
        "n": n,
        "buckets": buckets,
        "base": {"reach_c4_pct": round(base_reach, 1), "full_c4_pct": round(base_full, 1)},
    }
