"""ICT Concepts (LuxAlgo-inspired) in Python.

This is a standalone re-implementation of the core ICT ideas from the
"ICT Concepts [LuxAlgo]" Pine script:

  * swing highs / lows and a zigzag
  * MSS / BOS
  * Fair Value Gaps (FVG), optional BPR
  * order blocks (OB)
  * equal-high / equal-low liquidity
  * New Week Opening Gap (NWOG) / New Day Opening Gap (NDOG)
  * optional killzones for intraday data
  * Fibonacci levels on the last swing leg
  * a matplotlib chart and a signals CSV

It is not a line-by-line port of every Pine object/lifecycle detail; it is a
practical Python engine that can consume a CSV or a Futu OpenD dataframe.

Example:
    python ict_concepts.py --csv output/csi300/csv/SH600519.csv --bars 260

Original Pine indicator: CC BY-NC-SA 4.0, © LuxAlgo.
This Python re-implementation keeps the same non-commercial share-alike spirit.
"""
from __future__ import annotations

import argparse
import math
from dataclasses import dataclass, field
from datetime import datetime, time
from pathlib import Path
from typing import Iterable, Literal
from zoneinfo import ZoneInfo

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.patches import Rectangle

from .cisd import CISD, CISDConfig, detect_cisd
from .enhancements import SMT, SessionLevel, detect_htf_cisds, detect_session_levels, detect_smt
from .sweeps import KeyLevel, LiquiditySweep, detect_key_levels, detect_sweeps, nearest_key_target

Direction = Literal["bull", "bear"]


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------
@dataclass
class ICTConfig:
    mode: Literal["present", "historical"] = "present"
    swing_len: int = 5
    displacement_filter: bool = True
    displacement_body_ratio: float = 0.36
    fvg_min_atr: float = 0.0
    fvg_min_body: float = 0.0
    fvg_max_age: int = 500
    show_fvg: bool = True
    show_bpr: bool = False
    show_ifvg: bool = True
    show_ob: bool = True
    show_liquidity: bool = True
    show_nwog: bool = True
    show_ndog: bool = False
    show_killzones: bool = False
    show_fib: bool = True
    max_fvg: int = 2
    max_ob: int = 1
    max_liq: int = 2
    max_nwog: int = 3
    max_ndog: int = 1
    max_events: int = 4
    liq_margin_atr: float = 2.5  # tolerance = ATR / liq_margin_atr
    plot_bars: int = 260
    show_cisd: bool = True
    cisd_min_grade: int = 3
    cisd_max: int = 2
    cisd_all: bool = False
    cisd_extend: int = 16
    cisd_require_origin: bool = True
    cisd_min_displacement_atr: float = 0.0
    show_htf_cisd: bool = True
    htf_tfs: tuple[str, ...] = ("1h", "4h")
    max_htf: int = 2
    show_sessions: bool = True
    max_sessions: int = 2
    show_sweeps: bool = True
    sweep_window: int = 15
    sweep_reclaim_bars: int = 3
    sweep_min_penetration_atr: float = 0.0
    max_key_levels: int = 10
    show_smt: bool = False
    max_smt: int = 3


@dataclass
class Swing:
    index: int
    price: float
    kind: Literal["H", "L"]
    confirmed_at: int


@dataclass
class StructureEvent:
    index: int
    price: float
    kind: Literal["MSS", "BOS"]
    direction: Direction


@dataclass
class FVG:
    direction: Direction
    top: float
    bottom: float
    left: int
    right: int
    created: int
    active: bool = True
    mitigated: bool = False
    label: str = "FVG"
    inverted: bool = False
    invert_bar: int | None = None
    size_atr: float = 0.0
    body_ratio: float = 0.0
    volume_ratio: float = 0.0


@dataclass
class OrderBlock:
    direction: Direction
    top: float
    bottom: float
    left: int
    right: int
    created: int
    active: bool = True
    mitigated: bool = False


@dataclass
class Liquidity:
    direction: Literal["buyside", "sellside"]
    top: float
    bottom: float
    left: int
    right: int
    broken: bool = False
    touches: int = 1

    @property
    def created(self) -> int:
        return self.left

    @property
    def equal(self) -> bool:
        return self.touches >= 2


@dataclass
class Gap:
    kind: Literal["NWOG", "NDOG"]
    top: float
    bottom: float
    left: int
    right: int


@dataclass
class FibLevel:
    ratio: float
    price: float


@dataclass
class ICTResult:
    bars: pd.DataFrame
    swings: list[Swing] = field(default_factory=list)
    events: list[StructureEvent] = field(default_factory=list)
    fvgs: list[FVG] = field(default_factory=list)
    order_blocks: list[OrderBlock] = field(default_factory=list)
    liquidity: list[Liquidity] = field(default_factory=list)
    gaps: list[Gap] = field(default_factory=list)
    killzones: dict[str, list[tuple[int, int]]] = field(default_factory=dict)
    fib: list[FibLevel] = field(default_factory=list)
    trend: pd.Series | None = None
    cisds: list[CISD] = field(default_factory=list)
    htf_cisds: list[CISD] = field(default_factory=list)
    sessions: list[SessionLevel] = field(default_factory=list)
    smts: list[SMT] = field(default_factory=list)
    key_levels: list[KeyLevel] = field(default_factory=list)
    sweeps: list[LiquiditySweep] = field(default_factory=list)
    config: ICTConfig = field(default_factory=ICTConfig)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def load_csv(path: str | Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    rename = {
        "日期": "date", "时间": "date", "datetime": "date",
        "开盘": "open", "收盘": "close", "最高": "high",
        "最低": "low", "成交量": "volume",
    }
    df = df.rename(columns={k: v for k, v in rename.items() if k in df.columns})
    cols = ["date", "open", "high", "low", "close"]
    missing = [c for c in cols if c not in df.columns]
    if missing:
        raise ValueError(f"missing columns: {missing}")
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").drop_duplicates("date").reset_index(drop=True)
    for c in ["open", "high", "low", "close"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    if "volume" in df.columns:
        df["volume"] = pd.to_numeric(df["volume"], errors="coerce").fillna(0)
    else:
        df["volume"] = 0.0
    return df.dropna(subset=cols).reset_index(drop=True)


def atr(df: pd.DataFrame, n: int = 10) -> pd.Series:
    h, l, c = df["high"], df["low"], df["close"]
    prev = c.shift(1)
    tr = pd.concat([(h - l), (h - prev).abs(), (l - prev).abs()], axis=1).max(axis=1)
    return tr.rolling(n, min_periods=1).mean()


def find_pivots(df: pd.DataFrame, left: int, right: int = 1) -> list[Swing]:
    """Pine-like ta.pivothigh/pivotlow with left/right bars."""
    highs = df["high"].to_numpy()
    lows = df["low"].to_numpy()
    out: list[Swing] = []
    n = len(df)
    for i in range(left, n - right):
        window_h = highs[i - left: i + right + 1]
        if highs[i] >= window_h.max():
            out.append(Swing(i, float(highs[i]), "H", i + right))
        window_l = lows[i - left: i + right + 1]
        if lows[i] <= window_l.min():
            out.append(Swing(i, float(lows[i]), "L", i + right))
    return out


def build_zigzag(pivots: list[Swing]) -> list[Swing]:
    """Alternating H/L zigzag, replacing the last point on extension."""
    pivots = sorted(pivots, key=lambda s: (s.index, s.kind))
    zz: list[Swing] = []
    for p in pivots:
        if not zz:
            zz.append(p)
            continue
        last = zz[-1]
        if p.kind == last.kind:
            if (p.kind == "H" and p.price > last.price) or (p.kind == "L" and p.price < last.price):
                zz[-1] = p
            continue
        zz.append(p)
    return zz


# ---------------------------------------------------------------------------
# Core engine
# ---------------------------------------------------------------------------
def detect_structure(df: pd.DataFrame, swings: list[Swing]) -> tuple[list[StructureEvent], pd.Series]:
    """Close-through-swing MSS/BOS with a simple trend state."""
    n = len(df)
    close = df["close"].to_numpy()
    events: list[StructureEvent] = []
    trend = 0
    last_broken_high: float | None = None
    last_broken_low: float | None = None
    trend_series = np.zeros(n, dtype=int)

    for t in range(n):
        highs = [s for s in swings if s.kind == "H" and s.confirmed_at <= t and s.index < t]
        lows = [s for s in swings if s.kind == "L" and s.confirmed_at <= t and s.index < t]
        sh = highs[-1] if highs else None
        sl = lows[-1] if lows else None

        if sh is not None and close[t] > sh.price and sh.price != last_broken_high:
            kind: Literal["MSS", "BOS"] = "MSS" if trend <= 0 else "BOS"
            events.append(StructureEvent(t, sh.price, kind, "bull"))
            trend = 1
            last_broken_high = sh.price
            last_broken_low = None
        elif sl is not None and close[t] < sl.price and sl.price != last_broken_low:
            kind = "MSS" if trend >= 0 else "BOS"
            events.append(StructureEvent(t, sl.price, kind, "bear"))
            trend = -1
            last_broken_low = sl.price
            last_broken_high = None
        trend_series[t] = trend
    return events, pd.Series(trend_series, index=df.index, name="trend")


def detect_fvgs(df: pd.DataFrame, cfg: ICTConfig) -> list[FVG]:
    if not cfg.show_fvg:
        return []
    high = df["high"].to_numpy()
    low = df["low"].to_numpy()
    open_ = df["open"].to_numpy()
    close = df["close"].to_numpy()
    body = np.abs(close - open_)
    mean_body = pd.Series(body).rolling(cfg.swing_len, min_periods=1).mean().to_numpy()
    atr14 = atr(df, 14).to_numpy()
    vol = df["volume"].to_numpy(float) if "volume" in df else np.zeros(len(df))
    vol_avg = pd.Series(vol).rolling(20, min_periods=1).mean().to_numpy()
    out: list[FVG] = []
    n = len(df)
    for i in range(2, n):
        disp_up = body[i - 1] > mean_body[i - 1] and close[i - 1] > open_[i - 1]
        disp_dn = body[i - 1] > mean_body[i - 1] and close[i - 1] < open_[i - 1]
        body_ratio = body[i - 1] / max(high[i - 1] - low[i - 1], 1e-12)
        vol_ratio = vol[i - 1] / vol_avg[i - 1] if vol_avg[i - 1] > 0 else 0.0
        if low[i] > high[i - 2] and (disp_up or not cfg.displacement_filter):
            gap = float(low[i] - high[i - 2])
            if gap >= cfg.fvg_min_atr * atr14[i] and body_ratio >= cfg.fvg_min_body:
                out.append(FVG("bull", float(low[i]), float(high[i - 2]), i - 2, i, i,
                               size_atr=gap / max(atr14[i], 1e-12), body_ratio=body_ratio,
                               volume_ratio=vol_ratio))
        if high[i] < low[i - 2] and (disp_dn or not cfg.displacement_filter):
            gap = float(low[i - 2] - high[i])
            if gap >= cfg.fvg_min_atr * atr14[i] and body_ratio >= cfg.fvg_min_body:
                out.append(FVG("bear", float(low[i - 2]), float(high[i]), i - 2, i, i,
                               size_atr=gap / max(atr14[i], 1e-12), body_ratio=body_ratio,
                               volume_ratio=vol_ratio))

    # update mitigation and right edges
    for f in out:
        for t in range(f.created + 1, n):
            if f.direction == "bull":
                if low[t] <= f.bottom:
                    f.active = False
                    f.mitigated = True
                    f.right = t
                    break
                if low[t] < f.top:
                    f.right = t
            else:
                if high[t] >= f.top:
                    f.active = False
                    f.mitigated = True
                    f.right = t
                    break
                if high[t] > f.bottom:
                    f.right = t
        if f.active:
            f.right = n - 1
    return out


def detect_bpr(fvgs: list[FVG]) -> list[FVG]:
    """Balance Price Range = overlap of the latest bull and bear FVG."""
    bulls = [f for f in fvgs if f.direction == "bull"]
    bears = [f for f in fvgs if f.direction == "bear"]
    if not bulls or not bears:
        return []
    b, s = bulls[-1], bears[-1]
    top = min(b.top, s.top)
    bottom = max(b.bottom, s.bottom)
    if top <= bottom:
        return []
    return [FVG("bull", top, bottom, min(b.left, s.left), max(b.right, s.right),
                max(b.created, s.created), True, False, "BPR")]


def detect_ifvg(fvgs: list[FVG], df: pd.DataFrame) -> list[FVG]:
    """A close through an FVG flips it into an inverse FVG (IFVG)."""
    close = df["close"].to_numpy(float)
    n = len(df)
    out: list[FVG] = []
    for f in fvgs:
        if f.inverted or f.invert_bar is not None:
            continue
        for t in range(f.created + 1, n):
            if f.direction == "bull" and close[t] < f.bottom:
                f.inverted = True
                f.invert_bar = t
                out.append(FVG("bear", f.top, f.bottom, t, n - 1, t,
                               active=True, label="IFVG", inverted=True, invert_bar=t))
                break
            if f.direction == "bear" and close[t] > f.top:
                f.inverted = True
                f.invert_bar = t
                out.append(FVG("bull", f.top, f.bottom, t, n - 1, t,
                               active=True, label="IFVG", inverted=True, invert_bar=t))
                break
    return out


def detect_order_blocks(df: pd.DataFrame, events: list[StructureEvent], swings: list[Swing],
                        cfg: ICTConfig) -> list[OrderBlock]:
    if not cfg.show_ob:
        return []
    highs = df["high"].to_numpy()
    lows = df["low"].to_numpy()
    opens = df["open"].to_numpy()
    closes = df["close"].to_numpy()
    out: list[OrderBlock] = []
    for ev in events:
        prior = [s for s in swings if s.index < ev.index]
        if ev.direction == "bull":
            lows_before = [s for s in prior if s.kind == "L"]
            if not lows_before:
                continue
            anchor = lows_before[-1]
            seg_low = lows[anchor.index: ev.index + 1]
            if len(seg_low) == 0:
                continue
            j = anchor.index + int(np.argmin(seg_low))
            bottom = float(lows[j])
            top = float(max(opens[j], closes[j]))
            if top <= bottom:
                top = float(highs[j])
            out.append(OrderBlock("bull", top, bottom, j, ev.index, ev.index))
        else:
            highs_before = [s for s in prior if s.kind == "H"]
            if not highs_before:
                continue
            anchor = highs_before[-1]
            seg_high = highs[anchor.index: ev.index + 1]
            if len(seg_high) == 0:
                continue
            j = anchor.index + int(np.argmax(seg_high))
            top = float(highs[j])
            bottom = float(min(opens[j], closes[j]))
            if top <= bottom:
                bottom = float(lows[j])
            out.append(OrderBlock("bear", top, bottom, j, ev.index, ev.index))

    # mitigation
    n = len(df)
    for ob in out:
        for t in range(ob.created + 1, n):
            if ob.direction == "bull":
                if closes[t] < ob.bottom:
                    ob.active = False
                    ob.mitigated = True
                    ob.right = t
                    break
                if closes[t] > ob.top:
                    ob.right = t
            else:
                if closes[t] > ob.top:
                    ob.active = False
                    ob.mitigated = True
                    ob.right = t
                    break
                if closes[t] < ob.bottom:
                    ob.right = t
        if ob.active:
            ob.right = n - 1
    return out


def detect_liquidity(df: pd.DataFrame, pivots: list[Swing], cfg: ICTConfig) -> list[Liquidity]:
    if not cfg.show_liquidity:
        return []
    atr_series = atr(df, 10).to_numpy()
    tol = max(float(np.nanmedian(atr_series)) / max(cfg.liq_margin_atr, 0.1), 1e-9)
    out: list[Liquidity] = []
    n = len(df)
    for kind, direction in (("H", "buyside"), ("L", "sellside")):
        pts = [p for p in pivots if p.kind == kind]
        pts.sort(key=lambda p: p.index)
        used: set[int] = set()
        for i, p in enumerate(pts):
            if i in used:
                continue
            cluster = [p]
            for j in range(i + 1, len(pts)):
                if abs(pts[j].price - p.price) <= tol:
                    cluster.append(pts[j])
                    used.add(j)
                elif pts[j].index - p.index > 80:
                    break
            if len(cluster) >= 3:
                left, right = cluster[0].index, cluster[-1].index
                if direction == "buyside":
                    top = max(x.price for x in cluster) + tol
                    bottom = min(x.price for x in cluster) - tol
                else:
                    top = max(x.price for x in cluster) + tol
                    bottom = min(x.price for x in cluster) - tol
                out.append(Liquidity(direction, top, bottom, left,
                                     min(right + 10, n - 1), touches=len(cluster)))
    # break state
    closes = df["close"].to_numpy()
    for liq in out:
        for t in range(liq.left, n):
            if liq.direction == "buyside" and closes[t] > liq.top:
                liq.broken = True
                liq.right = t
                break
            if liq.direction == "sellside" and closes[t] < liq.bottom:
                liq.broken = True
                liq.right = t
                break
    return out


def detect_gaps(df: pd.DataFrame, cfg: ICTConfig) -> list[Gap]:
    out: list[Gap] = []
    n = len(df)
    dates = pd.to_datetime(df["date"])
    opens = df["open"].to_numpy()
    closes = df["close"].to_numpy()
    for i in range(1, n):
        # NDOG: previous close -> today's open
        if cfg.show_ndog and abs(opens[i] - closes[i - 1]) > 1e-12:
            out.append(Gap("NDOG", max(opens[i], closes[i - 1]), min(opens[i], closes[i - 1]), i - 1, i))
        # NWOG: Friday close -> Monday open
        if cfg.show_nwog and dates.iloc[i].weekday() == 0 and dates.iloc[i - 1].weekday() == 4:
            out.append(Gap("NWOG", max(opens[i], closes[i - 1]), min(opens[i], closes[i - 1]), i - 1, i))
    # keep latest N per kind
    result: list[Gap] = []
    for kind, max_n in (("NWOG", cfg.max_nwog), ("NDOG", cfg.max_ndog)):
        subset = [g for g in out if g.kind == kind][-max_n:] if max_n else []
        result.extend(subset)
    return result


def detect_killzones(df: pd.DataFrame, cfg: ICTConfig) -> dict[str, list[tuple[int, int]]]:
    if not cfg.show_killzones or len(df) < 3:
        return {}
    dt = pd.to_datetime(df["date"])
    if (dt.iloc[-1] - dt.iloc[0]).total_seconds() / max(len(df) - 1, 1) > 20 * 3600:
        return {}  # daily bars: no intraday killzones
    sessions = {
        "New York": ("America/New_York", time(7, 0), time(9, 0)),
        "London Open": ("Europe/London", time(7, 0), time(10, 0)),
        "London Close": ("Europe/London", time(15, 0), time(17, 0)),
        "Asian": ("Asia/Tokyo", time(10, 0), time(14, 0)),
    }
    zones: dict[str, list[tuple[int, int]]] = {}
    for name, (tz, start, end) in sessions.items():
        current: list[int] = []
        spans: list[tuple[int, int]] = []
        for i, ts in enumerate(dt):
            local = ts.tz_localize("UTC").tz_convert(ZoneInfo(tz)) if ts.tzinfo is None else ts.tz_convert(ZoneInfo(tz))
            inside = start <= local.time() <= end
            if inside:
                current.append(i)
            elif current:
                spans.append((current[0], current[-1]))
                current = []
        if current:
            spans.append((current[0], current[-1]))
        zones[name] = spans
    return zones


def fibonacci(swings: list[Swing]) -> list[FibLevel]:
    if len(swings) < 2:
        return []
    a, b = swings[-2], swings[-1]
    diff = b.price - a.price
    ratios = [0.0, 0.236, 0.382, 0.5, 0.618, 0.786, 1.0, 1.618]
    return [FibLevel(r, a.price + diff * r) for r in ratios]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def _previous_day_levels(df: pd.DataFrame) -> list[tuple[float, str]]:
    dates = pd.to_datetime(df["date"]).dt.date
    out: list[tuple[float, str]] = []
    for i in range(1, len(df)):
        if dates.iloc[i] != dates.iloc[i - 1]:
            prev = df.iloc[i - 1]
            out.append((float(prev["high"]), "H"))
            out.append((float(prev["low"]), "L"))
    # dedupe near-identical prices
    uniq: dict[tuple[float, str], None] = {}
    for p, k in out:
        uniq[(round(p, 4), k)] = None
    return list(uniq.keys())


def analyze(df: pd.DataFrame, config: ICTConfig | None = None,
            smt_df: pd.DataFrame | None = None) -> ICTResult:
    cfg = config or ICTConfig()
    if len(df) < max(cfg.swing_len + 2, 10):
        raise ValueError("not enough bars")
    pivots = find_pivots(df, cfg.swing_len, 1)
    swings = build_zigzag(pivots)
    events, trend = detect_structure(df, swings)
    fvgs = detect_fvgs(df, cfg)
    if cfg.show_ifvg:
        fvgs.extend(detect_ifvg(list(fvgs), df))
    if cfg.show_bpr:
        fvgs.extend(detect_bpr(fvgs))
    obs = detect_order_blocks(df, events, swings, cfg)
    liq = detect_liquidity(df, pivots, cfg)
    gaps = detect_gaps(df, cfg)
    kz = detect_killzones(df, cfg)
    fib = fibonacci(swings) if cfg.show_fib else []
    sessions = detect_session_levels(df, max_per=cfg.max_sessions) if cfg.show_sessions else []
    htf_cisds = detect_htf_cisds(df, cfg.htf_tfs) if cfg.show_htf_cisd else []
    smts = detect_smt(df, smt_df, max_out=cfg.max_smt) if (cfg.show_smt and smt_df is not None) else []
    smt_bars = [(s.end, s.direction) for s in smts]
    key_levels = detect_key_levels(df, swings, sessions, liq)
    sweeps = detect_sweeps(df, key_levels,
                           min_penetration_atr=cfg.sweep_min_penetration_atr,
                           reclaim_window=cfg.sweep_reclaim_bars) if cfg.show_sweeps else []
    sweep_bars = [(s.reclaim_bar, s.direction) for s in sweeps]

    cisds: list[CISD] = []
    if cfg.show_cisd:
        cisd_cfg = CISDConfig(
            enabled=True,
            min_grade=cfg.cisd_min_grade,
            max_per_side=(100000 if cfg.cisd_all else cfg.cisd_max),
            extend_bars=cfg.cisd_extend,
            require_origin=cfg.cisd_require_origin,
            min_displacement_atr=cfg.cisd_min_displacement_atr,
            sweep_window=cfg.sweep_window,
            sweep_reclaim_bars=cfg.sweep_reclaim_bars,
            lookback=500,
            hide_invalid=not cfg.cisd_all,
        )
        cisd_levels = [(s.price, s.kind, s.confirmed_at) for s in swings]
        cisd_zones = [(f.top, f.bottom, f.created, f.direction) for f in fvgs]
        cisd_sessions = [(s.price, s.kind, s.end) for s in sessions]
        cisds = detect_cisd(df, cisd_cfg, cisd_levels, cisd_zones, cisd_sessions, smt_bars, sweep_bars)

    # Pine "Present" mode: old structures are not drawn/emitted.
    if cfg.mode == "present":
        cutoff = max(0, len(df) - 500)
        events = [e for e in events if e.index >= cutoff]
        fvgs = [f for f in fvgs if f.created >= cutoff]
        obs = [o for o in obs if o.created >= cutoff]
        liq = [l for l in liq if l.created >= cutoff]
        gaps = [g for g in gaps if g.left >= cutoff]
        cisds = [c for c in cisds if c.end >= cutoff]
        htf_cisds = [c for c in htf_cisds if c.end >= cutoff]
        sessions = [s for s in sessions if s.end >= cutoff]
        smts = [s for s in smts if s.end >= cutoff]
        key_levels = [k for k in key_levels if k.known_at >= cutoff]
        sweeps = [s for s in sweeps if s.reclaim_bar >= cutoff]

    return ICTResult(
        bars=df, swings=swings, events=events, fvgs=fvgs, order_blocks=obs,
        liquidity=liq, gaps=gaps, killzones=kz, fib=fib, trend=trend,
        cisds=cisds, htf_cisds=htf_cisds, sessions=sessions, smts=smts,
        key_levels=key_levels, sweeps=sweeps, config=cfg,
    )


def signals_frame(result: ICTResult) -> pd.DataFrame:
    rows: list[dict] = []
    for ev in result.events:
        rows.append({
            "date": result.bars["date"].iloc[ev.index],
            "type": ev.kind,
            "direction": ev.direction,
            "price": ev.price,
            "index": ev.index,
        })
    for f in result.fvgs:
        rows.append({
            "date": result.bars["date"].iloc[f.created],
            "type": f.label,
            "direction": f.direction,
            "price": (f.top + f.bottom) / 2,
            "index": f.created,
        })
    for c in result.cisds:
        rows.append({
            "date": result.bars["date"].iloc[c.end],
            "type": "CISD",
            "direction": c.direction,
            "price": c.level,
            "index": c.end,
        })
    for s in result.smts:
        rows.append({
            "date": result.bars["date"].iloc[s.end],
            "type": "SMT",
            "direction": s.direction,
            "price": s.price,
            "index": s.end,
        })
    for c in result.htf_cisds:
        rows.append({
            "date": result.bars["date"].iloc[c.end],
            "type": f"HTF_{c.tf}",
            "direction": c.direction,
            "price": c.level,
            "index": c.end,
        })
    for s in result.sweeps:
        rows.append({
            "date": result.bars["date"].iloc[s.reclaim_bar],
            "type": f"SWEEP_{s.name}",
            "direction": s.direction,
            "price": s.level,
            "index": s.reclaim_bar,
            "sweep_index": s.sweep_bar,
            "reclaim_bars": s.reclaim_bars,
        })
    for k in result.key_levels:
        if k.swept or k.broken:
            continue
        rows.append({
            "date": result.bars["date"].iloc[k.known_at],
            "type": f"KEY_{k.name}",
            "direction": "bear" if k.kind == "H" else "bull",
            "price": k.price,
            "index": k.known_at,
        })
    return pd.DataFrame(rows).sort_values("index").reset_index(drop=True)


def _latest_by_direction(items: Iterable, max_n: int, directions: tuple[str, ...]) -> list:
    """Keep only the latest N items per direction, like LuxAlgo's visible boxes."""
    if max_n <= 0:
        return []
    picked = []
    for direction in directions:
        subset = [x for x in items if x.direction == direction]
        subset.sort(key=lambda x: x.created)
        picked.extend(subset[-max_n:])
    picked.sort(key=lambda x: x.created)
    return picked


# ---------------------------------------------------------------------------
# Plotting
# ---------------------------------------------------------------------------
def plot(result: ICTResult, out: str | Path, title: str = "ICT Concepts", bars: int | None = None) -> Path:
    cfg = result.config
    n = len(result.bars)
    bars = bars or cfg.plot_bars
    start = max(0, n - bars)
    df = result.bars.iloc[start:].reset_index(drop=True)
    m = len(df)
    if m == 0:
        raise ValueError("empty plot window")

    fig, ax = plt.subplots(figsize=(15, 8), dpi=140)
    dates = pd.to_datetime(df["date"])

    # candles
    for i, row in df.iterrows():
        color = "#d62728" if row["close"] < row["open"] else "#2ca02c"
        ax.vlines(i, row["low"], row["high"], color=color, linewidth=0.8, zorder=3)
        body_low = min(row["open"], row["close"])
        body_high = max(row["open"], row["close"])
        height = max(body_high - body_low, 1e-9)
        ax.add_patch(Rectangle((i - 0.32, body_low), 0.64, height,
                               facecolor=color, edgecolor=color, alpha=0.85, zorder=4))

    def rel(idx: int) -> int:
        return idx - start

    # FVG / BPR boxes
    for f in _latest_by_direction(result.fvgs, max(cfg.max_fvg, 0), ("bull", "bear")):
        if f.right < start or f.left > n - 1:
            continue
        x0 = max(rel(f.left), 0)
        x1 = min(rel(f.right), m - 1)
        if x1 <= x0:
            continue
        color = "#00e676" if f.direction == "bull" else "#ff5252"
        alpha = 0.16 if f.active else 0.06
        ax.add_patch(Rectangle((x0, f.bottom), x1 - x0, f.top - f.bottom,
                               facecolor=color, edgecolor=color, alpha=alpha,
                               linestyle="--" if f.label == "BPR" else "-", zorder=1))
        ax.text(x0, (f.top + f.bottom) / 2, f.label, color=color, fontsize=7,
                va="center", alpha=0.9)

    # OB boxes: only the latest still-active order block(s)
    active_obs = [o for o in result.order_blocks if o.active]
    active_obs.sort(key=lambda o: o.created)
    active_obs = active_obs[-max(cfg.max_ob, 0):] if cfg.max_ob > 0 else []
    for ob in active_obs:
        if ob.right < start or ob.left > n - 1:
            continue
        x0 = max(rel(ob.left), 0)
        x1 = min(rel(ob.right), m - 1)
        if x1 <= x0:
            continue
        color = "#3e89fa" if ob.direction == "bull" else "#ff3131"
        ax.add_patch(Rectangle((x0, ob.bottom), x1 - x0, ob.top - ob.bottom,
                               facecolor=color, edgecolor=color, alpha=0.12,
                               linestyle=":", zorder=1))
        ax.text(x1, (ob.top + ob.bottom) / 2, "+OB" if ob.direction == "bull" else "-OB",
                color=color, fontsize=7, va="center", ha="left")

    # liquidity
    for liq in _latest_by_direction(result.liquidity, max(cfg.max_liq, 0), ("buyside", "sellside")):
        if liq.right < start or liq.left > n - 1:
            continue
        x0 = max(rel(liq.left), 0)
        x1 = min(rel(liq.right), m - 1)
        color = "#fa451c" if liq.direction == "buyside" else "#1ce4fa"
        ax.add_patch(Rectangle((x0, liq.bottom), x1 - x0, liq.top - liq.bottom,
                               facecolor="none", edgecolor=color, alpha=0.7,
                               linestyle=":", zorder=2))
        ax.text(x0, liq.top, "BSL" if liq.direction == "buyside" else "SSL",
                color=color, fontsize=7, va="bottom")

    # key levels: BSL/SSL, PDH/PDL, PWH/PWL, Asia/London
    shown_levels = [k for k in result.key_levels if not k.swept and not k.broken]
    shown_levels = sorted(shown_levels, key=lambda k: k.known_at)[-max(cfg.max_key_levels, 0):]
    for k in shown_levels:
        if k.known_at < start:
            continue
        x0 = max(rel(k.known_at), 0)
        x1 = m - 1
        color = "#7E57C2" if k.kind == "H" else "#26A69A"
        ax.hlines(k.price, x0, x1, colors=color, linestyles="--", linewidth=0.7, alpha=0.55, zorder=2)
        ax.text(x1, k.price, k.name, color=color, fontsize=6, va="center", ha="left", alpha=0.8)

    # liquidity sweeps: exact sweep bar, level and reclaim close
    for s in result.sweeps[-8:]:
        if s.sweep_bar < start:
            continue
        color = "#2962FF" if s.direction == "bull" else "#F23645"
        ax.scatter([rel(s.sweep_bar)], [s.sweep_price], marker="x", color=color, s=28, zorder=8)
        ax.text(rel(s.sweep_bar), s.sweep_price, f"${s.name}",
                color=color, fontsize=6, ha="center",
                va="bottom" if s.direction == "bull" else "top", zorder=9)

    # session liquidity: latest PDH/PDL/PWH/PWL and Asia/London levels
    for lvl in result.sessions:
        if lvl.end < start:
            continue
        x0 = max(rel(lvl.start), 0)
        x1 = min(rel(lvl.end) + 8, m - 1)
        color = "#607D8B" if not lvl.swept else "#B0BEC5"
        ax.hlines(lvl.price, x0, x1, colors=color, linestyles=":", linewidth=0.8, alpha=0.8, zorder=2)
        ax.text(x1, lvl.price, lvl.name, color=color, fontsize=6, va="center", ha="left")

    # SMT divergence lines
    for s in result.smts:
        if s.end < start or s.start < 0:
            continue
        y1 = float(result.bars["high"].iloc[s.start]) if s.direction == "bear" else float(result.bars["low"].iloc[s.start])
        y2 = float(result.bars["high"].iloc[s.end]) if s.direction == "bear" else float(result.bars["low"].iloc[s.end])
        color = "#2962FF" if s.direction == "bull" else "#F23645"
        ax.plot([rel(s.start), rel(s.end)], [y1, y2], color=color, linestyle=":", linewidth=0.9, alpha=0.8, zorder=4)
        ax.text(rel(s.end), y2, "SMT", color=color, fontsize=6, va="bottom", ha="center")

    # gaps
    for g in result.gaps:
        if g.right < start:
            continue
        x0 = max(rel(g.left), 0)
        x1 = min(rel(g.right) + 8, m - 1)
        color = "#ff5252" if g.kind == "NWOG" else "#ff9800"
        ax.add_patch(Rectangle((x0, g.bottom), x1 - x0, g.top - g.bottom,
                               facecolor=color, edgecolor=color, alpha=0.10, zorder=0))
        ax.text(x0, g.top, g.kind, color=color, fontsize=7, va="bottom")

    # CISD levels, stop loss and target
    grade_txt = {7: "A+", 6: "A", 5: "B+", 4: "B-", 3: "C", 0: "-"}
    for c in result.cisds:
        if c.end < start:
            continue
        x0 = max(rel(c.start), 0)
        x1 = min(rel(c.end) + cfg.cisd_extend, m - 1)
        color = "#2962FF" if c.direction == "bull" else "#F23645"
        alpha = 0.95 if c.active else 0.35
        ax.hlines(c.level, x0, x1, colors=color, linestyles="-" if c.active else ":",
                  linewidth=1.4, alpha=alpha, zorder=6)
        ax.text(x1, c.level, f"{c.label}{'+' if c.direction == 'bull' else '-'} {grade_txt.get(c.grade, '')}",
                color=color, fontsize=7, va="center", ha="left", alpha=alpha, zorder=7)
        if c.target is not None:
            ax.hlines(c.target, max(rel(c.end) - 2, 0), min(rel(c.end) + 22, m - 1),
                      colors=color, linestyles="--", linewidth=0.8, alpha=0.5, zorder=5)
            ax.text(min(rel(c.end) + 22, m - 1), c.target, "TP", color=color,
                    fontsize=6, va="center", ha="left", alpha=0.7)
        if c.active:
            ax.hlines(c.sl, max(rel(c.end) - 1, 0), min(rel(c.end) + 8, m - 1),
                      colors="#555555", linestyles=":", linewidth=0.8, alpha=0.7, zorder=5)
            ax.text(min(rel(c.end) + 8, m - 1), c.sl, "SL", color="#555555",
                    fontsize=6, va="center", ha="left", alpha=0.7)

    # HTF CISD levels
    for c in result.htf_cisds[-max(cfg.max_htf, 0):]:
        if c.end < start:
            continue
        x0 = max(rel(c.start), 0)
        x1 = min(rel(c.end) + cfg.cisd_extend, m - 1)
        color = "#64B5F6" if c.direction == "bull" else "#E57373"
        ax.hlines(c.level, x0, x1, colors=color, linestyles="--", linewidth=1.0, alpha=0.8, zorder=5)
        ax.text(x1, c.level, f"{c.tf} CISD{'+' if c.direction == 'bull' else '-'}",
                color=color, fontsize=6, va="center", ha="left", alpha=0.8)

    # swings + structure
    shown_events = result.events
    if cfg.mode == "present":
        shown_events = result.events[-max(cfg.max_events, 0):]
    for s in result.swings:
        if start <= s.index < n:
            ax.plot(rel(s.index), s.price, marker="v" if s.kind == "H" else "^",
                    color="#888888", markersize=4, zorder=5)
    for ev in shown_events:
        if start <= ev.index < n:
            color = "#00e6a1" if ev.direction == "bull" else "#e60400"
            y = result.bars["low"].iloc[ev.index] if ev.direction == "bull" else result.bars["high"].iloc[ev.index]
            offset = -0.012 * (df["high"].max() - df["low"].min())
            if ev.direction == "bear":
                offset = -offset
            ax.annotate(ev.kind, (rel(ev.index), y + offset), color=color,
                        fontsize=8, ha="center",
                        va="top" if ev.direction == "bull" else "bottom",
                        fontweight="bold", zorder=6)

    # fib
    if result.fib and result.swings:
        a, b = result.swings[-2], result.swings[-1]
        if b.index >= start:
            x0, x1 = rel(a.index), m - 1
            for lvl in result.fib:
                ax.hlines(lvl.price, x0, x1, colors="#999999",
                          linestyles="--", linewidth=0.5, alpha=0.6, zorder=1)
                ax.text(x1 - 0.5, lvl.price, f"{lvl.ratio:.3f}", fontsize=6,
                        color="#777777", va="center", ha="right", clip_on=True)

    # killzones
    for name, spans in result.killzones.items():
        for (a_i, b_i) in spans:
            if b_i < start:
                continue
            ax.axvspan(max(rel(a_i), 0), min(rel(b_i), m - 1), color="#999999", alpha=0.08, zorder=0)

    ax.set_title(
        f"{title}  |  bars={m}  MSS/BOS={len(shown_events)}  "
        f"FVG={len(_latest_by_direction(result.fvgs, cfg.max_fvg, ('bull', 'bear')))}  "
        f"OB={len(active_obs)}  CISD={len(result.cisds)}  "
        f"HTF={len(result.htf_cisds)}  SMT={len(result.smts)}  "
        f"SWEEP={len(result.sweeps)}  KL={len(result.key_levels)}"
    )
    ax.set_ylabel("Price")
    ax.grid(alpha=0.15)
    ax.set_xlim(-1, m)
    ticks = np.linspace(0, m - 1, min(10, m)).astype(int)
    ax.set_xticks(ticks)
    ax.set_xticklabels([dates.iloc[i].strftime("%Y-%m-%d") for i in ticks], rotation=30, ha="right")
    fig.tight_layout()
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out)
    plt.close(fig)
    return out


# ---------------------------------------------------------------------------
# Optional Futu adapter
# ---------------------------------------------------------------------------
def fetch_futu(code: str, start: str, end: str, ktype: str = "K_DAY") -> pd.DataFrame:
    """Fetch qfq bars from Futu OpenD.

    Requires: pip install futu-api, and OpenD running on 127.0.0.1:11111.
    """
    from futu import AuType, KLType, OpenQuoteContext  # lazy import

    kt = getattr(KLType, ktype)
    ctx = OpenQuoteContext(host="127.0.0.1", port=11111)
    try:
        ret, data, page_key = ctx.request_history_kline(
            code=code, start=start, end=end, ktype=kt,
            autype=AuType.QFQ, max_count=1000,
        )
        if ret != 0:
            raise RuntimeError(data)
        frames = [data]
        while page_key:
            ret, data, page_key = ctx.request_history_kline(
                code=code, start=start, end=end, ktype=kt,
                autype=AuType.QFQ, max_count=1000, page_req_key=page_key,
            )
            if ret != 0:
                raise RuntimeError(data)
            frames.append(data)
        df = pd.concat(frames, ignore_index=True)
        df = df.rename(columns={
            "time_key": "date", "open": "open", "close": "close",
            "high": "high", "low": "low", "volume": "volume",
        })
        df["date"] = pd.to_datetime(df["date"])
        return df.sort_values("date").drop_duplicates("date").reset_index(drop=True)
    finally:
        ctx.close()


def main() -> None:
    ap = argparse.ArgumentParser(description="ICT Concepts in Python")
    ap.add_argument("--csv", default="output/csi300/csv/SH600519.csv")
    ap.add_argument("--symbol", default="SH600519")
    ap.add_argument("--out", default="output/ict/ict_chart.png")
    ap.add_argument("--signals", default="output/ict/ict_signals.csv")
    ap.add_argument("--bars", type=int, default=260)
    ap.add_argument("--swing-len", type=int, default=5)
    ap.add_argument("--max-events", type=int, default=4)
    ap.add_argument("--max-ob", type=int, default=1)
    ap.add_argument("--mode", choices=["present", "historical"], default="present")
    ap.add_argument("--no-ob", action="store_true")
    ap.add_argument("--no-fvg", action="store_true")
    ap.add_argument("--no-cisd", action="store_true")
    ap.add_argument("--no-htf", action="store_true")
    ap.add_argument("--no-sessions", action="store_true")
    ap.add_argument("--no-sweeps", action="store_true")
    ap.add_argument("--sweep-window", type=int, default=15)
    ap.add_argument("--sweep-penetration", type=float, default=0.0)
    ap.add_argument("--fvg-min-atr", type=float, default=0.0)
    ap.add_argument("--fvg-min-body", type=float, default=0.0)
    ap.add_argument("--smt-csv", help="correlated asset CSV for SMT")
    ap.add_argument("--smt-code", help="Futu code for correlated asset, e.g. SH.000300")
    ap.add_argument("--smt-sensitivity", choices=["Sensitive", "Normal", "Strict"], default="Normal")
    ap.add_argument("--min-grade", choices=["C", "B-", "B+", "A", "A+"], default="C")
    ap.add_argument("--cisd-max", type=int, default=2)
    ap.add_argument("--min-displacement", type=float, default=0.0,
                    help="minimum close-through distance in ATR units")
    ap.add_argument("--cisd-allow-no-origin", action="store_true")
    ap.add_argument("--bpr", action="store_true")
    ap.add_argument("--futu-code", help="e.g. HK.00700; fetch via OpenD instead of CSV")
    ap.add_argument("--start", default="2024-01-01")
    ap.add_argument("--end", default=datetime.now().strftime("%Y-%m-%d"))
    args = ap.parse_args()

    if args.futu_code:
        df = fetch_futu(args.futu_code, args.start, args.end)
        symbol = args.futu_code
    else:
        df = load_csv(args.csv)
        symbol = args.symbol

    smt_df = None
    if args.smt_csv:
        smt_df = load_csv(args.smt_csv)
    elif args.smt_code:
        smt_df = fetch_futu(args.smt_code, args.start, args.end)

    cfg = ICTConfig(
        mode=args.mode,
        swing_len=args.swing_len,
        max_events=args.max_events,
        max_ob=args.max_ob,
        show_ob=not args.no_ob,
        show_fvg=not args.no_fvg,
        show_cisd=not args.no_cisd,
        cisd_min_grade={"C": 3, "B-": 4, "B+": 5, "A": 6, "A+": 7}[args.min_grade],
        cisd_max=args.cisd_max,
        cisd_require_origin=not args.cisd_allow_no_origin,
        cisd_min_displacement_atr=args.min_displacement,
        show_htf_cisd=not args.no_htf,
        show_sessions=not args.no_sessions,
        show_sweeps=not args.no_sweeps,
        sweep_window=args.sweep_window,
        sweep_min_penetration_atr=args.sweep_penetration,
        fvg_min_atr=args.fvg_min_atr,
        fvg_min_body=args.fvg_min_body,
        show_smt=bool(args.smt_csv or args.smt_code),
        max_smt=3,
        show_bpr=args.bpr,
        plot_bars=args.bars,
    )
    df.attrs["cfg"] = cfg
    result = analyze(df, cfg, smt_df=smt_df)
    path = plot(result, args.out, title=f"ICT Concepts - {symbol}", bars=args.bars)
    sig = signals_frame(result)
    Path(args.signals).parent.mkdir(parents=True, exist_ok=True)
    sig.to_csv(args.signals, index=False, encoding="utf-8-sig")
    print(f"chart: {path}")
    print(f"signals: {args.signals} ({len(sig)} rows)")
    print(f"swings={len(result.swings)} events={len(result.events)} "
          f"fvg={len(result.fvgs)} ob={len(result.order_blocks)} liq={len(result.liquidity)} "
          f"gaps={len(result.gaps)} cisd={len(result.cisds)} "
          f"htf={len(result.htf_cisds)} smt={len(result.smts)}")
    if len(sig):
        print(sig.tail(8).to_string(index=False))


if __name__ == "__main__":
    main()

