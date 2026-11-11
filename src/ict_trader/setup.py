"""The frozen multi-timeframe setup.

  1D  : Ichimoku cloud + Tenkan/Kijun + MACD  (3/3)
  4H  : Ichimoku cloud + Tenkan/Kijun + MACD  (>= 2/3)
  15m : CISD trigger, with optional cloud/MACD score
        + recent liquidity sweep
        + PDA and displacement quality

Entry is the next 15m open by default; `entry_mode="retest_key"` waits for a
retest of the CISD key level.  Stop is the CISD invalidation wick; target is
fixed R by default, with optional liquidity-pool targeting.  A sweep may reclaim
within a small window instead of only on its penetration candle.

The goal is one setup that can be repeated and reviewed, not a parameter
search contest.
"""
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd

from .cisd import CISD
from .concepts import ICTConfig, analyze, detect_fvgs, load_csv
from .context import (SwingIndex, candle2_closure, candle3_closure, dealing_range,
                      in_session, ote_from_signal, structure_bias)
from .pd_arrays import count_path_liquidity, near_arrays
from .sweeps import nearest_key_target
from .strategy import StrategyConfig, simulate_cisd
from .indicators import (add_indicators, align_htf, ichimoku, macd,
                         mark_htf_known_at, resample_cn_4h, resample_htf)
from .quality import Displacement, measure_displacement, score_setup
from .regime import Regime, market_regime


@dataclass
class SetupConfig:
    market: str = "cn"
    min_grade: int = 3
    min_confluence: int = 2
    require_1d_cloud: bool = True
    require_1d_macd: bool = True
    require_1d_tk: bool = True
    require_4h_cloud: bool = True
    require_4h_macd: bool = True
    require_4h_tk: bool = True
    require_15m_cloud: bool = True
    require_15m_macd: bool = True
    require_15m_hist_rising: bool = True
    min_1d_score: int = 2
    min_4h_score: int = 2
    min_15m_score: int = 1
    use_score: bool = True
    require_sweep: bool = True
    origin_filter: str = "any"  # any | both | sweep | fvg
    sweep_window: int = 15
    sweep_reclaim_bars: int = 3
    min_sweep_strength: float = 0.0   # BTC 15m A/B: >=3.0 lifts the win rate
    min_stop_atr: float = 0.0         # BTC 15m A/B: >2.0 ATR stops win ~55-60%
    use_key_targets: bool = False
    target_mode: str = "fixed"  # fixed | liquidity | hybrid | dol | htf_dol
    min_target_rr: float = 2.0
    max_target_rr: float = 6.0
    min_disp_body_ratio: float = 0.0
    min_disp_atr: float = 0.0
    min_disp_multiple: float = 0.0
    min_setup_score: float = 0.0
    fvg_min_atr: float = 0.25
    fvg_min_body: float = 0.5
    fvg_min_width_atr: float = 0.0
    allow_short: bool = False
    rr: float = 2.0
    max_bars: int = 40
    breakeven_at_r: float = 0.0
    t_plus_1: bool = False
    sell_tax_bps: float = 5.0
    risk_pct: float = 0.01
    max_concurrent: int = 5
    entry_mode: str = "next_open"
    retest_max_bars: int = 20
    retest_tolerance_atr: float = 0.0
    trail_activate_r: float = 0.0
    trail_atr_mult: float = 0.0
    max_daily_loss_r: float = 0.0
    # Execution costs, forwarded to StrategyConfig by run_one.  ``commission_bps``
    # is ONE round trip; Binance futures taker 5 bps/side -> 10.
    commission_bps: float = 5.0
    slippage_bps: float = 2.0
    t1_at_r: float = 0.0             # partial scale-out level (0 = off)
    t1_fraction: float = 0.5
    t1_move_be: bool = True
    t1_at_eq: bool = False           # doc 631: first leg at the dealing-range EQ
    session_gate: str = "none"  # none | auto | london | ny_am | ny_pm | cn | hk
    pd_gate: bool = False
    pd_buy_max: float = 0.5
    pd_sell_min: float = 0.5
    pd_lookback: int = 120
    ote_gate: bool = False
    ote_min: float = 0.62
    ote_max: float = 0.79
    require_pda_array: bool = False
    pda_array_tol_atr: float = 0.75
    require_first_fvg: bool = False
    require_fresh_fvg: bool = False
    htf_fvg: bool = True
    htf_fvg_window: int = 15
    regime_features: bool = True
    regime_trend_gate: str = "none"  # none | up | down | aligned
    regime_vol_gate: str = "none"    # none | low | normal | high
    max_inducements: int = -1
    quality_profile: str = "standard"  # standard | high_value
    stack: str = "1d_4h_15m"  # 1d_4h_15m | 1d_1h_15m | 1d_15m | 4h_15m | 4h_1h_15m
    daily_bias_filter: str = "none"  # none | c2 | c3 (E1: Candle 2/3 daily bias)
    htf_structure_gate: str = "none"  # none | aligned (corpus HTF structure HH/HL vs LH/LL)


def apply_quality_profile(cfg: SetupConfig) -> SetupConfig:
    """Apply an explicit, opt-in stricter profile.

    The high-value profile is deliberately a *starting point*, not a proven
    edge: the 0.65 displacement threshold improved the in-sample basket but did
    not survive a chronological hold-out in the bundled study.

    The high-rr profile wires the cross-source corpus consensus (see
    docs/HIGH_QUALITY_SETUP.md): higher grade/confluence, strong displacement,
    fresh FVG and a 2.5R floor, without touching the target mode.
    """
    if cfg.quality_profile == "high_value":
        return replace(
            cfg,
            min_grade=max(cfg.min_grade, 4),
            min_confluence=max(cfg.min_confluence, 3),
            min_disp_body_ratio=max(cfg.min_disp_body_ratio, 0.65),
            min_target_rr=max(cfg.min_target_rr, 2.0),
        )
    if cfg.quality_profile == "high_rr":
        # A/B result (BTC/ETH/SOL 15m, 2020-2026): grade>=4 and confluence>=3
        # both reject every signal (the engine's grade tops out at 3 and
        # confluence at 2); raising the fixed RR and the DOL target both made
        # expectancy WORSE.  The only lever that helped was displacement.
        return replace(
            cfg,
            min_disp_body_ratio=max(cfg.min_disp_body_ratio, 0.65),
        )
    if cfg.quality_profile == "btc_15m":
        # BTCUSDT perpetual 15m study profile - NOT a proven edge, off by default.
        #
        # Everything below is measured on BTCUSDT 15m/1h 2024-11..2026-11 with
        # taker 0.05%/side + 1bps/side (scripts/optimize_btc.py, rule_search.py):
        #   pool = this frozen setup             n=146 win 30.8% avg -0.358R
        #   train slice, this profile            n= 21 win 52.4% avg +0.19R
        #   holdout slice, this profile          n=  6 win 50.0% avg +0.45R
        #   FULL sample, this profile            n= 23 win 39.1% avg -0.084R
        # The split looked good and the full sample did not reproduce it: the
        # two conditions (displacement >= 1.45 ATR, stop >= 1.4 ATR) are the only
        # rule of 1,793 that survived its first holdout, and it still fails the
        # second test.  Kept so the study is reproducible, not recommended for
        # live use.  Sweep strength was explicitly rejected: strength >= 3 was
        # the best in-sample rule (54.9% / +0.28R) and fell to 13.3% / -0.59R out
        # of sample on 15m, and to 32-35% / negative when the 1h base and the
        # cost basis were changed.
        return replace(
            cfg,
            min_disp_atr=max(cfg.min_disp_atr, 1.45),
            min_stop_atr=max(cfg.min_stop_atr, 1.4),
            rr=max(cfg.rr, 2.5),
        )
    return cfg


def _drop_incomplete_htf(base: pd.DataFrame, h: pd.DataFrame) -> pd.DataFrame:
    """Drop a resampled HTF bar whose period closes after the last base bar."""
    if len(h) == 0 or len(base) == 0:
        return h
    if pd.to_datetime(h["date"].iloc[-1]) > pd.to_datetime(base["date"].iloc[-1]):
        return h.iloc[:-1].reset_index(drop=True)
    return h


def _htf(df: pd.DataFrame, rule: str, market: str = "cn") -> pd.DataFrame:
    h = resample_cn_4h(df) if (rule == "4h" and market == "cn") else resample_htf(df, rule)
    h = _drop_incomplete_htf(df, h)
    if not (rule == "4h" and market == "cn"):
        # Left-labelled resample buckets become knowable only at bucket close.
        h = mark_htf_known_at(h, rule)
    return align_htf(df, h, ["close", "tenkan", "kijun", "span_a", "span_b", "cloud_top", "cloud_bottom",
                             "macd", "macd_signal", "macd_hist"])


def _daily_htf(base: pd.DataFrame, daily: pd.DataFrame, close_hour: int = 15) -> pd.DataFrame:
    d = daily.copy()
    d["date"] = pd.to_datetime(d["date"]).dt.normalize() + pd.Timedelta(hours=close_hour)
    h = add_indicators(d)
    return align_htf(base, h, ["close", "tenkan", "kijun", "span_a", "span_b", "cloud_top", "cloud_bottom",
                               "macd", "macd_signal", "macd_hist"])


@dataclass(frozen=True)
class StackSpec:
    bias_tf: str
    confirm_tf: str | None
    min_bias: int
    min_confirm: int


def _stack_spec(cfg: SetupConfig) -> StackSpec:
    """Resolve the named multi-timeframe stack without hard-coding 1D/4H."""
    stacks = {
        "1d_4h_15m": StackSpec("1D", "4h", cfg.min_1d_score, cfg.min_4h_score),
        "1d_1h_15m": StackSpec("1D", "1h", cfg.min_1d_score, cfg.min_4h_score),
        "1d_15m": StackSpec("1D", None, cfg.min_1d_score, 0),
        "4h_15m": StackSpec("4h", None, cfg.min_4h_score, 0),
        "4h_1h_15m": StackSpec("4h", "1h", cfg.min_4h_score, cfg.min_4h_score),
    }
    return stacks.get(cfg.stack, stacks["1d_4h_15m"])


class _BoxIndex:
    """Struct-of-arrays view of a list of price boxes (FVG / OB / PDA array).

    ``build_setup`` scanned every FVG (thousands) for every CISD, which was the
    largest cost in the loop (measured: 617k ``.ce`` property calls).  The same
    "nearest box on the correct side, known at bar t" answer is a numpy argmin
    over pre-extracted arrays.  List order is preserved, so the chosen box is
    identical to the stable ``sorted(key=abs(...))[0]`` version.
    """

    __slots__ = ("items", "created", "active", "fresh", "first", "edge")

    def __init__(self, items, edge_of, fresh_of=None, first_of=None) -> None:
        self.items = list(items)
        self.created = np.array([int(x.created) for x in self.items], dtype=np.int64)
        self.active = np.array([bool(x.active) for x in self.items], dtype=bool)
        self.edge = np.array([float(edge_of(x)) for x in self.items], dtype=float)
        self.fresh = (np.array([bool(fresh_of(x)) for x in self.items], dtype=bool)
                      if fresh_of is not None else np.ones(len(self.items), dtype=bool))
        self.first = (np.array([bool(first_of(x)) for x in self.items], dtype=bool)
                      if first_of is not None else np.ones(len(self.items), dtype=bool))

    def nearest(self, level: float, t: int, *, require_fresh: bool = False):
        """Return ``(index, item)`` of the nearest live box, or ``(None, None)``."""
        if not self.items:
            return None, None
        mask = (self.created <= t) & self.active
        if require_fresh:
            mask &= self.fresh
        if not mask.any():
            return None, None
        diff = np.where(mask, np.abs(self.edge - float(level)), np.inf)
        return int(np.argmin(diff)), self.items[int(np.argmin(diff))]

    def is_first(self, j) -> bool:
        return bool(self.first[j]) if j is not None else False


def _swing_index(swings: Iterable):
    """Backwards-compatible alias kept for readability inside setup.py."""
    return SwingIndex(swings)


class _SetupPrecompute:
    """Everything ``build_setup`` needs per bar, computed once per call."""

    def __init__(self, df: pd.DataFrame, result, cfg: "SetupConfig", daily, spec: StackSpec):
        self.n = len(df)
        self.close = df["close"].to_numpy(float)
        self.high = df["high"].to_numpy(float)
        self.low = df["low"].to_numpy(float)
        self.open = df["open"].to_numpy(float)
        tr = np.maximum.reduce([
            self.high - self.low,
            np.abs(self.high - np.r_[np.nan, self.close[:-1]]),
            np.abs(self.low - np.r_[np.nan, self.close[:-1]]),
        ])
        self.atr14 = pd.Series(tr).rolling(14, min_periods=1).mean().to_numpy()
        self.swings = SwingIndex(result.swings)

        # HTF indicator columns as numpy, plus the NaN gate and the per-bar
        # checks for both directions (avoids a pandas row lookup per CISD).
        d1 = _tf_frame(df, daily, spec.bias_tf, cfg.market)
        self.d1 = {c: d1[c].to_numpy(float) for c in
                   ("close", "tenkan", "kijun", "span_a", "span_b", "macd", "macd_signal")}
        self.d1_ok = ~np.isnan(np.column_stack(
            [self.d1[c] for c in ("tenkan", "kijun", "span_a", "span_b", "macd", "macd_signal")]
        )).any(axis=1)
        self.h4 = None
        self.h4_ok = None
        if spec.confirm_tf:
            h4 = _tf_frame(df, daily, spec.confirm_tf, cfg.market)
            self.h4 = {c: h4[c].to_numpy(float) for c in
                       ("close", "tenkan", "kijun", "span_a", "span_b", "macd", "macd_signal")}
            self.h4_ok = ~np.isnan(np.column_stack(
                [self.h4[c] for c in
                 ("close", "tenkan", "kijun", "span_a", "span_b", "macd", "macd_signal")]
            )).any(axis=1)
        i15 = ichimoku(df)
        self.i15 = {c: i15[c].to_numpy(float) for c in ("tenkan", "kijun", "span_a", "span_b")}
        self.i15_ok = ~np.isnan(np.column_stack(list(self.i15.values()))).any(axis=1)
        self.hist = macd(df)["hist"].to_numpy(float)
        self.hist_prev = np.r_[np.nan, self.hist[:-1]]

        # price boxes
        self.fvg = {d: _BoxIndex([f for f in result.fvgs if f.direction == d],
                                 edge_of=lambda f: f.ce,
                                 fresh_of=lambda f: f.fresh,
                                 first_of=lambda f: f.first_presented)
                    for d in ("bull", "bear")}
        self.ob = {d: _BoxIndex([o for o in result.order_blocks if o.direction == d],
                                edge_of=lambda o: o.mean)
                   for d in ("bull", "bear")}
        self.pda_top = {d: np.array([float(a.top) for a in result.pd_arrays if a.direction == d])
                        for d in ("bull", "bear")}
        self.pda_bottom = {d: np.array([float(a.bottom) for a in result.pd_arrays
                                        if a.direction == d]) for d in ("bull", "bear")}
        self.pda_created = {d: np.array([int(a.confirmed_at) for a in result.pd_arrays
                                         if a.direction == d], dtype=np.int64)
                            for d in ("bull", "bear")}
        self.pda_kind = {d: [a.kind for a in result.pd_arrays if a.direction == d]
                         for d in ("bull", "bear")}

        # sweeps (result.sweeps is sorted by reclaim_bar)
        self.sweep_reclaim = np.array([int(s.reclaim_bar) for s in result.sweeps], dtype=np.int64)
        self.sweep_dir = [s.direction for s in result.sweeps]

        # regime + displacement inputs
        self.ema20 = pd.Series(self.close).ewm(span=20, adjust=False).mean().to_numpy()
        self.body = np.abs(self.close - self.open)
        self.vol = df["volume"].to_numpy(float) if "volume" in df else np.zeros(self.n)

    def bars_ok(self, t: int) -> bool:
        """NaN gate: HTF 1D indicators, 4H indicators and 15m Ichimoku present."""
        if not self.d1_ok[t] or not self.i15_ok[t]:
            return False
        return True if self.h4_ok is None else bool(self.h4_ok[t])

    def htf_checks(self, t: int, direction: str, cfg: "SetupConfig"):
        """The 1D / 4H / 15m indicator checks for one CISD, as bool lists."""
        bull = direction == "bull"
        d1, h4, i15 = self.d1, self.h4, self.i15
        d1_checks = []
        if cfg.require_1d_cloud:
            span = np.maximum(d1["span_a"][t], d1["span_b"][t]) if bull else \
                np.minimum(d1["span_a"][t], d1["span_b"][t])
            d1_checks.append(bool(d1["close"][t] > span) if bull else bool(d1["close"][t] < span))
        if cfg.require_1d_tk:
            d1_checks.append(bool(d1["tenkan"][t] > d1["kijun"][t]) if bull
                             else bool(d1["tenkan"][t] < d1["kijun"][t]))
        if cfg.require_1d_macd:
            d1_checks.append(bool(d1["macd"][t] > d1["macd_signal"][t]) if bull
                             else bool(d1["macd"][t] < d1["macd_signal"][t]))
        h4_checks = []
        if h4 is not None:
            if cfg.require_4h_cloud:
                span = np.maximum(h4["span_a"][t], h4["span_b"][t]) if bull else \
                    np.minimum(h4["span_a"][t], h4["span_b"][t])
                h4_checks.append(bool(h4["close"][t] > span) if bull
                                 else bool(h4["close"][t] < span))
            if cfg.require_4h_tk:
                h4_checks.append(bool(h4["tenkan"][t] > h4["kijun"][t]) if bull
                                 else bool(h4["tenkan"][t] < h4["kijun"][t]))
            if cfg.require_4h_macd:
                h4_checks.append(bool(h4["macd"][t] > h4["macd_signal"][t]) if bull
                                 else bool(h4["macd"][t] < h4["macd_signal"][t]))
        m15_checks = []
        if cfg.require_15m_cloud:
            span = np.maximum(i15["span_a"][t], i15["span_b"][t]) if bull else \
                np.minimum(i15["span_a"][t], i15["span_b"][t])
            m15_checks.append(bool(self.close[t] > span) if bull else bool(self.close[t] < span))
        if cfg.require_15m_macd:
            m15_checks.append(bool(self.hist[t] > 0) if bull else bool(self.hist[t] < 0))
        if cfg.require_15m_hist_rising:
            rising = t > 0 and self.hist[t] > self.hist_prev[t]
            m15_checks.append(rising if bull else (t > 0 and self.hist[t] < self.hist_prev[t]))
        return d1_checks, h4_checks, m15_checks

    def near_arrays(self, direction: str, price: float, atr: float, t: int,
                    tol_atr: float) -> list[str]:
        """Kinds of same-side PDA arrays known at ``t`` within ``tol_atr`` ATR."""
        top, bottom = self.pda_top[direction], self.pda_bottom[direction]
        if len(top) == 0:
            return []
        tol = max(float(atr) * float(tol_atr), 0.0)
        dist = np.where((bottom <= price) & (price <= top), 0.0,
                        np.minimum(np.abs(price - top), np.abs(price - bottom)))
        mask = (dist <= tol) & (self.pda_created[direction] <= t)
        return [self.pda_kind[direction][i] for i in np.nonzero(mask)[0]]

    def regime(self, t: int, lookback: int = 500):
        """Vectorised ``regime.market_regime`` for one bar."""
        if t < 20 or t >= self.n:
            return Regime("flat", "normal", 0.0, 0.0, 0.5)
        a = float(self.atr14[t]) if not np.isnan(self.atr14[t]) else 0.0
        slope = float(self.ema20[t] - self.ema20[max(0, t - 10)])
        strength = slope / a if a > 0 else 0.0
        atr_pct = a / float(self.close[t]) if self.close[t] else 0.0
        lo = max(0, t - lookback + 1)
        hist = self.atr14[lo:t + 1]
        hist = hist[~np.isnan(hist)]
        rank = float((hist <= a).mean()) if len(hist) else 0.5
        trend = "up" if strength > 0.15 else "down" if strength < -0.15 else "flat"
        vol = "high" if rank >= 0.75 else "low" if rank <= 0.25 else "normal"
        return Regime(trend, vol, strength, atr_pct, rank)

    def displacement(self, idx: int, lookback: int = 20) -> Displacement:
        """Same arithmetic as ``quality.measure_displacement``, O(lookback)."""
        body = abs(float(self.close[idx]) - float(self.open[idx]))
        rng = max(float(self.high[idx]) - float(self.low[idx]), 1e-12)
        body_ratio = body / rng
        a = max(0, idx - lookback)
        prev_body = self.body[a:idx]
        avg_body = float(prev_body.mean()) if len(prev_body) else body
        body_multiple = body / max(avg_body, 1e-12)
        body_atr = body / max(float(self.atr14[idx]), 1e-12)
        vol_avg = float(self.vol[a:idx].mean()) if idx else 0.0
        volume_ratio = float(self.vol[idx] / vol_avg) if vol_avg > 0 else 0.0
        score = 0.0
        score += 0.4 * min(body_ratio / 0.65, 1.0)
        score += 0.3 * min(body_atr / 1.0, 1.0)
        score += 0.2 * min(body_multiple / 1.3, 1.0)
        score += 0.1 * min(volume_ratio / 1.2, 1.0)
        return Displacement(body_ratio, body_atr, body_multiple, volume_ratio, min(score, 1.0))


def _tf_frame(df: pd.DataFrame, daily: pd.DataFrame | None, tf: str, market: str) -> pd.DataFrame:
    if tf == "1D":
        if market == "crypto":
            return _htf(df, "1D", market)
        close_hour = 16 if market in {"hk", "us"} else 15
        return _daily_htf(df, daily, close_hour) if daily is not None else _htf(df, "1D", market)
    return _htf(df, tf, market)


def _raw_htf(df: pd.DataFrame, daily: pd.DataFrame | None, tf: str, market: str) -> pd.DataFrame | None:
    if tf == "1D" and market != "crypto":
        if daily is None:
            return None
        h = daily.copy()
        h["date"] = (pd.to_datetime(h["date"]).dt.normalize()
                     + pd.Timedelta(hours=16 if market in {"hk", "us"} else 15))
        return _drop_incomplete_htf(df, h.sort_values("date").reset_index(drop=True))
    if tf == "4h" and market == "cn":
        return _drop_incomplete_htf(df, resample_cn_4h(df))
    return mark_htf_known_at(_drop_incomplete_htf(df, resample_htf(df, tf)), tf)


def _htf_fvg_zones(df: pd.DataFrame, daily: pd.DataFrame | None,
                   cfg: SetupConfig) -> list[tuple[float, float, int, str, str]]:
    """HTF FVG zones mapped to the first base bar that knows the zone."""
    base_dates = pd.to_datetime(df["date"]).to_numpy()
    fcfg = ICTConfig(show_fvg=True, displacement_filter=True,
                     fvg_min_atr=cfg.fvg_min_atr, fvg_min_body=cfg.fvg_min_body,
                     fvg_min_width_atr=cfg.fvg_min_width_atr)
    zones: list[tuple[float, float, int, str, str]] = []
    for tf in ("1h", "4h", "1D"):
        h = _raw_htf(df, daily, tf, cfg.market)
        if h is None or len(h) < 4:
            continue
        h = h.reset_index(drop=True)
        hdates = pd.to_datetime(h["date"]).to_numpy()
        for f in detect_fvgs(h, fcfg):
            known = int(np.searchsorted(base_dates, hdates[f.created], side="left"))
            if known >= len(df):
                continue
            top = float(f.top)
            bot = float(f.bottom)
            if top > bot:
                zones.append((top, bot, known, f.direction, tf))
    return zones


def _htf_fvg_delivery(zones: list[tuple[float, float, int, str, str]],
                      df: pd.DataFrame, start: int, end: int, direction: str,
                      window: int) -> tuple[bool, str]:
    low = df["low"].to_numpy(float)
    high = df["high"].to_numpy(float)
    lo = max(start, end - max(window, 1))
    for top, bot, known, fdir, tf in zones:
        if fdir != direction or known > end:
            continue
        a = max(lo, known)
        if a > end:
            continue
        if np.any((low[a:end + 1] <= top) & (high[a:end + 1] >= bot)):
            return True, tf
    return False, ""



def _daily_bias_series(df: pd.DataFrame, daily: pd.DataFrame | None,
                       cfg: SetupConfig, spec: StackSpec) -> list[str | None]:
    """E1: bias-TF Candle-2/3 state, right-aligned to the base bars.

    Each base bar sees only the most recently *closed* bias-TF bar (``searchsorted
    side="right"`` minus one), so the filter never uses an in-progress daily/4H bar.
    Returns a list aligned to ``len(df)`` of ``"bull"``/``"bear"``/``None``.
    """
    if cfg.daily_bias_filter not in {"c2", "c3"}:
        return [None] * len(df)
    d = _raw_htf(df, daily, spec.bias_tf, cfg.market)
    if d is None or len(d) < 2:
        return [None] * len(df)
    d = d.reset_index(drop=True)
    n = len(d)
    bias: list[str | None] = [None] * n
    mode = cfg.daily_bias_filter
    for i in range(n):
        if i < 1:
            continue
        prev = d.iloc[i - 1]
        cur = d.iloc[i]
        if mode == "c2":
            bias[i] = candle2_closure(
                float(prev["high"]), float(prev["low"]),
                float(cur["high"]), float(cur["low"]), float(cur["close"]))
        else:
            if i < 2:
                continue
            c2 = d.iloc[i - 1]
            c3 = d.iloc[i]
            bias[i] = candle3_closure(
                float(prev["high"]), float(prev["low"]),
                float(c2["open"]), float(c2["close"]),
                float(c2["high"]), float(c2["low"]),
                float(c3["open"]), float(c3["close"]))
    ddates = pd.to_datetime(d["date"]).to_numpy()
    base_dates = pd.to_datetime(df["date"]).to_numpy()
    out: list[str | None] = [None] * len(df)
    for j, bd in enumerate(base_dates):
        idx = int(np.searchsorted(ddates, bd, side="right")) - 1
        if 0 <= idx < n:
            out[j] = bias[idx]
    return out


def build_setup(df: pd.DataFrame, result, cfg: SetupConfig,
                daily: pd.DataFrame | None = None) -> list[CISD]:
    cfg = apply_quality_profile(cfg)
    if cfg.origin_filter == "fvg" and cfg.require_sweep:
        cfg = replace(cfg, require_sweep=False)
    spec = _stack_spec(cfg)
    # All per-bar series are computed once: the loop below visits every CISD
    # (1210 on 20k BTC bars) and previously re-derived ATR, the HTF row lookup,
    # the swing scans and the displacement window each time.
    pre = _SetupPrecompute(df, result, cfg, daily, spec)
    close, high, low, atr14 = pre.close, pre.high, pre.low, pre.atr14
    htf_zones = _htf_fvg_zones(df, daily, cfg) if cfg.htf_fvg else []
    daily_bias = _daily_bias_series(df, daily, cfg, spec)
    out: list[CISD] = []
    for c in result.cisds:
        t = c.end
        if c.direction == "bear" and not cfg.allow_short:
            continue
        if c.grade < cfg.min_grade or sum(bool(v) for v in c.checks.values()) < cfg.min_confluence:
            continue
        bias_dir = daily_bias[t]
        if cfg.daily_bias_filter != "none" and bias_dir != c.direction:
            continue
        htf_struct = pre.swings.structure_bias(t)
        if cfg.htf_structure_gate == "aligned" and htf_struct != c.direction:
            continue
        if cfg.session_gate != "none" and not in_session(df, t, cfg.market, cfg.session_gate):
            continue

        pd_pos = None
        dr = pre.swings.dealing_range(t, cfg.pd_lookback)
        if dr is not None:
            pd_pos = dr.position(c.level)
        if cfg.pd_gate:
            if dr is None:
                continue
            if c.direction == "bull" and pd_pos > cfg.pd_buy_max:
                continue
            if c.direction == "bear" and pd_pos < cfg.pd_sell_min:
                continue

        ote_pos = ote_from_signal(df, c.start, c.end, c.level, c.direction)
        if cfg.ote_gate:
            if ote_pos is None or not (cfg.ote_min <= ote_pos <= cfg.ote_max):
                continue

        array_hits = pre.near_arrays(c.direction, c.level, atr14[t], t,
                                     cfg.pda_array_tol_atr)
        if cfg.require_pda_array and not array_hits:
            continue
        _, chosen_fvg = pre.fvg[c.direction].nearest(
            c.level, t, require_fresh=cfg.require_fresh_fvg)
        if cfg.require_fresh_fvg and chosen_fvg is None:
            continue
        if cfg.require_first_fvg and (chosen_fvg is None or not chosen_fvg.first_presented):
            continue

        _, chosen_ob = pre.ob[c.direction].nearest(c.level, t)

        if not pre.bars_ok(t):
            continue
        d1_checks, h4_checks, m15_checks = pre.htf_checks(t, c.direction, cfg)

        def passed(checks, min_score):
            if not checks:
                return True
            if cfg.use_score:
                return sum(bool(x) for x in checks) >= min(min_score, len(checks))
            return all(checks)

        if passed(d1_checks, spec.min_bias) and passed(h4_checks, spec.min_confirm) and passed(m15_checks, cfg.min_15m_score):
            # result.sweeps is sorted by reclaim_bar, so the window is a slice.
            lo = int(np.searchsorted(pre.sweep_reclaim, t - cfg.sweep_window, side="left"))
            hi = int(np.searchsorted(pre.sweep_reclaim, t, side="right"))
            recent_sweeps = [result.sweeps[k] for k in range(lo, hi)
                             if pre.sweep_dir[k] == c.direction]
            origin = (
                "both" if c.checks.get("sweep") and c.checks.get("pda")
                else "sweep" if c.checks.get("sweep")
                else "fvg" if c.checks.get("pda")
                else "none"
            )
            if cfg.origin_filter != "any" and origin != cfg.origin_filter:
                continue
            htf_pda, htf_tf = _htf_fvg_delivery(
                htf_zones, df, c.start, c.end, c.direction, cfg.htf_fvg_window
            )
            pda_source = (
                "both" if htf_pda and c.checks.get("pda")
                else "htf" if htf_pda
                else "current" if c.checks.get("pda")
                else "none"
            )
            if cfg.require_sweep and not recent_sweeps:
                continue
            sweep_strength = max((s.strength for s in recent_sweeps), default=0)
            if cfg.min_sweep_strength > 0 and sweep_strength < cfg.min_sweep_strength:
                continue
            if cfg.min_stop_atr > 0:
                stop_atr = abs(float(c.level) - float(c.sl)) / max(float(atr14[t]), 1e-12)
                if stop_atr < cfg.min_stop_atr:
                    continue
            disp = pre.displacement(t)
            if disp.body_ratio < cfg.min_disp_body_ratio:
                continue
            if disp.body_atr < cfg.min_disp_atr:
                continue
            if disp.body_multiple < cfg.min_disp_multiple:
                continue
            quality, quality_parts = score_setup(
                d1_score=sum(bool(x) for x in d1_checks),
                h4_score=sum(bool(x) for x in h4_checks),
                cisd_grade=c.grade,
                sweep_strength=sweep_strength,
                pda=bool(c.checks.get("pda")),
                displacement=disp.score,
                volume=bool(c.checks.get("volume")),
                session=bool(c.checks.get("session")),
            )
            if quality < cfg.min_setup_score:
                continue
            best_sweep = max(recent_sweeps, key=lambda s: s.strength) if recent_sweeps else None
            quality_parts.update({
                "stack": cfg.stack,
                "bias_tf": spec.bias_tf,
                "confirm_tf": spec.confirm_tf,
                "cisd_swept": bool(c.checks.get("sweep")),
                "cisd_pda": bool(c.checks.get("pda")),
                "htf_pda": bool(htf_pda),
                "htf_fvg_tf": htf_tf,
                "pda_source": pda_source,
                "origin": (
                    "both" if c.checks.get("sweep") and c.checks.get("pda")
                    else "sweep" if c.checks.get("sweep")
                    else "fvg" if c.checks.get("pda")
                    else "none"
                ),
                "d1_score": sum(bool(x) for x in d1_checks),
                "h4_score": sum(bool(x) for x in h4_checks),
                "m15_score": sum(bool(x) for x in m15_checks),
                "sweep_strength": sweep_strength,
                "sweep_reclaim_bars": int(best_sweep.reclaim_bars) if best_sweep else -1,
                "sweep_penetration_atr": float(best_sweep.penetration_atr) if best_sweep else 0.0,
                "disp_body_ratio": float(disp.body_ratio),
                "disp_body_atr": float(disp.body_atr),
                "disp_body_multiple": float(disp.body_multiple),
                "disp_volume_ratio": float(disp.volume_ratio),
                "daily_bias": bias_dir or "",
                "htf_structure": htf_struct,
            })
            if pd_pos is not None:
                quality_parts["pd"] = float(pd_pos)
            if dr is not None:
                # doc 631 L76: the equilibrium is the partial-exit level, so the
                # exact price must travel with the setup to the strategy layer.
                quality_parts["dr_eq"] = float(dr.eq)
                quality_parts["dr_high"] = float(dr.high)
                quality_parts["dr_low"] = float(dr.low)
            if ote_pos is not None:
                quality_parts["ote"] = float(ote_pos)
            if array_hits:
                quality_parts["pda_arrays"] = list(array_hits)
            if chosen_fvg is not None:
                quality_parts["fvg_ce"] = float(chosen_fvg.ce)
                quality_parts["fvg_size_atr"] = float(chosen_fvg.size_atr)
                quality_parts["fvg_first"] = bool(chosen_fvg.first_presented)
                quality_parts["fvg_fresh"] = bool(chosen_fvg.fresh)
            target = None
            mode = cfg.target_mode
            if cfg.use_key_targets and mode == "fixed":
                mode = "hybrid"
            if mode in {"liquidity", "hybrid", "dol", "htf_dol"}:
                levels = [k for k in result.key_levels
                          if k.known_at <= t
                          and (k.sweep_bar is None or k.sweep_bar > t)
                          and (k.broken_at is None or k.broken_at > t)]
                risk = abs(c.level - c.sl)
                prices = sorted(
                    [k.price for k in levels
                     if (k.price > c.level if c.direction == "bull" else k.price < c.level)],
                    key=lambda p: abs(p - c.level),
                )
                if mode in {"dol", "htf_dol"}:
                    if mode == "htf_dol":
                        # Corpus HTF DOL: prefer time-anchored session/pool levels
                        # (PDH/PWH/EQH above, PDL/PWL/EQL below), skip arbitrary
                        # swing BSL/SSL. Fall back to all levels if none anchor.
                        anchored = [k for k in levels if k.source in {"session", "pool"}]
                        pool_prices = sorted(
                            [k.price for k in anchored
                             if (k.price > c.level if c.direction == "bull" else k.price < c.level)],
                            key=lambda p: abs(p - c.level),
                        )
                        pool_prices = pool_prices or prices
                    else:
                        pool_prices = prices
                    valid = [p for p in pool_prices if risk > 0
                             and cfg.min_target_rr <= abs(p - c.level) / risk <= cfg.max_target_rr]
                    target = valid[0] if valid else None
                    if target is None:
                        continue
                else:
                    tgt = nearest_key_target(levels, c.level, c.direction == "bull", at=t)
                    if tgt is not None and risk > 0:
                        rr = abs(tgt - c.level) / risk
                        if cfg.min_target_rr <= rr <= cfg.max_target_rr:
                            target = tgt
                    if target is None and mode == "liquidity":
                        continue
            path_target = target
            if path_target is None:
                risk = abs(c.level - c.sl)
                path_target = c.level + (cfg.rr * risk if c.direction == "bull" else -cfg.rr * risk)
            inducements = count_path_liquidity(result.swings, c.level, path_target, c.direction)
            if cfg.max_inducements >= 0 and inducements > cfg.max_inducements:
                continue
            quality_parts["inducement_candidates"] = inducements
            if cfg.regime_features or cfg.regime_trend_gate != "none" or cfg.regime_vol_gate != "none":
                reg = pre.regime(t)
                expected = "up" if c.direction == "bull" else "down"
                if cfg.regime_trend_gate == "aligned" and reg.trend != expected:
                    continue
                if cfg.regime_trend_gate in {"up", "down"} and reg.trend != cfg.regime_trend_gate:
                    continue
                if cfg.regime_vol_gate != "none" and reg.volatility != cfg.regime_vol_gate:
                    continue
                if cfg.regime_features:
                    quality_parts.update({
                        "regime_trend": reg.trend,
                        "regime_vol": reg.volatility,
                        "trend_strength": float(reg.trend_strength),
                        "atr_pct": float(reg.atr_pct),
                        "vol_rank": float(reg.vol_rank),
                    })
            if cfg.entry_mode == "fvg_ce" and chosen_fvg:
                entry_hint = float(chosen_fvg.ce)
            elif cfg.entry_mode == "ob_me" and chosen_ob:
                entry_hint = float(chosen_ob.mean)
            elif cfg.entry_mode == "ote":
                seg_high = float(high[c.start:c.end + 1].max())
                seg_low = float(low[c.start:c.end + 1].min())
                if seg_high > seg_low:
                    entry_hint = (seg_high - 0.705 * (seg_high - seg_low)) if c.direction == "bull" \
                        else (seg_low + 0.705 * (seg_high - seg_low))
                else:
                    entry_hint = None
            else:
                entry_hint = None
            out.append(replace(c, target=target, quality=quality,
                               quality_parts=quality_parts, entry_hint=entry_hint))
    return out


def run_one(path: str | Path, cfg: SetupConfig, daily_path: str | Path | None = None) -> tuple[list, dict]:
    df = load_csv(path)
    daily = load_csv(daily_path) if daily_path else None
    ict_cfg = ICTConfig(mode="historical", show_sessions=False, show_htf_cisd=False,
                        cisd_all=True, cisd_min_grade=0,
                        fvg_min_atr=cfg.fvg_min_atr, fvg_min_body=cfg.fvg_min_body,
                        fvg_min_width_atr=cfg.fvg_min_width_atr,
                        sweep_reclaim_bars=cfg.sweep_reclaim_bars)
    res = analyze(df, ict_cfg)
    sigs = build_setup(df, res, cfg, daily=daily)
    res2 = replace(res, cisds=sigs)
    sc = StrategyConfig(rr=cfg.rr, max_bars=cfg.max_bars, min_grade=0, min_confluence=0,
                        allow_short=cfg.allow_short, risk_pct=cfg.risk_pct,
                        breakeven_at_r=cfg.breakeven_at_r,
                        t_plus_1=cfg.t_plus_1, sell_tax_bps=cfg.sell_tax_bps,
                        commission_bps=cfg.commission_bps, slippage_bps=cfg.slippage_bps,
                        t1_at_r=cfg.t1_at_r, t1_fraction=cfg.t1_fraction,
                        t1_move_be=cfg.t1_move_be, t1_at_eq=cfg.t1_at_eq,
                        max_concurrent=cfg.max_concurrent,
                        min_rr=cfg.min_target_rr,
                        entry_mode=cfg.entry_mode,
                        retest_max_bars=cfg.retest_max_bars,
                        retest_tolerance_atr=cfg.retest_tolerance_atr,
                        trail_activate_r=cfg.trail_activate_r,
                        trail_atr_mult=cfg.trail_atr_mult,
                        max_daily_loss_r=cfg.max_daily_loss_r)
    r = simulate_cisd(df, res2, sc, Path(path).stem)
    return r.trades, {"signals": len(sigs), **r.metrics}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", required=True)
    ap.add_argument("--daily-csv", default=None)
    ap.add_argument("--market", choices=["cn", "hk", "crypto", "other"], default="cn")
    ap.add_argument("--out", default="output/ict/setup_trades.csv")
    ap.add_argument("--metrics", default="output/ict/setup_metrics.json")
    ap.add_argument("--rr", type=float, default=2.0)
    ap.add_argument("--max-bars", type=int, default=40)
    ap.add_argument("--breakeven-at-r", type=float, default=0.0)
    ap.add_argument("--t-plus-1", action="store_true")
    ap.add_argument("--sell-tax-bps", type=float, default=5.0)
    ap.add_argument("--min-grade", type=int, default=3)
    ap.add_argument("--min-confluence", type=int, default=2)
    ap.add_argument("--allow-short", action="store_true")
    ap.add_argument("--min-1d-score", type=int, default=2)
    ap.add_argument("--min-4h-score", type=int, default=2)
    ap.add_argument("--min-15m-score", type=int, default=1)
    ap.add_argument("--strict", action="store_true", help="require every enabled check (AND)")
    ap.add_argument("--allow-no-sweep", action="store_true")
    ap.add_argument("--origin-mode", choices=["strict_sweep", "pine"], default="strict_sweep")
    ap.add_argument("--origin-filter", choices=["any", "both", "sweep", "fvg"], default="any")
    ap.add_argument("--sweep-reclaim-bars", type=int, default=3)
    ap.add_argument("--use-key-targets", action="store_true")
    ap.add_argument("--target-mode", choices=["fixed", "liquidity", "hybrid", "dol", "htf_dol"], default="fixed")
    ap.add_argument("--entry-mode", choices=["next_open", "retest_key", "fvg_ce", "ob_me", "ote"], default="next_open")
    ap.add_argument("--retest-max-bars", type=int, default=20)
    ap.add_argument("--retest-tolerance-atr", type=float, default=0.0)
    ap.add_argument("--trail-activate-r", type=float, default=0.0)
    ap.add_argument("--trail-atr-mult", type=float, default=0.0)
    ap.add_argument("--max-daily-loss-r", type=float, default=0.0)
    ap.add_argument("--session-gate", choices=["none", "auto", "asian", "london", "london_close", "ny_am", "ny_pm", "silver_bullet", "cn", "hk"],
                    default="none")
    ap.add_argument("--pd-gate", action="store_true")
    ap.add_argument("--ote-gate", action="store_true")
    ap.add_argument("--ote-min", type=float, default=0.62)
    ap.add_argument("--ote-max", type=float, default=0.79)
    ap.add_argument("--regime-trend-gate", choices=["none", "up", "down", "aligned"], default="none")
    ap.add_argument("--regime-vol-gate", choices=["none", "low", "normal", "high"], default="none")
    ap.add_argument("--require-pda-array", action="store_true")
    ap.add_argument("--require-first-fvg", action="store_true")
    ap.add_argument("--require-fresh-fvg", action="store_true")
    ap.add_argument("--max-inducements", type=int, default=-1)
    ap.add_argument("--quality-profile", choices=["standard", "high_value", "high_rr"], default="standard")
    ap.add_argument("--stack", choices=["1d_4h_15m", "1d_1h_15m", "1d_15m", "4h_15m", "4h_1h_15m"],
                    default="1d_4h_15m")
    ap.add_argument("--daily-bias-filter", choices=["none", "c2", "c3"], default="none",
                    help="E1: require a Candle-2/3 daily bias aligned with the signal")
    ap.add_argument("--htf-structure-gate", choices=["none", "aligned"], default="none",
                    help="require HTF structure (HH/HL for long, LH/LL for short) aligned with the signal")
    ap.add_argument("--min-disp-body", type=float, default=0.0)
    ap.add_argument("--min-disp-atr", type=float, default=0.0)
    ap.add_argument("--min-disp-multiple", type=float, default=0.0)
    ap.add_argument("--min-setup-score", type=float, default=0.0)
    ap.add_argument("--min-target-rr", type=float, default=2.0)
    ap.add_argument("--max-target-rr", type=float, default=6.0)
    args = ap.parse_args()
    cfg = SetupConfig(rr=args.rr, max_bars=args.max_bars, min_grade=args.min_grade,
                      min_confluence=args.min_confluence, allow_short=args.allow_short,
                      market=args.market, breakeven_at_r=args.breakeven_at_r,
                      t_plus_1=args.t_plus_1, sell_tax_bps=args.sell_tax_bps,
                      min_1d_score=args.min_1d_score, min_4h_score=args.min_4h_score,
                      min_15m_score=args.min_15m_score, use_score=not args.strict,
                      require_sweep=not args.allow_no_sweep and args.origin_mode != "pine" and args.origin_filter != "fvg",
                      origin_filter=args.origin_filter,
                      sweep_reclaim_bars=args.sweep_reclaim_bars,
                      use_key_targets=args.use_key_targets,
                      target_mode=args.target_mode,
                      entry_mode=args.entry_mode,
                      retest_max_bars=args.retest_max_bars,
                      retest_tolerance_atr=args.retest_tolerance_atr,
                      trail_activate_r=args.trail_activate_r,
                      trail_atr_mult=args.trail_atr_mult,
                      max_daily_loss_r=args.max_daily_loss_r,
                      session_gate=args.session_gate,
                      pd_gate=args.pd_gate,
                      ote_gate=args.ote_gate,
                      ote_min=args.ote_min,
                      ote_max=args.ote_max,
                      regime_trend_gate=args.regime_trend_gate,
                      regime_vol_gate=args.regime_vol_gate,
                      require_pda_array=args.require_pda_array,
                      require_first_fvg=args.require_first_fvg,
                      require_fresh_fvg=args.require_fresh_fvg,
                      max_inducements=args.max_inducements,
                      quality_profile=args.quality_profile,
                      stack=args.stack,
                      min_disp_body_ratio=args.min_disp_body,
                      min_disp_atr=args.min_disp_atr,
                      min_disp_multiple=args.min_disp_multiple,
                      min_setup_score=args.min_setup_score,
                      min_target_rr=args.min_target_rr,
                      max_target_rr=args.max_target_rr,
                      daily_bias_filter=args.daily_bias_filter,
                      htf_structure_gate=args.htf_structure_gate)
    trades, m = run_one(args.csv, cfg, daily_path=args.daily_csv)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame([t.__dict__ for t in trades]).to_csv(args.out, index=False, encoding="utf-8-sig")
    Path(args.metrics).write_text(json.dumps(m, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps(m, ensure_ascii=False, indent=2, default=str))
    print(f"trades: {args.out}")


if __name__ == "__main__":
    main()
