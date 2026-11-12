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
from ....context import dealing_range_state as ctx_dealing_range_state
from ....setup import SetupConfig, build_setup
from ...overlays import build_ict_overlays
from ...models import dt_to_ms
from ..contracts import AnalysisContext, EngineSignal, PriceZone
from ..dealing_range import DealingRange as AnalysisDealingRange
from ..evidence import (
    NT_DATA_MISSING, NT_NO_DISPLACEMENT, NT_NO_SHIFT, NT_SHIFT_UNCONFIRMED,
    DisplacementSpec, breaker_evidence, default_displacement_spec,
    displacement_evidence, evaluate_displacement, extract_sweep,
    htf_bias_evidence, hidden_ob_evidence, ifvg_evidence, last_bar_closed,
    pd_array_evidence, rvol_evidence, structure_evidence,
    unconfirmed_shift_evidence,
)

ENGINE_VERSION = "ict_trader-0.1.0"

#: RE:RM-15 / V28: ``strategy.RR_MODE_FLOORS`` declares four gross-RR floors
#: (3.0 / 1.5 / 2.0 / 0.0).  **None of them is 1.0**, so the engine's former
#: ``if rr < 1.0`` was a private, undeclared floor that appeared in no table.
#: P0-20 declares it as the engine's own ``repo`` admission mode instead of
#: borrowing a corpus number; the V28 register itself is P1-13.
ENGINE_RR_MODES = ("floor_1_0", "none")
DEFAULT_ENGINE_RR_MODE = "floor_1_0"
ENGINE_RR_FLOORS: dict[str, float] = {"floor_1_0": 1.0, "none": 0.0}
ENGINE_RR_MODE_SOURCES: dict[str, dict[str, str]] = {
    "floor_1_0": {
        "label": "repo",
        "v28": "outside V28",
        "source": "engine admission floor; 1.0 is not one of V28's four floors",
    },
    "none": {
        "label": "repo",
        "v28": "outside V28",
        "source": "declared absence of the engine admission floor",
    },
}


def engine_rr_floor(mode: str = DEFAULT_ENGINE_RR_MODE) -> float:
    """The declared gross-RR floor for ``mode`` (ValueError if undeclared)."""
    try:
        return ENGINE_RR_FLOORS[mode]
    except KeyError as exc:
        raise ValueError(
            f"unknown engine rr_mode {mode!r}; declare one of {ENGINE_RR_MODES}"
        ) from exc


#: §12 refusal codes for the §8.1 structure chain.  The one typed vocabulary
#: lives in :mod:`ict_trader.variants`; this module re-exports the two names the
#: previous engine declared and keeps the short internal aliases (P1-07).
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
    #: Declared engine gross-RR admission mode (P0-20).  The old literal 1.0 is
    #: now named and validated; ``none`` removes the engine floor (the setup /
    #: strategy gates still apply their own declared floors).
    rr_mode: str = DEFAULT_ENGINE_RR_MODE
    #: Declared V25 displacement detector (P1-07).  The default is the
    #: **labelled repo** form: the P0-17 measured-body conjunct, which grades
    #: magnitude and bounds none (DP-04).  A run that declares a bounded V25
    #: form and omits a magnitude is refused with ``NT_NO_DISPLACEMENT``.
    displacement_spec: DisplacementSpec = field(
        default_factory=default_displacement_spec)
    #: CB-01/CB-10: the last scan evaluated a closed bar (``True``) or refused
    #: because the frame's last bar was still forming at ``context.as_of_ms``.
    last_bar_closed: bool = True
    #: Refusals from the last scan, one dict per rejected setup
    #: (``{"code", "direction", "end", "data_gaps"}``).  An empty signal list
    #: alone is not a passing refusal (A3); this is where the §12 code lives.
    refusals: list[dict[str, Any]] = field(default_factory=list)
    #: Why the last scan has no structural dealing range, or ``None`` when it
    #: has one.  P0-16: a stale (120-bar) range is a diagnosis, not a silent
    #: ``None``; it survives even when the scan emits no signal.
    range_diagnostic: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        engine_rr_floor(self.rr_mode)  # validate the declaration early

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
        self.range_diagnostic = None
        self.last_bar_closed = True
        if not isinstance(window, pd.DataFrame):
            raise TypeError("ICTEngine expects a pandas DataFrame")
        if len(window) < 100:
            return []
        frame = window.copy()
        for column in ("open", "high", "low", "close", "volume"):
            if column not in frame:
                raise ValueError(f"missing OHLCV column: {column}")
            frame[column] = frame[column].astype(float)
        n = len(frame)
        # CB-01/CB-10 / RE:EX-01 ``window_closed_bar``: the admission guard is
        # explicit here rather than delegated to the caller.  A bar whose close
        # has not printed at the evaluation instant is never evaluated (F09).
        if not last_bar_closed(
                frame,
                as_of_ms=(int(context.as_of_ms) if context is not None else None),
                interval=(context.interval if context is not None
                          else frame.attrs.get("interval"))):
            self.last_bar_closed = False
            self.refusals.append({
                "code": NT_DATA_MISSING,
                "direction": "",
                "end": n - 1,
                "data_gaps": ["closed_bar"],
                "detail": ("CB-01/CB-10: the last bar has not closed at "
                           "as_of_ms; no predicate may read a forming bar"),
            })
            return []
        result = analyze(frame, self.ict_config)
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
        dr_ctx, dr_diagnostic = ctx_dealing_range_state(result.swings, n - 1)
        self.range_diagnostic = (dr_diagnostic.to_public()
                                 if dr_diagnostic is not None else None)
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
            if rr < engine_rr_floor(self.rr_mode):
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
            # ST-08: a CHoCH that has not yet been confirmed by the first BOS in
            # the new direction is a warning, not a shift.  It is refused with
            # its own §12 code instead of being reported as a missing MSS.
            unconfirmed = unconfirmed_shift_evidence(
                result.events, idx, times,
                direction=direction, after_index=sweep_bar,
            )
            # P1-07: the DP1 conjunct is now the **declared V25 detector**, not
            # "any body measurement".  ``displacement`` stays the raw measured
            # payload that older readers consume; ``displacement_gate`` carries
            # the declaration, form and verdict.
            displacement = displacement_evidence(c.quality_parts)
            displacement_gate = evaluate_displacement(
                c.quality_parts, self.displacement_spec)
            breakers = breaker_evidence(result.pd_arrays, idx)
            pd_arrays = pd_array_evidence(result, idx)
            htf = htf_bias_evidence(frame, idx)
            rvol = rvol_evidence(frame, idx)
            data_gaps = []
            if sweep is None:
                data_gaps.append("sweep")
            if structure is None:
                data_gaps.append("mss")
            if displacement is None or not displacement_gate["ok"]:
                data_gaps.append("displacement")
            if htf is None or any(v is None for v in htf.values()):
                data_gaps.append("htf_bias")
            if rvol is None:
                data_gaps.append("rvol")
            if not displacement_gate["ok"]:
                # DP-07: no displacement means take nothing.  The gate's own
                # §12 code is used, so an undeclared V25 is not misreported as a
                # missing measurement.
                self.refusals.append({
                    "code": displacement_gate.get("code") or NO_DISPLACEMENT,
                    "direction": direction,
                    "end": idx,
                    "data_gaps": sorted(set(data_gaps)),
                    "detail": str(displacement_gate.get("reason", "")),
                })
                continue
            if structure is None and unconfirmed is not None:
                self.refusals.append({
                    "code": NT_SHIFT_UNCONFIRMED,
                    "direction": direction,
                    "end": idx,
                    "data_gaps": sorted(set(data_gaps)),
                    "detail": ("ST-08: the CHoCH is unconfirmed until the first "
                               "BOS in the new direction"),
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
                # §8.1 conjunction: the emitted evidence carries the MSS and the
                # declared displacement verdict as separate, inspectable keys
                # (P0-17/P1-07).  ``mss`` and ``displacement`` are the payloads;
                # ``mss_confirmed``/``displacement_gate`` are their states.
                "structure": structure,
                "mss": structure,
                "mss_confirmed": structure is not None,
                "unconfirmed_shift": unconfirmed,
                "displacement": displacement,
                "displacement_gate": displacement_gate,
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
                # P0-16: an absent/stale range carries its §12-style reason
                # (NT_RANGE_INVALID) instead of being an unexplained None.
                "dealing_range_diagnostic": (
                    dict(self.range_diagnostic)
                    if self.range_diagnostic is not None else None
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
