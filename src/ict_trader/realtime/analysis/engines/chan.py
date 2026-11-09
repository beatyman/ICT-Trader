"""Chanlun / chan.py engine adapter.

The upstream ``analyze_chanlun`` is optional (chan.py is vendored, czsc is a
fallback).  This adapter treats Chan as a *structure confirmation* engine: it
only emits when the last closed bar has a non-neutral bias and an explicit
buy/sell point.  Missing chan.py is a normal ``no signal`` result, not a
process crash.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

from ....chanlun import ChanlunResult, analyze_chanlun
from ....concepts import atr
from ...models import dt_to_ms
from ..contracts import AnalysisContext, EngineSignal, PriceZone


@dataclass(slots=True)
class ChanConfig:
    freq: str = "15m"
    rr: float = 2.0
    min_confidence: float = 0.55
    max_bars: int = 600


@dataclass(slots=True)
class ChanEngine:
    config: ChanConfig
    engine_id: str = "chan"
    version: str = "chan-1.0.0"
    warmup_bars: int = 120

    def analyze(self, window: pd.DataFrame,
                context: AnalysisContext) -> list[EngineSignal]:
        if not isinstance(window, pd.DataFrame):
            raise TypeError("ChanEngine expects a pandas DataFrame")
        if len(window) < 30:
            return []
        df = window.iloc[-self.config.max_bars:].copy() if len(window) > self.config.max_bars else window.copy()
        for col in ("open", "high", "low", "close", "volume"):
            if col not in df:
                raise ValueError(f"missing OHLCV column: {col}")
            df[col] = df[col].astype(float)
        try:
            result = analyze_chanlun(df, symbol=context.symbol, freq=self.config.freq)
        except Exception:
            # Optional dependency unavailable: a shadow engine must not take
            # down the strategy loop.
            return []
        sig = self._build(df, result, context)
        return [sig] if sig is not None else []

    def _build(self, df: pd.DataFrame, result: ChanlunResult,
               context: AnalysisContext) -> EngineSignal | None:
        bsp = result.bsp_type or ""
        if bsp in {"1买", "2买", "3买"}:
            bsp_direction = "long"
        elif bsp in {"1卖", "2卖", "3卖"}:
            bsp_direction = "short"
        else:
            return None
        # chan.py can expose a stale `bias` while the newest BSP is opposite.
        # Refuse the contradiction instead of emitting a wrong-side setup.
        if result.bias != bsp_direction:
            return None
        long = bsp_direction == "long"
        entry = float(df["close"].iloc[-1])
        a = float(atr(df, 14).iloc[-1])
        if not math.isfinite(a) or a <= 0:
            return None
        if long:
            stop = result.last_zs_zd if result.last_zs_zd is not None else float(df["low"].iloc[-20:].min()) - 0.25 * a
            if not math.isfinite(float(stop)) or float(stop) >= entry:
                stop = entry - 1.2 * a
        else:
            stop = result.last_zs_zg if result.last_zs_zg is not None else float(df["high"].iloc[-20:].max()) + 0.25 * a
            if not math.isfinite(float(stop)) or float(stop) <= entry:
                stop = entry + 1.2 * a
        risk = abs(entry - stop)
        if risk <= 0:
            return None
        target = entry + self.config.rr * risk if long else entry - self.config.rr * risk
        confidence = 0.55 + (0.10 if result.bsp_count >= 2 else 0.0)
        confidence = max(0.0, min(1.0, confidence))
        if confidence < self.config.min_confidence:
            return None
        evidence = {
            "entry": entry,
            "stop": float(stop),
            "target": target,
            "rr": self.config.rr,
            "bias": result.bias,
            "bsp_type": result.bsp_type,
            "bsp_count": int(result.bsp_count),
            "position": result.position,
            "bi_count": int(result.bi_count),
            "seg_count": int(result.seg_count),
            "zs_count": int(result.zs_count),
            "last_zs_zd": result.last_zs_zd,
            "last_zs_zg": result.last_zs_zg,
            "reasons": list(result.reasons),
            "thesis": f"Chan {result.bsp_type} · {result.position} · bias {result.bias}",
        }
        return EngineSignal(
            engine=self.engine_id,
            symbol=context.symbol,
            interval=context.interval,
            direction="long" if long else "short",
            signal_type="chan_bsp",
            trigger_price_zone=PriceZone(
                max(1e-12, entry - 0.05 * risk), entry + 0.05 * risk,
            ),
            invalidation_price=float(stop),
            confidence=confidence,
            source_kline_open_time=dt_to_ms(
                pd.Timestamp(df["date"].iloc[-1]).to_pydatetime()
            ),
            engine_version=self.version,
            evidence=evidence,
        )
