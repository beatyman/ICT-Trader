"""ICT-Trader adapter: CISD setup -> immutable EngineSignal.

The research engine remains the source of truth.  This class is a thin mapping
layer: it does not reimplement ICT concepts, and it never writes to Redis,
ClickHouse, the broker or the AI queue.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from numbers import Real
from typing import Any

import numpy as np
import pandas as pd

from ....concepts import ICTConfig, analyze
from ....context import dealing_range as ctx_dealing_range
from ....setup import SetupConfig, build_setup
from ...overlays import build_ict_overlays
from ...models import dt_to_ms
from ..contracts import AnalysisContext, EngineSignal, PriceZone
from ..dealing_range import DealingRange as AnalysisDealingRange
from ..evidence import (
    breaker_evidence, displacement_evidence, extract_sweep, htf_bias_evidence,
    hidden_ob_evidence, ifvg_evidence, pd_array_evidence,
    rvol_evidence, structure_evidence,
)

ENGINE_VERSION = "ict_trader-0.1.0"

#: §12 refusal codes for the §8.1 structure chain.  The V25 detector is a
#: P1-07 object, so a setup whose displacement conjunct is absent must fail
#: closed instead of treating the unmeasured conjunct as true (DP-07).  A
#: structure print that is not the required MSS-after-sweep is likewise a
#: refusal, never a satisfied conjunct (P0-17).
NT_NO_DISPLACEMENT = "NT_NO_DISPLACEMENT"
NT_NO_SHIFT = "NT_NO_SHIFT"
#: Backwards-compatible short aliases used by the engine internals.
NO_DISPLACEMENT = NT_NO_DISPLACEMENT
NO_SHIFT = NT_NO_SHIFT


def _plain(value: Any) -> Any:
    if isinstance(value, (np.floating, np.integer)):
        return value.item()
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, Real):
        return value
    return value


def _bar_index(times: list[int], at_ms: int) -> int | None:
    """Index of the bar whose open time is ``at_ms`` (``None`` if not found)."""
    try:
        return times.index(int(at_ms))
    except (ValueError, TypeError):
        return None


@dataclass(slots=True)
class ICTEngine:
    setup_config: SetupConfig
    ict_config: ICTConfig
    engine_id: str = "ict"
    version: str = ENGINE_VERSION
    warmup_bars: int = 800
    rule_mode: bool = False
    #: Refusals from the last scan, one dict per rejected setup
    #: (``{"code", "direction", "end", "data_gaps"}``).  An empty signal list
    #: alone is not a passing refusal (A3); this is where the §12 code lives.
    refusals: list[dict[str, Any]] = field(default_factory=list)

    def analyze(self, window: pd.DataFrame,
                context: AnalysisContext) -> list[EngineSignal]:
        """Return only setups confirmed by the last closed bar."""
        return self._scan(window, actionable_only=True, context=context)

    def scan_history(self, window: pd.DataFrame,
                     context: AnalysisContext | None = None) -> list[EngineSignal]:
        """Return historical setups for the dashboard/backfill path."""
        return self._scan(window, actionable_only=False, context=context)

    def _scan(self, window: pd.DataFrame, *, actionable_only: bool,
              context: AnalysisContext | None) -> list[EngineSignal]:
        self.refusals = []
        if not isinstance(window, pd.DataFrame):
            raise TypeError("ICTEngine expects a pandas DataFrame")
        if len(window) < 100:
            return []
        frame = window.copy()
        for column in ("open", "high", "low", "close", "volume"):
            if column not in frame:
                raise ValueError(f"missing OHLCV column: {column}")
            frame[column] = frame[column].astype(float)
        result = analyze(frame, self.ict_config)
        n = len(frame)
        if actionable_only:
            recent = [c for c in result.cisds if int(c.end) >= n - 3]
            result = replace(result, cisds=recent)
        setups = build_setup(frame, result, self.setup_config)
        if actionable_only:
            setups = [c for c in setups if int(c.end) == n - 1]
        else:
            setups = setups[-60:]
        close = frame["close"].to_numpy(float)
        symbol = (context.symbol if context else frame.attrs.get("symbol", ""))
        interval = (context.interval if context else frame.attrs.get("interval", "15m"))
        # corpus context, computed once per scan rather than per setup
        dr_ctx = ctx_dealing_range(result.swings, n - 1)
        dr_analysis = (AnalysisDealingRange(float(dr_ctx.high), float(dr_ctx.low))
                       if dr_ctx is not None else None)
        hidden_obs = hidden_ob_evidence(frame, n - 1)
        out: list[EngineSignal] = []
        for c in setups:
            idx = int(c.end)
            if not 0 <= idx < n:
                continue
            entry = float(close[idx])
            stop = float(c.sl)
            risk = abs(entry - stop)
            if risk <= 0:
                continue
            target = float(c.target) if c.target is not None else (
                entry + self.setup_config.rr * risk
                if c.direction == "bull" else entry - self.setup_config.rr * risk
            )
            if c.direction == "bull" and not (stop < entry < target):
                continue
            if c.direction == "bear" and not (target < entry < stop):
                continue
            rr = abs(target - entry) / risk
            if rr < 1.0:
                continue
            zone_low, zone_high = sorted((entry, float(c.level)))
            overlay_evidence = build_ict_overlays(frame)
            source_ms = int(pd.Timestamp(frame["date"].iloc[idx]).timestamp() * 1000)
            times = [int(pd.Timestamp(x).timestamp() * 1000) for x in frame["date"]]
            direction = "long" if c.direction == "bull" else "short"
            sweep = extract_sweep(
                overlay_evidence.get("sweeps", []),
                direction=direction,
                source_kline_open_time=source_ms,
            )
            # §8.1: `structure` means the MSS *after the sweep*, never "any
            # BOS"; the sweep's reclaim bar is the lower bound.
            sweep_bar = (_bar_index(times, int(sweep["time"]))
                         if sweep is not None else None)
            structure = structure_evidence(
                result.events, idx, times,
                direction=direction, after_index=sweep_bar,
            )
            displacement = displacement_evidence(c.quality_parts)
            breakers = breaker_evidence(result.pd_arrays, idx)
            pd_arrays = pd_array_evidence(result, idx)
            htf = htf_bias_evidence(frame, idx)
            rvol = rvol_evidence(frame, idx)
            data_gaps = []
            if sweep is None:
                data_gaps.append("sweep")
            if structure is None:
                data_gaps.append("mss")
            if displacement is None:
                data_gaps.append("displacement")
            if htf is None or any(v is None for v in htf.values()):
                data_gaps.append("htf_bias")
            if rvol is None:
                data_gaps.append("rvol")
            if displacement is None:
                # P1-07 owns the V25 displacement detector.  Until it exists an
                # unmeasured conjunct is unknown, not satisfied, so the setup
                # must be refused instead of silently admitted (DP-07).
                self.refusals.append({
                    "code": NO_DISPLACEMENT,
                    "direction": direction,
                    "end": idx,
                    "data_gaps": sorted(set(data_gaps)),
                })
                continue
            if structure is None and result.events:
                # A structure print exists, but it is a BOS (or an MSS in the
                # wrong direction / before the sweep), not the MSS the §8.1
                # conjunction requires.  Admitting it would repeat the defect.
                self.refusals.append({
                    "code": NO_SHIFT,
                    "direction": direction,
                    "end": idx,
                    "data_gaps": sorted(set(data_gaps)),
                })
                continue
            evidence = {
                "entry": entry,
                "stop": stop,
                "target": target,
                "rr": rr,
                "grade": int(c.grade),
                "quality": float(c.quality),
                "checks": _plain(dict(c.checks or {})),
                "quality_parts": _plain(dict(c.quality_parts or {})),
                "setup_level": float(c.level),
                "thesis": self._thesis(c),
                "ict_overlays": overlay_evidence,
                "sweep": sweep,
                "structure": structure,
                "mss": structure,
                "displacement": displacement,
                "breaker": breakers,
                "fvg": pd_arrays["fvg"],
                "ifvg": pd_arrays["ifvg"],
                "ifvg_context": ifvg_evidence(pd_arrays["ifvg"], dr_analysis),
                "hidden_ob": hidden_obs,
                "dealing_range": (
                    {"high": float(dr_analysis.high), "low": float(dr_analysis.low),
                     "ce": float(dr_analysis.ce), "zone": dr_analysis.zone(entry)}
                    if dr_analysis is not None else None
                ),
                "ob": pd_arrays["ob"],
                "pd_array": pd_arrays["pd_array"],
                "htf_bias": htf,
                "rvol": rvol,
                "data_gaps": sorted(set(data_gaps)),
            }
            out.append(EngineSignal(
                engine=self.engine_id,
                symbol=symbol,
                interval=interval,
                direction="long" if c.direction == "bull" else "short",
                signal_type="ict_cisd",
                trigger_price_zone=PriceZone(zone_low, zone_high),
                invalidation_price=stop,
                confidence=max(0.0, min(1.0, float(c.quality))),
                source_kline_open_time=source_ms,
                engine_version=self.version,
                evidence=evidence,
            ))
        if self.rule_mode and actionable_only:
            from ..candidate_adapter import to_engine_signal
            from ..candidate_builder import build_candidates
            source_ms = int(pd.Timestamp(frame["date"].iloc[-1]).timestamp() * 1000)
            candidates = build_candidates(
                frame, result, symbol=symbol, now=pd.Timestamp(frame["date"].iloc[-1]).to_pydatetime(),
            )
            return [to_engine_signal(
                c, symbol=symbol, interval=interval,
                source_kline_open_time=source_ms,
                engine_version=self.version,
            ) for c in candidates]
        return out

    @staticmethod
    def _thesis(c) -> str:
        checks = c.checks or {}
        passed = [k for k in ("sweep", "pda", "volume", "session", "targets", "smt")
                  if checks.get(k)]
        return (
            f"CISD {c.direction} · grade {c.grade} · "
            f"{' / '.join(passed) if passed else 'base confluence'}"
        )
