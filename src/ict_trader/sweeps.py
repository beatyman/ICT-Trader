"""Liquidity sweeps and key levels.

A sweep is a wick through a known level followed by a close back through it:

  BSL/PDH/... : high > level and close < level  -> bearish sweep
  SSL/PDL/... : low  < level and close > level  -> bullish sweep

A close through the level without reclaim is a *break*, not a sweep.

Levels are only eligible after their confirmation time.  This avoids using a
swing/session level before it was actually known.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Literal

import numpy as np
import pandas as pd

Direction = Literal["bull", "bear"]
Kind = Literal["H", "L"]


@dataclass
class KeyLevel:
    name: str
    price: float
    kind: Kind
    known_at: int
    source: str = "swing"
    swept: bool = False
    broken: bool = False
    sweep_bar: int | None = None
    touches: int = 1


@dataclass
class LiquiditySweep:
    direction: Direction
    name: str
    level: float
    kind: Kind
    sweep_bar: int
    reclaim_bar: int
    sweep_price: float
    reclaim_close: float
    penetration_atr: float = 0.0
    volume_ratio: float = 0.0
    strength: int = 1


def detect_key_levels(
    df: pd.DataFrame,
    swings: Iterable,
    sessions: Iterable = (),
    liquidity: Iterable = (),
    tol_atr: float = 0.15,
) -> list[KeyLevel]:
    """Collect swing, session and liquidity-pool levels with dedupe."""
    atr = _atr(df)
    tol = max(float(np.nanmedian(atr)) * tol_atr, 1e-9)
    levels: list[KeyLevel] = []

    for s in swings:
        levels.append(KeyLevel(
            name="BSL" if s.kind == "H" else "SSL",
            price=float(s.price), kind=s.kind,
            known_at=int(s.confirmed_at), source="swing",
        ))
    for s in sessions:
        levels.append(KeyLevel(
            name=s.name, price=float(s.price), kind=s.kind,
            known_at=int(s.end), source="session",
        ))
    for l in liquidity:
        levels.append(KeyLevel(
            name="BSL" if l.direction == "buyside" else "SSL",
            price=float(l.top if l.direction == "buyside" else l.bottom),
            kind="H" if l.direction == "buyside" else "L",
            known_at=int(getattr(l, "right", l.created)), source="pool",
        ))

    # merge levels of the same side within tolerance; keep the earliest known one
    levels.sort(key=lambda x: (x.kind, x.price, x.known_at))
    merged: list[KeyLevel] = []
    for lv in levels:
        hit = None
        for m in merged:
            if m.kind == lv.kind and abs(m.price - lv.price) <= tol:
                hit = m
                break
        if hit is None:
            merged.append(lv)
        else:
            hit.touches += 1
            if lv.known_at < hit.known_at:
                hit.known_at = lv.known_at
    return merged


def _atr(df: pd.DataFrame, n: int = 14) -> np.ndarray:
    close = df["close"].to_numpy(float)
    h = df["high"].to_numpy(float)
    l = df["low"].to_numpy(float)
    tr = np.maximum.reduce([h - l, np.abs(h - np.r_[np.nan, close[:-1]]), np.abs(l - np.r_[np.nan, close[:-1]])])
    return pd.Series(tr).rolling(n, min_periods=1).mean().to_numpy()


def detect_sweeps(
    df: pd.DataFrame,
    levels: Iterable[KeyLevel],
    min_penetration_atr: float = 0.0,
    volume_mult: float = 1.2,
) -> list[LiquiditySweep]:
    atr = _atr(df)
    volume = df["volume"].to_numpy(float) if "volume" in df else np.zeros(len(df))
    vol_sma = pd.Series(volume).rolling(20, min_periods=1).mean().to_numpy()
    high = df["high"].to_numpy(float)
    low = df["low"].to_numpy(float)
    close = df["close"].to_numpy(float)
    out: list[LiquiditySweep] = []
    for lv in levels:
        for t in range(max(lv.known_at, 1), len(df)):
            if lv.swept or lv.broken:
                break
            a = atr[t] if not np.isnan(atr[t]) and atr[t] > 0 else 1e-9
            if lv.kind == "H":
                if high[t] > lv.price:
                    if close[t] < lv.price:
                        pen = (high[t] - lv.price) / a
                        if pen >= min_penetration_atr:
                            vr = volume[t] / vol_sma[t] if vol_sma[t] > 0 else 0.0
                            lv.swept = True
                            lv.sweep_bar = t
                            out.append(LiquiditySweep(
                                direction="bear", name=lv.name, level=lv.price, kind="H",
                                sweep_bar=t, reclaim_bar=t,
                                sweep_price=float(high[t]), reclaim_close=float(close[t]),
                                penetration_atr=float(pen), volume_ratio=float(vr),
                                strength=1 + int(vr >= volume_mult) + int(pen >= 0.5),
                            ))
                    else:
                        lv.broken = True
                    break
            else:
                if low[t] < lv.price:
                    if close[t] > lv.price:
                        pen = (lv.price - low[t]) / a
                        if pen >= min_penetration_atr:
                            vr = volume[t] / vol_sma[t] if vol_sma[t] > 0 else 0.0
                            lv.swept = True
                            lv.sweep_bar = t
                            out.append(LiquiditySweep(
                                direction="bull", name=lv.name, level=lv.price, kind="L",
                                sweep_bar=t, reclaim_bar=t,
                                sweep_price=float(low[t]), reclaim_close=float(close[t]),
                                penetration_atr=float(pen), volume_ratio=float(vr),
                                strength=1 + int(vr >= volume_mult) + int(pen >= 0.5),
                            ))
                    else:
                        lv.broken = True
                    break
    out.sort(key=lambda s: s.reclaim_bar)
    # collapse duplicate levels hit by the same bar
    uniq: dict[tuple[int, str, str], LiquiditySweep] = {}
    for s in out:
        key = (s.reclaim_bar, s.direction, s.name)
        if key not in uniq or s.strength > uniq[key].strength:
            uniq[key] = s
    return sorted(uniq.values(), key=lambda s: s.reclaim_bar)


def nearest_key_target(levels: Iterable[KeyLevel], price: float, bull: bool) -> float | None:
    vals = [lv.price for lv in levels if not lv.swept and not lv.broken
            and (lv.price > price if bull else lv.price < price)]
    if not vals:
        return None
    return min(vals) if bull else max(vals)

