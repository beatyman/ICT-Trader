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

import numpy as np
import pandas as pd

from .cisd import CISD
from .concepts import ICTConfig, analyze, load_csv
from .context import dealing_range, in_session, ote_from_signal
from .pd_arrays import count_path_liquidity, near_arrays
from .sweeps import nearest_key_target
from .strategy import StrategyConfig, simulate_cisd
from .indicators import add_indicators, align_htf, ichimoku, macd, resample_cn_4h, resample_htf
from .quality import measure_displacement, score_setup


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
    sweep_window: int = 15
    sweep_reclaim_bars: int = 3
    use_key_targets: bool = False
    target_mode: str = "fixed"  # fixed | liquidity | hybrid
    min_target_rr: float = 2.0
    min_disp_body_ratio: float = 0.0
    min_disp_atr: float = 0.0
    min_disp_multiple: float = 0.0
    min_setup_score: float = 0.0
    fvg_min_atr: float = 0.25
    fvg_min_body: float = 0.5
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
    max_inducements: int = -1


def _htf(df: pd.DataFrame, rule: str, market: str = "cn") -> pd.DataFrame:
    h = resample_cn_4h(df) if (rule == "4h" and market == "cn") else resample_htf(df, rule)
    return align_htf(df, h, ["close", "tenkan", "kijun", "span_a", "span_b", "cloud_top", "cloud_bottom",
                             "macd", "macd_signal", "macd_hist"])


def _daily_htf(base: pd.DataFrame, daily: pd.DataFrame, close_hour: int = 15) -> pd.DataFrame:
    d = daily.copy()
    d["date"] = pd.to_datetime(d["date"]).dt.normalize() + pd.Timedelta(hours=close_hour)
    h = add_indicators(d)
    return align_htf(base, h, ["close", "tenkan", "kijun", "span_a", "span_b", "cloud_top", "cloud_bottom",
                               "macd", "macd_signal", "macd_hist"])


def build_setup(df: pd.DataFrame, result, cfg: SetupConfig,
                daily: pd.DataFrame | None = None) -> list[CISD]:
    if cfg.market == "crypto":
        # Crypto daily closes at 00:00 UTC; a 15m-resampled 1D avoids using
        # the current, still-open daily candle.
        d1 = _htf(df, "1D", cfg.market)
    else:
        close_hour = 16 if cfg.market == "hk" else 15
        d1 = _daily_htf(df, daily, close_hour) if daily is not None else _htf(df, "1D", cfg.market)
    h4 = _htf(df, "4h", cfg.market)
    i15 = ichimoku(df)
    m15 = macd(df)
    close = df["close"].to_numpy(float)
    high = df["high"].to_numpy(float)
    low = df["low"].to_numpy(float)
    tr = np.maximum.reduce([high - low, np.abs(high - np.r_[np.nan, close[:-1]]),
                            np.abs(low - np.r_[np.nan, close[:-1]])])
    atr14 = pd.Series(tr).rolling(14, min_periods=1).mean().to_numpy()
    out: list[CISD] = []
    for c in result.cisds:
        t = c.end
        if c.direction == "bear" and not cfg.allow_short:
            continue
        if c.grade < cfg.min_grade or sum(bool(v) for v in c.checks.values()) < cfg.min_confluence:
            continue
        if cfg.session_gate != "none" and not in_session(df, t, cfg.market, cfg.session_gate):
            continue

        pd_pos = None
        if cfg.pd_gate:
            dr = dealing_range(result.swings, t, cfg.pd_lookback)
            if dr is None:
                continue
            pd_pos = dr.position(c.level)
            if c.direction == "bull" and pd_pos > cfg.pd_buy_max:
                continue
            if c.direction == "bear" and pd_pos < cfg.pd_sell_min:
                continue

        ote_pos = None
        if cfg.ote_gate:
            ote_pos = ote_from_signal(df, c.start, c.end, c.level, c.direction)
            if ote_pos is None or not (cfg.ote_min <= ote_pos <= cfg.ote_max):
                continue

        array_hits = near_arrays(result.pd_arrays, c.level, c.direction,
                                 atr14[t], t, cfg.pda_array_tol_atr)
        if cfg.require_pda_array and not array_hits:
            continue

        row1 = d1.iloc[t]
        row4 = h4.iloc[t]
        if row1[["tenkan", "kijun", "span_a", "span_b", "macd", "macd_signal"]].isna().any():
            continue
        if row4[["close", "tenkan", "kijun", "span_a", "span_b", "macd", "macd_signal"]].isna().any():
            continue
        if i15.iloc[t][["tenkan", "kijun", "span_a", "span_b"]].isna().any():
            continue

        if c.direction == "bull":
            d1_checks = []
            if cfg.require_1d_cloud:
                d1_checks.append(row1["close"] > max(row1["span_a"], row1["span_b"]))
            if cfg.require_1d_tk:
                d1_checks.append(row1["tenkan"] > row1["kijun"])
            if cfg.require_1d_macd:
                d1_checks.append(row1["macd"] > row1["macd_signal"])
            h4_checks = []
            if cfg.require_4h_cloud:
                h4_checks.append(row4["close"] > max(row4["span_a"], row4["span_b"]))
            if cfg.require_4h_tk:
                h4_checks.append(row4["tenkan"] > row4["kijun"])
            if cfg.require_4h_macd:
                h4_checks.append(row4["macd"] > row4["macd_signal"])
            m15_checks = []
            if cfg.require_15m_cloud:
                m15_checks.append(close[t] > max(i15["span_a"].iloc[t], i15["span_b"].iloc[t]))
            if cfg.require_15m_macd:
                m15_checks.append(m15["hist"].iloc[t] > 0)
            if cfg.require_15m_hist_rising:
                m15_checks.append(t > 0 and m15["hist"].iloc[t] > m15["hist"].iloc[t - 1])
        else:
            d1_checks = []
            if cfg.require_1d_cloud:
                d1_checks.append(row1["close"] < min(row1["span_a"], row1["span_b"]))
            if cfg.require_1d_tk:
                d1_checks.append(row1["tenkan"] < row1["kijun"])
            if cfg.require_1d_macd:
                d1_checks.append(row1["macd"] < row1["macd_signal"])
            h4_checks = []
            if cfg.require_4h_cloud:
                h4_checks.append(row4["close"] < min(row4["span_a"], row4["span_b"]))
            if cfg.require_4h_tk:
                h4_checks.append(row4["tenkan"] < row4["kijun"])
            if cfg.require_4h_macd:
                h4_checks.append(row4["macd"] < row4["macd_signal"])
            m15_checks = []
            if cfg.require_15m_cloud:
                m15_checks.append(close[t] < min(i15["span_a"].iloc[t], i15["span_b"].iloc[t]))
            if cfg.require_15m_macd:
                m15_checks.append(m15["hist"].iloc[t] < 0)
            if cfg.require_15m_hist_rising:
                m15_checks.append(t > 0 and m15["hist"].iloc[t] < m15["hist"].iloc[t - 1])

        def passed(checks, min_score):
            if not checks:
                return True
            if cfg.use_score:
                return sum(bool(x) for x in checks) >= min(min_score, len(checks))
            return all(checks)

        if passed(d1_checks, cfg.min_1d_score) and passed(h4_checks, cfg.min_4h_score) and passed(m15_checks, cfg.min_15m_score):
            recent_sweeps = [
                s for s in result.sweeps
                if s.direction == c.direction and 0 <= t - s.reclaim_bar <= cfg.sweep_window
            ]
            if cfg.require_sweep and not recent_sweeps:
                continue
            sweep_strength = max((s.strength for s in recent_sweeps), default=0)
            disp = measure_displacement(df, t)
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
            if pd_pos is not None:
                quality_parts["pd"] = float(pd_pos)
            if ote_pos is not None:
                quality_parts["ote"] = float(ote_pos)
            if array_hits:
                quality_parts["pda_arrays"] = [a.kind for a in array_hits]
            target = None
            mode = cfg.target_mode
            if cfg.use_key_targets and mode == "fixed":
                mode = "hybrid"
            if mode in {"liquidity", "hybrid"}:
                levels = [k for k in result.key_levels
                          if k.known_at <= t and not k.swept and not k.broken]
                tgt = nearest_key_target(levels, c.level, c.direction == "bull")
                risk = abs(c.level - c.sl)
                if tgt is not None and risk > 0:
                    if abs(tgt - c.level) / risk >= cfg.min_target_rr:
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
            out.append(replace(c, target=target, quality=quality, quality_parts=quality_parts))
    return out


def run_one(path: str | Path, cfg: SetupConfig, daily_path: str | Path | None = None) -> tuple[list, dict]:
    df = load_csv(path)
    daily = load_csv(daily_path) if daily_path else None
    ict_cfg = ICTConfig(mode="historical", show_sessions=False, show_htf_cisd=False,
                        cisd_all=True, cisd_min_grade=0,
                        fvg_min_atr=cfg.fvg_min_atr, fvg_min_body=cfg.fvg_min_body,
                        sweep_reclaim_bars=cfg.sweep_reclaim_bars)
    res = analyze(df, ict_cfg)
    sigs = build_setup(df, res, cfg, daily=daily)
    res2 = replace(res, cisds=sigs)
    sc = StrategyConfig(rr=cfg.rr, max_bars=cfg.max_bars, min_grade=0, min_confluence=0,
                        allow_short=cfg.allow_short, risk_pct=cfg.risk_pct,
                        breakeven_at_r=cfg.breakeven_at_r,
                        t_plus_1=cfg.t_plus_1, sell_tax_bps=cfg.sell_tax_bps,
                        max_concurrent=cfg.max_concurrent,
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
    ap.add_argument("--sweep-reclaim-bars", type=int, default=3)
    ap.add_argument("--use-key-targets", action="store_true")
    ap.add_argument("--target-mode", choices=["fixed", "liquidity", "hybrid"], default="fixed")
    ap.add_argument("--entry-mode", choices=["next_open", "retest_key"], default="next_open")
    ap.add_argument("--retest-max-bars", type=int, default=20)
    ap.add_argument("--retest-tolerance-atr", type=float, default=0.0)
    ap.add_argument("--trail-activate-r", type=float, default=0.0)
    ap.add_argument("--trail-atr-mult", type=float, default=0.0)
    ap.add_argument("--max-daily-loss-r", type=float, default=0.0)
    ap.add_argument("--session-gate", choices=["none", "auto", "london", "ny_am", "ny_pm", "cn", "hk"],
                    default="none")
    ap.add_argument("--pd-gate", action="store_true")
    ap.add_argument("--ote-gate", action="store_true")
    ap.add_argument("--require-pda-array", action="store_true")
    ap.add_argument("--max-inducements", type=int, default=-1)
    ap.add_argument("--min-disp-body", type=float, default=0.0)
    ap.add_argument("--min-disp-atr", type=float, default=0.0)
    ap.add_argument("--min-disp-multiple", type=float, default=0.0)
    ap.add_argument("--min-setup-score", type=float, default=0.0)
    args = ap.parse_args()
    cfg = SetupConfig(rr=args.rr, max_bars=args.max_bars, min_grade=args.min_grade,
                      min_confluence=args.min_confluence, allow_short=args.allow_short,
                      market=args.market, breakeven_at_r=args.breakeven_at_r,
                      t_plus_1=args.t_plus_1, sell_tax_bps=args.sell_tax_bps,
                      min_1d_score=args.min_1d_score, min_4h_score=args.min_4h_score,
                      min_15m_score=args.min_15m_score, use_score=not args.strict,
                      require_sweep=not args.allow_no_sweep,
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
                      require_pda_array=args.require_pda_array,
                      max_inducements=args.max_inducements,
                      min_disp_body_ratio=args.min_disp_body,
                      min_disp_atr=args.min_disp_atr,
                      min_disp_multiple=args.min_disp_multiple,
                      min_setup_score=args.min_setup_score)
    trades, m = run_one(args.csv, cfg, daily_path=args.daily_csv)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame([t.__dict__ for t in trades]).to_csv(args.out, index=False, encoding="utf-8-sig")
    Path(args.metrics).write_text(json.dumps(m, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps(m, ensure_ascii=False, indent=2, default=str))
    print(f"trades: {args.out}")


if __name__ == "__main__":
    main()


