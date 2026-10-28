"""Signal-quality measurements shared by the backtest and the live setup.

The weights are deliberately simple and transparent:
  HTF bias, MTF alignment, CISD grade, sweep, PD array, displacement,
  volume, session.

They are inspired by the weighted SMC scoring used in tb_smc/mt5_smc, but are
not copied from any repository.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass
class Displacement:
    body_ratio: float      # body / range
    body_atr: float        # body / ATR
    body_multiple: float   # body / average body
    volume_ratio: float
    score: float           # 0..1


def measure_displacement(df: pd.DataFrame, idx: int, lookback: int = 20, atr_len: int = 14) -> Displacement:
    o = float(df["open"].iloc[idx])
    c = float(df["close"].iloc[idx])
    h = float(df["high"].iloc[idx])
    l = float(df["low"].iloc[idx])
    body = abs(c - o)
    rng = max(h - l, 1e-12)
    body_ratio = body / rng
    prev = df.iloc[max(0, idx - lookback):idx]
    avg_body = float((prev["close"] - prev["open"]).abs().mean()) if len(prev) else body
    body_multiple = body / max(avg_body, 1e-12)
    tr = pd.Series(np.maximum.reduce([
        df["high"].to_numpy(float) - df["low"].to_numpy(float),
        np.abs(df["high"].to_numpy(float) - df["close"].shift(1).to_numpy(float)),
        np.abs(df["low"].to_numpy(float) - df["close"].shift(1).to_numpy(float)),
    ]))
    atr = float(tr.rolling(atr_len, min_periods=1).mean().iloc[idx])
    body_atr = body / max(atr, 1e-12)
    vol = df["volume"].to_numpy(float) if "volume" in df else np.zeros(len(df))
    vol_avg = float(pd.Series(vol).iloc[max(0, idx - lookback):idx].mean()) if idx else 0.0
    volume_ratio = float(vol[idx] / vol_avg) if vol_avg > 0 else 0.0
    score = 0.0
    score += 0.4 * min(body_ratio / 0.65, 1.0)
    score += 0.3 * min(body_atr / 1.0, 1.0)
    score += 0.2 * min(body_multiple / 1.3, 1.0)
    score += 0.1 * min(volume_ratio / 1.2, 1.0)
    return Displacement(body_ratio, body_atr, body_multiple, volume_ratio, min(score, 1.0))


def score_setup(
    *,
    d1_score: int,
    h4_score: int,
    cisd_grade: int,
    sweep_strength: int,
    pda: bool,
    displacement: float,
    volume: bool,
    session: bool,
) -> tuple[float, dict[str, float]]:
    parts = {
        "htf": 0.15 * min(d1_score / 3, 1.0),
        "mtf": 0.10 * min(h4_score / 3, 1.0),
        "cisd": 0.20 * min(cisd_grade / 7, 1.0),
        "sweep": 0.10 * min(sweep_strength / 3, 1.0),
        "pda": 0.15 * (1.0 if pda else 0.0),
        "displacement": 0.15 * min(max(displacement, 0.0), 1.0),
        "volume": 0.10 * (1.0 if volume else 0.0),
        "session": 0.05 * (1.0 if session else 0.0),
    }
    return sum(parts.values()), parts

