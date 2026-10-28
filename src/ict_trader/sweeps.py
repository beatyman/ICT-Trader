"""Liquidity sweeps and key levels.

A sweep is a wick through a known level followed by a close back through it:

  BSL/PDH/... : high > level and a close back below it  -> bearish sweep
  SSL/PDL/... : low  < level and a close back above it  -> bullish sweep

The reclaim may occur on the penetration candle or within ``reclaim_window``
bars.  A close through the level without a reclaim in that window is a
*break*, not a sweep.  Events are known at the reclaim bar; no future data is
used.

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
    reclaim_bars: int = 0


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
        equal = bool(getattr(l, "equal", False))
        base = "EQH" if l.direction == "buyside" else "EQL"
        name = base if equal else ("BSL" if l.direction == "buyside" else "SSL")
        levels.append(KeyLevel(
            name=name,
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
    reclaim_window: int = 3,
) -> list[LiquiditySweep]:
    """Detect wick-through-and-reclaim events.

    A sweep does not have to reclaim on the same candle.  The level is tested,
    price closes beyond it, and if a close back through the level arrives
    within ``reclaim_window`` bars, the whole event is a sweep.  If no reclaim
    arrives in that window, the level is marked broken and is not reused.

    All measurements use only bars up to the reclaim bar.  The event is
    therefore known at ``reclaim_bar`` and cannot leak future prices into a
    backtest.
    """
    atr = _atr(df)
    volume = df["volume"].to_numpy(float) if "volume" in df else np.zeros(len(df))
    vol_sma = pd.Series(volume).rolling(20, min_periods=1).mean().to_numpy()
    high = df["high"].to_numpy(float)
    low = df["low"].to_numpy(float)
    close = df["close"].to_numpy(float)
    n = len(df)
    window = max(int(reclaim_window), 0)
    out: list[LiquiditySweep] = []

    for lv in levels:
        t = max(lv.known_at, 1)
        while t < n and not lv.swept and not lv.broken:
            a = atr[t] if not np.isnan(atr[t]) and atr[t] > 0 else 1e-9
            if lv.kind == "H" and high[t] > lv.price:
                if close[t] < lv.price:
                    reclaim = t
                else:
                    reclaim = None
                    for k in range(t + 1, min(n, t + window + 1)):
                        if close[k] < lv.price:
                            reclaim = k
                            break

                if reclaim is None:
                    lv.broken = True
                    break

                extreme = float(np.max(high[t:reclaim + 1]))
                pen = (extreme - lv.price) / a
                if pen < min_penetration_atr:
                    # The wick was too shallow to qualify; keep watching the
                    # level rather than treating it as a sweep or a break.
                    t += 1
                    continue

                vr = max(
                    (volume[k] / vol_sma[k] if vol_sma[k] > 0 else 0.0)
                    for k in range(t, reclaim + 1)
                )
                rb = max(0, reclaim - t)
                lv.swept = True
                lv.sweep_bar = t
                out.append(LiquiditySweep(
                    direction="bear", name=lv.name, level=lv.price, kind="H",
                    sweep_bar=t, reclaim_bar=reclaim,
                    sweep_price=extreme, reclaim_close=float(close[reclaim]),
                    penetration_atr=float(pen), volume_ratio=float(vr),
                    strength=1 + int(vr >= volume_mult) + int(pen >= 0.5) + int(rb <= 1),
                    reclaim_bars=rb,
                ))
                break

            if lv.kind == "L" and low[t] < lv.price:
                if close[t] > lv.price:
                    reclaim = t
                else:
                    reclaim = None
                    for k in range(t + 1, min(n, t + window + 1)):
                        if close[k] > lv.price:
                            reclaim = k
                            break

                if reclaim is None:
                    lv.broken = True
                    break

                extreme = float(np.min(low[t:reclaim + 1]))
                pen = (lv.price - extreme) / a
                if pen < min_penetration_atr:
                    t += 1
                    continue

                vr = max(
                    (volume[k] / vol_sma[k] if vol_sma[k] > 0 else 0.0)
                    for k in range(t, reclaim + 1)
                )
                rb = max(0, reclaim - t)
                lv.swept = True
                lv.sweep_bar = t
                out.append(LiquiditySweep(
                    direction="bull", name=lv.name, level=lv.price, kind="L",
                    sweep_bar=t, reclaim_bar=reclaim,
                    sweep_price=extreme, reclaim_close=float(close[reclaim]),
                    penetration_atr=float(pen), volume_ratio=float(vr),
                    strength=1 + int(vr >= volume_mult) + int(pen >= 0.5) + int(rb <= 1),
                    reclaim_bars=rb,
                ))
                break

            t += 1

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

