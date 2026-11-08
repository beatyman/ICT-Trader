"""Wyckoff 质量评分 + 量价 effort ratio —— 从 WillyAlgoTrader Wyckoff Range Engine 吸收。

给 wyckoff.py 的状态机补上「努力与结果」的量化维度：
  · effort_ratio：bar effort（volume 或 bar range）/ 基线均值（1.0 = 正常）
  · climax_score：高潮质量 0-100（30% 平均努力 + 30% 峰值努力 + 20% 腿长 + 20% 速度）
  · spring_score：Spring/UTAD 质量 0-100（40% 深度 + 40% 努力 + 20% 速度）
  · sos_score：SOS/SOW 质量 0-100（40% 腿努力 + 30% 腿长 + 30% 收盘位置）
"""
from __future__ import annotations

import numpy as np
import pandas as pd

__all__ = ["effort_ratio", "climax_score", "spring_score", "sos_score"]


def _clamp01(x: float) -> float:
    return float(max(0.0, min(1.0, x)))


def effort_ratio(df: pd.DataFrame, baseline_len: int = 50) -> pd.Series:
    """量价 effort ratio：bar effort / 基线均值（1.0 = 正常）。

    effort 源：有 volume 用 volume，否则用 bar range（high-low）。
    """
    use_vol = "volume" in df.columns and df["volume"].notna().all()
    eff = df["volume"].astype(float) if use_vol else (df["high"] - df["low"]).astype(float)
    base = eff.rolling(baseline_len, min_periods=1).mean()
    out = (eff / base.replace(0, np.nan)).fillna(1.0)
    return out


def climax_score(run_eff: np.ndarray, run_atr: float, run_bars: int) -> int:
    """高潮（SC/BC）质量 0-100。

    30% 平均努力 + 30% 峰值努力 + 20% 腿长(ATR) + 20% 速度(ATR/bar)。
    """
    if len(run_eff) == 0:
        return 0
    mean_eff = float(np.mean(run_eff))
    peak_eff = float(np.max(run_eff))
    eff_c = _clamp01(mean_eff - 1.0)
    peak_c = _clamp01((peak_eff - 1.0) / 3.0)
    ext_c = _clamp01((run_atr - 2.0) / 6.0)
    vel_c = _clamp01((run_atr / max(run_bars, 1)) / 0.5)
    return int(round(30.0 * eff_c + 30.0 * peak_c + 20.0 * ext_c + 20.0 * vel_c))


def spring_score(depth_atr: float, spring_eff: float, climax_eff: float,
                 bars_since: int) -> int:
    """Spring/UTAD 质量 0-100。40% 深度 + 40% 努力 + 20% 速度。"""
    depth_c = _clamp01(1.0 - depth_atr / 1.5)
    eff_c = _clamp01(1.0 - spring_eff / climax_eff if climax_eff > 0 else 1.0)
    speed_c = _clamp01(1.0 - bars_since / 10.0)
    return int(round(40.0 * depth_c + 40.0 * eff_c + 20.0 * speed_c))


def sos_score(leg_eff: float, leg_atr: float, close_pos: float) -> int:
    """SOS/SOW 质量 0-100。40% 腿努力 + 30% 腿长 + 30% 收盘位置。"""
    eff_c = _clamp01((leg_eff - 1.0) / 1.5)
    len_c = _clamp01(leg_atr / 3.0)
    pos_c = _clamp01(close_pos)
    return int(round(40.0 * eff_c + 30.0 * len_c + 30.0 * pos_c))
