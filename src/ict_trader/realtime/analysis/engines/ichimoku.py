"""Ichimoku engine adapter.

Rules implemented on a *closed* base bar:
  * Tenkan/Kijun cross on the just-closed bar;
  * price on the correct side of the cloud;
  * Chikou confirmation (`close > close.shift(kijun)` / inverse);
  * cloud thickness filter in ATR units;
  * optional higher-timeframe cloud + TK alignment.

Higher-timeframe data is resampled from the same base window and passed through
``mark_htf_known_at``; a forming 4H/1D bucket is never visible.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from ....concepts import atr
from ...models import dt_to_ms
from ..contracts import AnalysisContext, EngineSignal, PriceZone


@dataclass(slots=True)
class IchimokuConfig:
    tenkan: int = 9
    kijun: int = 26
    senkou: int = 52
    htf_interval: str = "4h"
    require_htf: bool = True
    require_chikou: bool = True
    min_cloud_atr: float = 0.15
    rr: float = 2.0
    min_confidence: float = 0.55


@dataclass(slots=True)
class IchimokuEngine:
    config: IchimokuConfig = field(default_factory=IchimokuConfig)
    engine_id: str = "ichimoku"
    version: str = "ichimoku-1.0.0"
    warmup_bars: int = 1200

    def analyze(self, window: pd.DataFrame,
                context: AnalysisContext) -> list[EngineSignal]:
        if not isinstance(window, pd.DataFrame):
            raise TypeError("IchimokuEngine expects a pandas DataFrame")
        if len(window) < max(self.config.senkou * 2, 120):
            return []
        df = window.copy()
        for col in ("open", "high", "low", "close", "volume"):
            if col not in df:
                raise ValueError(f"missing OHLCV column: {col}")
            df[col] = df[col].astype(float)
        df["date"] = pd.to_datetime(df["date"], utc=True)

        ind = self._ichimoku(df, df)
        if len(ind) < 2:
            return []
        signal = self._evaluate(df, ind, context)
        return [signal] if signal is not None else []

    # ------------------------------------------------------------------
    def _ichimoku(self, frame: pd.DataFrame, out: pd.DataFrame) -> pd.DataFrame:
        high = frame["high"].astype(float)
        low = frame["low"].astype(float)
        close = frame["close"].astype(float)
        cfg = self.config
        conv = (high.rolling(cfg.tenkan).max() + low.rolling(cfg.tenkan).min()) / 2
        base = (high.rolling(cfg.kijun).max() + low.rolling(cfg.kijun).min()) / 2
        span_a = ((conv + base) / 2).shift(cfg.kijun)
        span_b = ((high.rolling(cfg.senkou).max() + low.rolling(cfg.senkou).min()) / 2).shift(cfg.kijun)
        result = pd.DataFrame(index=out.index)
        result["tenkan"] = conv
        result["kijun"] = base
        result["span_a"] = span_a
        result["span_b"] = span_b
        result["cloud_top"] = result[["span_a", "span_b"]].max(axis=1)
        result["cloud_bottom"] = result[["span_a", "span_b"]].min(axis=1)
        result["chikou_ok"] = close > close.shift(cfg.kijun)
        result["chikou_ok_short"] = close < close.shift(cfg.kijun)
        return result

    def _htf_frame(self, df: pd.DataFrame) -> pd.DataFrame | None:
        cfg = self.config
        if not cfg.require_htf:
            return None
        x = df.copy()
        x["date"] = pd.to_datetime(x["date"], utc=True)
        x = x.set_index("date")
        out = x.resample(cfg.htf_interval, label="left", closed="left").agg(
            {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
        ).dropna(subset=["open", "high", "low", "close"])
        if len(out) and len(df):
            step = pd.tseries.frequencies.to_offset(cfg.htf_interval)
            last_base = pd.Timestamp(df["date"].iloc[-1])
            if pd.Timestamp(out.index[-1]) + step > last_base:
                out = out.iloc[:-1]
        out = out.reset_index()
        if len(out) < cfg.senkou + cfg.kijun:
            return None
        h = self._ichimoku(out, out)
        h["date"] = out["date"].to_numpy()
        h["close"] = out["close"].to_numpy()
        # close-time labels = known_at; the last row is only usable after close.
        h["date"] = pd.to_datetime(h["date"], utc=True) + pd.tseries.frequencies.to_offset(cfg.htf_interval)
        return h

    def _evaluate(self, df: pd.DataFrame, ind: pd.DataFrame,
                  context: AnalysisContext) -> EngineSignal | None:
        cfg = self.config
        last = ind.iloc[-1]
        prev = ind.iloc[-2]
        required = ("tenkan", "kijun", "span_a", "span_b", "cloud_top", "cloud_bottom")
        if any(pd.isna(last[c]) for c in required) or any(pd.isna(prev[c]) for c in ("tenkan", "kijun")):
            return None
        close = float(df["close"].iloc[-1])
        high = float(df["high"].iloc[-1])
        low = float(df["low"].iloc[-1])
        atr_value = float(atr(df, 14).iloc[-1])
        if not np.isfinite(atr_value) or atr_value <= 0:
            return None
        cloud_top = float(last["cloud_top"])
        cloud_bottom = float(last["cloud_bottom"])
        thickness_atr = (cloud_top - cloud_bottom) / atr_value
        if thickness_atr < cfg.min_cloud_atr:
            return None

        prev_diff = float(prev["tenkan"] - prev["kijun"])
        curr_diff = float(last["tenkan"] - last["kijun"])
        cross_up = prev_diff <= 0 < curr_diff
        cross_down = prev_diff >= 0 > curr_diff
        if not (cross_up or cross_down):
            return None
        long = cross_up
        if long:
            if not (close > cloud_top and close > float(last["tenkan"]) > float(last["kijun"])):
                return None
            if cfg.require_chikou and not bool(last["chikou_ok"]):
                return None
        else:
            if not (close < cloud_bottom and close < float(last["tenkan"]) < float(last["kijun"])):
                return None
            if cfg.require_chikou and not bool(last["chikou_ok_short"]):
                return None

        htf_last = None
        htf = self._htf_frame(df)
        if htf is not None:
            usable = htf[pd.to_datetime(htf["date"], utc=True) <= pd.Timestamp(df["date"].iloc[-1])]
            if usable.empty:
                return None
            htf_last = usable.iloc[-1]
            if long and not (
                float(htf_last["close"]) > float(htf_last["cloud_top"])
                and float(htf_last["tenkan"]) > float(htf_last["kijun"])
            ):
                return None
            if not long and not (
                float(htf_last["close"]) < float(htf_last["cloud_bottom"])
                and float(htf_last["tenkan"]) < float(htf_last["kijun"])
            ):
                return None
        elif cfg.require_htf:
            return None

        invalidation = (cloud_bottom - 0.15 * atr_value) if long else (cloud_top + 0.15 * atr_value)
        risk = abs(close - invalidation)
        if risk <= 0:
            return None
        target = close + cfg.rr * risk if long else close - cfg.rr * risk
        confidence = 0.45 + 0.15 + 0.10 + min(0.15, thickness_atr * 0.05) + (0.10 if htf_last is not None else 0.0)
        if cfg.require_chikou:
            confidence += 0.05
        confidence = max(0.0, min(1.0, confidence))
        if confidence < cfg.min_confidence:
            return None
        zone_pad = 0.10 * atr_value
        evidence = {
            "tenkan": float(last["tenkan"]),
            "kijun": float(last["kijun"]),
            "span_a": float(last["span_a"]),
            "span_b": float(last["span_b"]),
            "cloud_top": cloud_top,
            "cloud_bottom": cloud_bottom,
            "cloud_thickness_atr": round(thickness_atr, 4),
            "chikou_ok": bool(last["chikou_ok"] if long else last["chikou_ok_short"]),
            "htf_interval": cfg.htf_interval,
            "htf_close": None if htf_last is None else float(htf_last["close"]),
            "htf_cloud_top": None if htf_last is None else float(htf_last["cloud_top"]),
            "htf_cloud_bottom": None if htf_last is None else float(htf_last["cloud_bottom"]),
            "atr": atr_value,
            "entry": close,
            "stop": invalidation,
            "target": target,
            "rr": cfg.rr,
            "thesis": (
                f"Ichimoku TK {'golden' if long else 'dead'} cross · "
                f"{cfg.htf_interval} {'bull' if long else 'bear'} alignment"
            ),
        }
        return EngineSignal(
            engine=self.engine_id,
            symbol=context.symbol,
            interval=context.interval,
            direction="long" if long else "short",
            signal_type="ichimoku_tk_cross",
            trigger_price_zone=PriceZone(max(1e-12, close - zone_pad), close + zone_pad),
            invalidation_price=invalidation,
            confidence=confidence,
            source_kline_open_time=dt_to_ms(pd.Timestamp(df["date"].iloc[-1]).to_pydatetime()),
            engine_version=self.version,
            evidence=evidence,
        )
