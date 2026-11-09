"""Wyckoff engine adapter.

Wraps the existing non-repainting ``detect_wyckoff`` state machine.  Only an
event whose trigger bar is the just-closed bar is actionable; historical phase
changes are never emitted by the live adapter.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

from ....wyckoff import WyckoffSignal, detect_wyckoff
from ...models import dt_to_ms
from ..contracts import AnalysisContext, EngineSignal, PriceZone

KIND_CONFIDENCE = {
    "spring": 0.72, "utad": 0.72,
    "sos": 0.68, "sow": 0.68,
    "lps": 0.62, "lpsy": 0.62,
}


@dataclass(slots=True)
class WyckoffConfig:
    trend_len: int = 50
    rr: float = 2.0
    min_confidence: float = 0.55


@dataclass(slots=True)
class WyckoffEngine:
    config: WyckoffConfig
    engine_id: str = "wyckoff"
    version: str = "wyckoff-1.0.0"
    warmup_bars: int = 300

    def analyze(self, window: pd.DataFrame,
                context: AnalysisContext) -> list[EngineSignal]:
        if not isinstance(window, pd.DataFrame):
            raise TypeError("WyckoffEngine expects a pandas DataFrame")
        if len(window) < 80:
            return []
        df = window.copy()
        for col in ("open", "high", "low", "close", "volume"):
            if col not in df:
                raise ValueError(f"missing OHLCV column: {col}")
            df[col] = df[col].astype(float)
        state, _ = detect_wyckoff(df, trend_len=self.config.trend_len)
        n = len(df)
        out: list[EngineSignal] = []
        for sig in state.signals:
            if int(sig.bar) != n - 1:
                continue
            built = self._build(df, state, sig, context)
            if built is not None:
                out.append(built)
        return out

    def _build(self, df: pd.DataFrame, state, sig: WyckoffSignal,
               context: AnalysisContext) -> EngineSignal | None:
        long = sig.direction == "long"
        entry = float(sig.entry_price)
        if not math.isfinite(entry) or entry <= 0:
            entry = float(df["close"].iloc[sig.bar])
        stop = float(sig.spring_extreme) if math.isfinite(float(sig.spring_extreme)) else float(sig.climax_side)
        if not math.isfinite(stop):
            stop = float(sig.range_bottom) if long else float(sig.range_top)
        if not math.isfinite(stop) or (long and stop >= entry) or (not long and stop <= entry):
            return None
        risk = abs(entry - stop)
        if risk <= 0:
            return None
        target = float(state.t1) if math.isfinite(float(state.t1)) else 0.0
        if (long and target <= entry) or (not long and (target <= 0 or target >= entry)):
            target = entry + self.config.rr * risk if long else entry - self.config.rr * risk
        confidence = KIND_CONFIDENCE.get(str(sig.kind), 0.55)
        if state.phase in {"D", "E"}:
            confidence += 0.04
        confidence = max(0.0, min(1.0, confidence))
        if confidence < self.config.min_confidence:
            return None
        evidence = {
            "entry": entry,
            "stop": stop,
            "target": target,
            "rr": abs(target - entry) / risk,
            "kind": str(sig.kind),
            "phase": str(state.phase),
            "stage": int(state.stage),
            "range_top": _float(sig.range_top),
            "range_bottom": _float(sig.range_bottom),
            "creek_ice": _float(sig.creek_ice),
            "climax_side": _float(sig.climax_side),
            "spring_extreme": _float(sig.spring_extreme),
            "t1": _float(state.t1),
            "t2": _float(state.t2),
            "thesis": f"Wyckoff {sig.kind} · phase {state.phase or '?'}",
        }
        return EngineSignal(
            engine=self.engine_id,
            symbol=context.symbol,
            interval=context.interval,
            direction="long" if long else "short",
            signal_type=f"wyckoff_{sig.kind}",
            trigger_price_zone=PriceZone(
                max(1e-12, entry - 0.05 * risk),
                entry + 0.05 * risk,
            ),
            invalidation_price=stop,
            confidence=confidence,
            source_kline_open_time=dt_to_ms(
                pd.Timestamp(df["date"].iloc[sig.bar]).to_pydatetime()
            ),
            engine_version=self.version,
            evidence=evidence,
        )


def _float(value) -> float | None:
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None
