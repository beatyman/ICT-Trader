"""Extra ICT context: HTF CISDs, SMT divergences and session liquidity."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import time as dtime
from typing import Iterable, Literal
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from .cisd import CISD, CISDConfig, detect_cisd

Direction = Literal["bull", "bear"]


@dataclass
class SessionLevel:
    name: str
    price: float
    kind: Literal["H", "L"]
    start: int
    end: int
    swept: bool = False


@dataclass
class SMT:
    direction: Direction
    start: int
    end: int
    price: float
    cmp_price: float
    symbol: str = ""
    label: str = "SMT"


def _pivots(df: pd.DataFrame, left: int, right: int = 1):
    high = df["high"].to_numpy(float)
    low = df["low"].to_numpy(float)
    out = []
    for i in range(left, len(df) - right):
        if high[i] >= high[i - left:i + right + 1].max():
            out.append(("H", i, float(high[i]), i + right))
        if low[i] <= low[i - left:i + right + 1].min():
            out.append(("L", i, float(low[i]), i + right))
    return out


def detect_session_levels(df: pd.DataFrame, max_per: int = 2) -> list[SessionLevel]:
    """Previous day/week plus Asia/London session highs/lows."""
    if len(df) < 2:
        return []
    dates = pd.to_datetime(df["date"])
    high = df["high"].to_numpy(float)
    low = df["low"].to_numpy(float)
    n = len(df)
    out: list[SessionLevel] = []

    # previous day / week
    day_start = 0
    week_start = 0
    cur_day = dates.iloc[0].date()
    cur_week = dates.iloc[0].isocalendar().week
    day_h = high[0]
    day_l = low[0]
    week_h = high[0]
    week_l = low[0]
    for i in range(1, n):
        d = dates.iloc[i].date()
        w = dates.iloc[i].isocalendar().week
        if d != cur_day:
            out.append(SessionLevel("PDH", float(day_h), "H", day_start, i - 1))
            out.append(SessionLevel("PDL", float(day_l), "L", day_start, i - 1))
            cur_day = d
            day_start = i
            day_h = high[i]
            day_l = low[i]
        else:
            day_h = max(day_h, high[i])
            day_l = min(day_l, low[i])
        if w != cur_week:
            out.append(SessionLevel("PWH", float(week_h), "H", week_start, i - 1))
            out.append(SessionLevel("PWL", float(week_l), "L", week_start, i - 1))
            cur_week = w
            week_start = i
            week_h = high[i]
            week_l = low[i]
        else:
            week_h = max(week_h, high[i])
            week_l = min(week_l, low[i])

    # Asia 20:00-00:00 ET, London 02:00-05:00 ET (same defaults as the Pine)
    if n >= 4:
        tz = ZoneInfo("America/New_York")
        try:
            et = dates.dt.tz_localize("Asia/Shanghai").dt.tz_convert(tz)
        except Exception:
            et = dates.dt.tz_localize("UTC").dt.tz_convert(tz)
        for name, start_t, end_t in (
            ("Asia", dtime(20, 0), dtime(23, 59, 59)),
            ("London", dtime(2, 0), dtime(5, 0)),
        ):
            cur_key = None
            cur_h = cur_l = None
            cur_start = 0
            for i, ts in enumerate(et):
                key = ts.date() if start_t <= ts.time() <= end_t else None
                if key is not None:
                    if cur_key != key:
                        if cur_key is not None and cur_h is not None:
                            out.append(SessionLevel(f"{name}.H", float(cur_h), "H", cur_start, i - 1))
                            out.append(SessionLevel(f"{name}.L", float(cur_l), "L", cur_start, i - 1))
                        cur_key = key
                        cur_h = high[i]
                        cur_l = low[i]
                        cur_start = i
                    else:
                        cur_h = max(cur_h, high[i])  # type: ignore[arg-type]
                        cur_l = min(cur_l, low[i])  # type: ignore[arg-type]
            if cur_key is not None and cur_h is not None:
                out.append(SessionLevel(f"{name}.H", float(cur_h), "H", cur_start, n - 1))
                out.append(SessionLevel(f"{name}.L", float(cur_l), "L", cur_start, n - 1))

    # sweep state
    close = df["close"].to_numpy(float)
    for lvl in out:
        for t in range(lvl.start, n):
            if lvl.kind == "H" and high[t] > lvl.price:
                lvl.swept = True
                lvl.end = t
                break
            if lvl.kind == "L" and low[t] < lvl.price:
                lvl.swept = True
                lvl.end = t
                break

    # keep the latest N per name/kind
    result: list[SessionLevel] = []
    for key in {(x.name, x.kind) for x in out}:
        subset = [x for x in out if (x.name, x.kind) == key][-max_per:]
        result.extend(subset)
    return result


def detect_htf_cisds(df: pd.DataFrame, tfs: Iterable[str] = ("1h", "4h")) -> list[CISD]:
    """Resample to higher timeframes, run CISD, map levels back to base bars."""
    if len(df) < 10:
        return []
    base = df.copy()
    base["date"] = pd.to_datetime(base["date"])
    idx = base.set_index("date")
    out: list[CISD] = []
    for tf in tfs:
        try:
            htf = idx.resample(tf).agg(
                {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
            ).dropna(subset=["open", "high", "low", "close"])
        except Exception:
            continue
        if len(htf) < 10:
            continue
        htf = htf.reset_index()
        cfg = CISDConfig(enabled=True, min_grade=0, max_per_side=1,
                         require_origin=False, hide_invalid=False, min_rr=0.0)
        found = detect_cisd(htf, cfg)
        for c in found:
            h_start = htf["date"].iloc[c.start]
            h_end = htf["date"].iloc[c.end]
            b_start = int(base["date"].searchsorted(h_start, side="right") - 1)
            b_end = int(base["date"].searchsorted(h_end, side="right") - 1)
            if b_start < 0:
                b_start = 0
            if b_end < 0:
                continue
            out.append(CISD(
                direction=c.direction, level=c.level, extreme=c.extreme,
                body_extreme=c.body_extreme, start=b_start, end=b_end,
                sl=c.sl, target=c.target, grade=c.grade, checks=c.checks,
                active=c.active, label=f"{tf.upper()} CISD", tf=tf.upper(),
            ))
    out.sort(key=lambda c: c.end)
    return out


def detect_smt(df: pd.DataFrame, cmp_df: pd.DataFrame, sensitivity: str = "Normal",
               symbol: str = "", max_out: int = 3) -> list[SMT]:
    """Price/correlated-asset pivot divergence (SMT)."""
    if cmp_df is None or len(cmp_df) < 10:
        return []
    cmp = cmp_df.copy()
    cmp["date"] = pd.to_datetime(cmp["date"])
    base = df.copy()
    base["date"] = pd.to_datetime(base["date"])
    # align correlated highs/lows to base timestamps
    cmp = cmp.sort_values("date").set_index("date")
    aligned = cmp.reindex(base["date"], method="ffill")
    if aligned[["high", "low"]].isna().all().any():
        return []
    left = {"Sensitive": 2, "Normal": 5, "Strict": 10}.get(sensitivity, 5)
    bp = _pivots(base, left, 1)
    cp = {i: (kind, price) for kind, i, price, _ in _pivots(cmp.reset_index(), left, 1)}

    last_high = None
    last_low = None
    out: list[SMT] = []
    for kind, i, price, confirmed in sorted(bp, key=lambda x: x[3]):
        if confirmed >= len(base):
            continue
        c_kind, c_price = cp.get(i, ("", np.nan))
        if c_kind != kind or np.isnan(c_price):
            continue
        if kind == "H":
            if last_high is not None:
                p_i, p_price, p_cmp = last_high
                if price > p_price and c_price < p_cmp:
                    out.append(SMT("bear", p_i, i, price, float(c_price), symbol))
            last_high = (i, price, float(c_price))
        else:
            if last_low is not None:
                p_i, p_price, p_cmp = last_low
                if price < p_price and c_price > p_cmp:
                    out.append(SMT("bull", p_i, i, price, float(c_price), symbol))
            last_low = (i, price, float(c_price))
    out.sort(key=lambda s: s.end)
    return out[-max_out:]

