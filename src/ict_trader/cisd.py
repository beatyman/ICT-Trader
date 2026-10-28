"""CISD (Change in State of Delivery) engine.

This is a Python re-implementation of the core CISD idea from the
"CISD Model+ | Triton Trades" Pine script:

  * track the current delivery leg with a Pine-like state machine
  * ``cm_legExtremeCurrent`` / ``cm_legExtremePrevious`` open extremes
  * a close back through the level is a CISD in the opposite direction
  * optional origin gate: liquidity sweep / FVG delivery
  * grade with sweep / PDA / volume / target checks
  * stop loss, target and invalidation

It is intentionally independent from ict_concepts so it can be tested alone;
ict_concepts passes swing/liquidity levels and FVG zones into it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Literal

import numpy as np
import pandas as pd

Direction = Literal["bull", "bear"]


@dataclass
class CISDConfig:
    enabled: bool = True
    min_grade: int = 3          # 3=C, 4=B-, 5=B+, 6=A, 7=A+
    max_per_side: int = 2
    extend_bars: int = 16
    sl_extend_bars: int = 4
    require_origin: bool = True
    proximity_atr: float = 0.75
    min_displacement_atr: float = 0.0
    sweep_window: int = 15
    sl_mode: Literal["wick", "body"] = "wick"
    volume_mult: float = 1.2
    min_rr: float = 1.5
    lookback: int = 500
    hide_invalid: bool = True


@dataclass
class CISDLeg:
    start: int
    end: int
    direction: Direction
    level: float
    extreme: float
    body_extreme: float
    pivot: int


@dataclass
class CISD:
    direction: Direction
    level: float
    extreme: float
    body_extreme: float
    start: int
    end: int
    sl: float
    target: float | None
    grade: int
    checks: dict[str, bool] = field(default_factory=dict)
    active: bool = True
    invalidated: bool = False
    label: str = "CISD"
    tf: str = "base"
    quality: float = 0.0
    quality_parts: dict = field(default_factory=dict)

    @property
    def rr(self) -> float:
        if self.target is None:
            return 0.0
        risk = abs(self.level - self.sl)
        return abs(self.target - self.level) / risk if risk > 0 else 0.0


def _legs(df: pd.DataFrame) -> list[CISDLeg]:
    close = df["close"].to_numpy(float)
    open_ = df["open"].to_numpy(float)
    high = df["high"].to_numpy(float)
    low = df["low"].to_numpy(float)
    n = len(df)
    if n == 0:
        return []
    # 0 = doji: carry the previous non-zero direction
    dirs = np.where(close > open_, 1, np.where(close < open_, -1, 0))
    last = 0
    for i in range(n):
        if dirs[i] == 0:
            dirs[i] = last
        else:
            last = dirs[i]

    legs: list[CISDLeg] = []
    start = 0
    for i in range(1, n + 1):
        if i == n or dirs[i] != dirs[start]:
            end = i - 1
            d: Direction = "bull" if dirs[start] > 0 else "bear"
            opens = open_[start:end + 1]
            if d == "bull":
                level = float(np.min(opens))
                extreme = float(np.min(low[start:end + 1]))
                pivot = start + int(np.argmin(low[start:end + 1]))
            else:
                level = float(np.max(opens))
                extreme = float(np.max(high[start:end + 1]))
                pivot = start + int(np.argmax(high[start:end + 1]))
            body_extreme = float(min(open_[pivot], close[pivot])) if d == "bull" else float(max(open_[pivot], close[pivot]))
            legs.append(CISDLeg(start, end, d, level, extreme, body_extreme, pivot))
            start = i
    return legs


def _swept(levels: Iterable[tuple], start: int, end: int,
           df: pd.DataFrame, bull: bool) -> bool:
    """True if a sell-side (bull) / buy-side (bear) level was swept and reclaimed.

    A level tuple may be (price, kind) or (price, kind, known_at).  The latter
    prevents using a swing/session level before it was confirmed.
    """
    high = df["high"].to_numpy(float)
    low = df["low"].to_numpy(float)
    close = df["close"].to_numpy(float)
    for item in levels:
        price, kind = float(item[0]), item[1]
        known = int(item[2]) if len(item) > 2 else 0
        for t in range(max(start, known), min(end + 1, len(df))):
            if bull and kind == "L" and low[t] < price and close[t] > price:
                return True
            if not bull and kind == "H" and high[t] > price and close[t] < price:
                return True
    return False


def _fvg_delivery(zones: Iterable[tuple[float, float, int, str]], start: int, end: int,
                  df: pd.DataFrame, bull: bool) -> bool:
    low = df["low"].to_numpy(float)
    high = df["high"].to_numpy(float)
    for top, bot, created, direction in zones:
        if created > end:
            continue
        if direction != ("bull" if bull else "bear"):
            continue
        for t in range(max(start, created), min(end + 1, len(df))):
            if low[t] <= top and high[t] >= bot:
                return True
    return False


def _nearest_target(levels: Iterable[tuple], level: float, bull: bool, t: int) -> float | None:
    vals = [
        float(item[0])
        for item in levels
        if (len(item) < 3 or int(item[2]) <= t)
        and (float(item[0]) > level if bull else float(item[0]) < level)
    ]
    if not vals:
        return None
    return min(vals) if bull else max(vals)


def _grade(enabled: dict[str, bool], hits: dict[str, bool]) -> int:
    n = sum(1 for k, v in enabled.items() if v)
    if n == 0:
        return 0
    h = sum(1 for k, v in enabled.items() if v and hits.get(k, False))
    ratio = h / n
    if ratio >= 0.95:
        return 7
    if ratio >= 0.80:
        return 6
    if ratio >= 0.65:
        return 5
    if ratio >= 0.45:
        return 4
    if ratio >= 0.25:
        return 3
    return 0


def _ctype(close: np.ndarray, open_: np.ndarray) -> np.ndarray:
    out = np.zeros(len(close), dtype=int)
    last = 0
    for i in range(len(close)):
        if close[i] > open_[i]:
            out[i] = 1
            last = 1
        elif close[i] < open_[i]:
            out[i] = -1
            last = -1
        else:
            out[i] = last
    return out


def _leg_extreme_current(open_: np.ndarray, ctype: np.ndarray, bias: int, t: int) -> tuple[float, int]:
    """Pine cm_legExtremeCurrent: open extreme of the current same-colour run."""
    extreme = float(open_[t])
    temp_shift = 0
    if ctype[t] != 0 and ctype[t] == bias:
        for i in range(1, min(500, t) + 1):
            ct = ctype[t - i]
            if ct == 0:
                continue
            if ct != bias:
                break
            temp_shift = i
            if bias == 1:
                extreme = min(extreme, float(open_[t - i]))
            else:
                extreme = max(extreme, float(open_[t - i]))
    extreme_shift = 0
    for k in range(0, temp_shift + 1):
        if abs(float(open_[t - k]) - extreme) < 1e-9:
            extreme_shift = k
            break
    return extreme, t - extreme_shift


def _leg_extreme_previous(open_: np.ndarray, ctype: np.ndarray, bias: int, t: int) -> tuple[float | None, int | None]:
    """Pine cm_legExtremePrevious: open extreme of the prior same-colour run."""
    found = False
    max_shift = 1
    extreme: float | None = None
    for j in range(1, min(500, t) + 1):
        ct = ctype[t - j]
        if ct == 0:
            continue
        matches = ct == bias
        if not found:
            if matches:
                found = True
                max_shift = j
                extreme = float(open_[t - j])
        else:
            if not matches:
                break
            max_shift = j
            if bias == 1:
                extreme = min(extreme, float(open_[t - j]))  # type: ignore[arg-type]
            else:
                extreme = max(extreme, float(open_[t - j]))  # type: ignore[arg-type]
    if not found or extreme is None:
        return None, None
    extreme_shift = max_shift
    for k in range(1, max_shift + 1):
        if abs(float(open_[t - k]) - extreme) < 1e-9:
            extreme_shift = k
            break
    return extreme, t - extreme_shift


def detect_cisd(
    df: pd.DataFrame,
    config: CISDConfig | None = None,
    swing_levels: Iterable[tuple[float, str]] = (),
    fvg_zones: Iterable[tuple[float, float, int, str]] = (),
    sessions: Iterable[tuple[float, str]] = (),
    smt_bars: Iterable[tuple[int, str]] = (),
    sweep_bars: Iterable[tuple[int, str]] = (),
) -> list[CISD]:
    cfg = config or CISDConfig()
    if not cfg.enabled or len(df) < 5:
        return []

    close = df["close"].to_numpy(float)
    open_ = df["open"].to_numpy(float)
    high = df["high"].to_numpy(float)
    low = df["low"].to_numpy(float)
    volume = df["volume"].to_numpy(float) if "volume" in df else np.zeros(len(df))
    ctype = _ctype(close, open_)
    vol_sma = pd.Series(volume).rolling(20, min_periods=1).mean().to_numpy()
    atr = pd.Series(
        np.maximum.reduce([
            high - low,
            np.abs(high - np.r_[np.nan, close[:-1]]),
            np.abs(low - np.r_[np.nan, close[:-1]]),
        ])
    ).rolling(14, min_periods=1).mean().to_numpy()

    levels = list(swing_levels)
    zones = list(fvg_zones)
    sess = list(sessions)
    smt_list = list(smt_bars)
    sweep_list = list(sweep_bars)

    out: list[CISD] = []
    bias = 0
    level: float | None = None
    level_idx: int | None = None
    pivot: float | None = None
    pivot_idx: int | None = None

    for t in range(len(df)):
        ct = ctype[t]

        # seed the state from the first non-doji candle
        if bias == 0:
            seed = ct
            if seed == 0:
                for k in range(t - 1, -1, -1):
                    if ctype[k] != 0:
                        seed = ctype[k]
                        break
            if seed == 0:
                continue
            bias = 1 if seed > 0 else -1
            level, level_idx = _leg_extreme_current(open_, ctype, bias, t)
            pivot = float(high[t]) if bias == 1 else float(low[t])
            pivot_idx = t

        # update pivot and the current/previous leg open extreme
        if bias == 1 and high[t] > pivot:  # type: ignore[operator]
            pivot = float(high[t])
            pivot_idx = t
            if ct == 1:
                level, level_idx = _leg_extreme_current(open_, ctype, 1, t)
            else:
                p, pi = _leg_extreme_previous(open_, ctype, 1, t)
                if p is not None:
                    level, level_idx = p, pi
        elif bias == -1 and low[t] < pivot:  # type: ignore[operator]
            pivot = float(low[t])
            pivot_idx = t
            if ct == -1:
                level, level_idx = _leg_extreme_current(open_, ctype, -1, t)
            else:
                p, pi = _leg_extreme_previous(open_, ctype, -1, t)
                if p is not None:
                    level, level_idx = p, pi

        if level is None or level_idx is None or pivot is None:
            continue

        # close back through the level = CISD in the opposite direction
        is_bull = bias == -1 and close[t] > level
        is_bear = bias == 1 and close[t] < level
        if not (is_bull or is_bear):
            continue

        start = int(level_idx)
        sl = float(pivot)  # Pine current-TF body/wick placement uses cm_pivot
        target = _nearest_target(levels + sess, level, is_bull, t)
        target_ok = target is not None and abs(target - level) / max(abs(level - sl), 1e-9) >= cfg.min_rr
        if sweep_list:
            swept = any(
                (d == ("bull" if is_bull else "bear")) and (0 <= t - b <= cfg.sweep_window)
                for b, d in sweep_list
            )
        else:
            swept = _swept(levels, start, t, df, is_bull)
        pda = _fvg_delivery(zones, start, t, df, is_bull)
        vol_ok = bool(vol_sma[t] > 0 and volume[t] > vol_sma[t] * cfg.volume_mult)
        sess_sweep = _swept(sess, start, t, df, is_bull)
        smt = any(
            (d == ("bull" if is_bull else "bear")) and abs(t - b) <= max(cfg.lookback, 12)
            for b, d in smt_list
        )

        enabled = {
            "sweep": True,
            "pda": True,
            "volume": True,
            "session": bool(sess),
            "targets": True,
            "smt": True,
        }
        hits = {
            "sweep": swept,
            "pda": pda,
            "volume": vol_ok,
            "session": sess_sweep,
            "targets": target_ok,
            "smt": smt,
        }
        origin_ok = swept or pda or smt
        g = _grade(enabled, hits)
        disp_ok = bool(np.isnan(atr[t]) or atr[t] <= 0 or abs(close[t] - level) >= atr[t] * cfg.min_displacement_atr)

        if (not cfg.require_origin or origin_ok) and g >= cfg.min_grade and disp_ok:
            if not (out and out[-1].direction == ("bull" if is_bull else "bear")
                    and abs(out[-1].level - level) < (atr[t] if not np.isnan(atr[t]) else 1e-9) * cfg.proximity_atr):
                c = CISD(
                    direction="bull" if is_bull else "bear",
                    level=float(level),
                    extreme=float(pivot),
                    body_extreme=float(pivot),
                    start=start,
                    end=t,
                    sl=sl,
                    target=float(target) if target is not None else None,
                    grade=g,
                    checks=hits,
                    active=True,
                    label="CISD",
                )
                for k in range(t + 1, len(df)):
                    if (c.direction == "bull" and close[k] < c.sl) or (c.direction == "bear" and close[k] > c.sl):
                        c.active = False
                        c.invalidated = True
                        break
                out.append(c)

        # reset state to the new delivery direction
        bias = 1 if is_bull else -1
        pivot = float(high[t]) if bias == 1 else float(low[t])
        pivot_idx = t
        level, level_idx = _leg_extreme_current(open_, ctype, bias, t)

    result: list[CISD] = []
    for d in ("bull", "bear"):
        subset = [c for c in out if c.direction == d]
        if cfg.hide_invalid:
            subset = [c for c in subset if c.active]
        result.extend(subset[-cfg.max_per_side:])
    result.sort(key=lambda c: c.end)
    return result

