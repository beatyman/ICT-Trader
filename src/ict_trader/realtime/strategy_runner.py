"""Realtime adapter around the frozen ICT-Trader setup.

The research engine is intentionally reused as-is:
``analyze()`` -> ``build_setup()`` on a rolling closed-bar DataFrame.  The
adapter adds only the live concerns:

* evaluate once per **closed** strategy-interval bar, never on a tick;
* run CPU-heavy analysis in a worker thread;
* expose only a setup whose confirmation bar is the bar that just closed;
* keep a stable signal id so a retry/replay cannot execute the same setup twice.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import time
from collections import deque
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

import numpy as np
import pandas as pd

from ..concepts import ICTConfig, analyze
from ..setup import SetupConfig, build_setup
from .config import Settings
from .metrics import Metrics
from .models import Kline, interval_ms, ms_to_datetime, utc_ms

log = logging.getLogger(__name__)


@dataclass
class TradeSignal:
    id: str
    symbol: str
    interval: str
    bar_time: int
    created_at: int
    direction: str                 # long | short
    entry: float
    stop: float
    target: float
    rr: float
    grade: int
    quality: float
    status: str                    # ARMED | EXECUTED | REJECTED | HISTORICAL | CLOSED
    actionable: bool
    executed: bool
    reason: str = ""
    checks: dict[str, bool] = field(default_factory=dict)
    quality_parts: dict[str, Any] = field(default_factory=dict)
    setup_level: float = 0.0
    order_id: str = ""

    @property
    def bar_time_dt(self) -> datetime:
        return ms_to_datetime(self.bar_time)

    @property
    def created_at_dt(self) -> datetime:
        return ms_to_datetime(self.created_at)

    def to_public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "symbol": self.symbol,
            "interval": self.interval,
            "bar_time": self.bar_time,
            "created_at": self.created_at,
            "direction": self.direction,
            "entry": round(self.entry, 8),
            "stop": round(self.stop, 8),
            "target": round(self.target, 8),
            "rr": round(self.rr, 2),
            "grade": self.grade,
            "quality": round(self.quality, 3),
            "status": self.status,
            "actionable": self.actionable,
            "executed": self.executed,
            "reason": self.reason,
            "checks": self.checks,
            "quality_parts": self.quality_parts,
            "setup_level": round(self.setup_level, 8),
            "order_id": self.order_id,
        }


def _jsonable(value: Any) -> Any:
    if isinstance(value, (float, np.floating)):
        number = float(value)
        return number if math.isfinite(number) else None
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    if isinstance(value, np.integer):
        return value.item()
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return str(value)


class ICTStrategyRunner:
    def __init__(self, settings: Settings, metrics: Metrics) -> None:
        self.settings = settings
        self.metrics = metrics
        self.interval = settings.strategy_interval
        # One closed 4H cloud needs 52*4H ~= 832 15m bars; 2400 leaves a wide
        # buffer and cuts per-close CPU by more than half. 1D cloud needs
        # 52+26=78 daily bars -> 8000 15m bars with headroom.
        auto_window = 8000 if "1d" in settings.stack.lower() else 1800
        self.eval_window = max(400, int(settings.strategy_eval_window or auto_window))
        self.histories: dict[str, deque[Kline]] = {
            s: deque(maxlen=self.eval_window) for s in settings.symbols
        }
        self._locks: dict[str, asyncio.Lock] = {s: asyncio.Lock() for s in settings.symbols}
        self._eval_sem = asyncio.Semaphore(1)
        self._seen: set[str] = set()
        self.signals: list[TradeSignal] = []
        self._signal_index: dict[str, TradeSignal] = {}
        self.on_signal: Callable[[TradeSignal], Awaitable[None]] | None = None
        self.last_eval_ms: dict[str, int] = {}
        self.last_eval_duration_ms: dict[str, float] = {}
        self.eval_errors: int = 0
        self._setup_cfg = self._build_setup_config()
        self._ict_cfg = ICTConfig(
            mode="historical", show_sessions=False, show_htf_cisd=False,
            cisd_all=True, cisd_min_grade=0,
            fvg_min_atr=self._setup_cfg.fvg_min_atr,
            fvg_min_body=self._setup_cfg.fvg_min_body,
            fvg_min_width_atr=self._setup_cfg.fvg_min_width_atr,
            sweep_reclaim_bars=self._setup_cfg.sweep_reclaim_bars,
        )

    def _build_setup_config(self) -> SetupConfig:
        s = self.settings
        return SetupConfig(
            market="crypto",
            stack=s.stack,
            quality_profile=s.quality_profile,
            require_sweep=s.require_sweep,
            allow_short=s.allow_short,
            min_grade=s.min_grade,
            min_confluence=s.min_confluence,
            use_key_targets=s.use_key_targets,
            target_mode=s.target_mode,
            rr=s.rr,
            max_bars=s.max_bars,
            commission_bps=s.commission_bps,
            slippage_bps=s.slippage_bps,
            risk_pct=s.risk_pct,
        )

    # ------------------------------------------------------------------
    async def bootstrap(self, symbol: str, bars: list[Kline]) -> list[TradeSignal]:
        """Load history and expose recent *historical* setups to the dashboard."""
        closed = [b for b in bars if b.is_closed]
        if len(closed) < 100:
            return []
        # Full history is evaluated once to populate the research signal stream;
        # only the tail is retained for per-close live evaluation.
        full = list(closed)
        async with self._eval_sem:
            signals = await asyncio.to_thread(self._compute, symbol, full, False)
        async with self._locks[symbol]:
            self.histories[symbol] = deque(closed[-self.eval_window:], maxlen=self.eval_window)
        # Keep the most recent 120 days' setups, capped for a compact dashboard.
        signals = sorted(signals, key=lambda x: x.bar_time)[-30:]
        for signal in signals:
            signal.status = "HISTORICAL"
            signal.actionable = False
            signal.executed = False
            self._remember(signal)
        self.metrics.event(
            "INFO", "strategy",
            f"{symbol}: history loaded ({len(closed)} bars, {len(signals)} setups)",
        )
        return signals

    def append_recovered(self, bar: Kline) -> None:
        """Append a successfully backfilled strategy bar without trading it.

        Its setup belongs to the past.  We keep it in the rolling history so
        indicators and gap replays are continuous, but never execute a stale
        signal that is discovered minutes late.
        """
        if bar.interval != self.interval:
            return
        history = self.histories.setdefault(bar.symbol, deque(maxlen=self.eval_window))
        if history and bar.open_time <= history[-1].open_time:
            return
        history.append(bar)

    async def on_closed(self, bar: Kline) -> None:
        if bar.interval != self.interval:
            return
        symbol = bar.symbol
        async with self._locks.setdefault(symbol, asyncio.Lock()):
            history = self.histories.setdefault(
                symbol, deque(maxlen=self.eval_window)
            )
            if history and bar.open_time <= history[-1].open_time:
                return
            history.append(bar)
            snapshot = list(history)
        if len(snapshot) < 120:
            return
        queued_at = time.perf_counter()
        try:
            async with self._eval_sem:
                compute_start = time.perf_counter()
                self.metrics.observe("strategy_wait_ms", (compute_start - queued_at) * 1000.0)
                signals = await asyncio.to_thread(self._compute, symbol, snapshot, True)
                elapsed = (time.perf_counter() - compute_start) * 1000.0
        except Exception as exc:
            self.eval_errors += 1
            self.metrics.inc("strategy_eval_error")
            self.metrics.event("ERROR", "strategy", f"{symbol} evaluation failed", error=str(exc)[:300])
            log.exception("strategy evaluation failed for %s: %s", symbol, exc)
            return
        self.last_eval_ms[symbol] = bar.open_time
        self.last_eval_duration_ms[symbol] = elapsed
        self.metrics.observe("strategy_eval_ms", elapsed)
        self.metrics.inc("strategy_eval")
        for signal in signals:
            if signal.id in self._seen:
                continue
            self._remember(signal)
            self.metrics.inc("signal")
            self.metrics.event(
                "SIGNAL", "strategy",
                f"{symbol} {signal.direction.upper()} setup · grade {signal.grade} · {signal.rr:.2f}R",
                signal_id=signal.id,
                entry=round(signal.entry, 6),
                stop=round(signal.stop, 6),
                target=round(signal.target, 6),
            )
            if self.on_signal is not None and signal.actionable:
                await self.on_signal(signal)

    # ------------------------------------------------------------------
    def _compute(self, symbol: str, bars: list[Kline], actionable: bool) -> list[TradeSignal]:
        if len(bars) < 100:
            return []
        frame = pd.DataFrame([b.to_csv_row() for b in bars])
        for col in ("open", "high", "low", "close", "volume"):
            frame[col] = frame[col].astype(float)
        result = analyze(frame, self._ict_cfg)
        n = len(frame)
        if actionable:
            # build_setup() accepts any ICTResult. Restricting it to CISDs born
            # on the just-closed bar avoids rescanning hundreds of old setups.
            recent = [c for c in result.cisds if int(c.end) >= n - 3]
            result = replace(result, cisds=recent)
        setups = build_setup(frame, result, self._setup_cfg)
        if actionable:
            setups = [c for c in setups if int(c.end) == len(frame) - 1]
        else:
            setups = setups[-60:]
        out: list[TradeSignal] = []
        closes = frame["close"].to_numpy(float)
        n = len(frame)
        for c in setups:
            idx = int(c.end)
            if not (0 <= idx < n):
                continue
            direction = "long" if c.direction == "bull" else "short"
            entry = float(closes[idx])
            stop = float(c.sl)
            risk = abs(entry - stop)
            if risk <= 0:
                continue
            target = float(c.target) if c.target is not None else (
                entry + self.settings.rr * risk if direction == "long"
                else entry - self.settings.rr * risk
            )
            if direction == "long" and not (stop < entry < target):
                continue
            if direction == "short" and not (target < entry < stop):
                continue
            rr = abs(target - entry) / risk
            if rr < 1.0:
                continue
            bar_time = bars[idx].open_time
            digest = hashlib.sha1(
                f"{symbol}|{bar_time}|{c.direction}|{c.level:.8f}".encode()
            ).hexdigest()[:10]
            checks = {str(k): bool(v) for k, v in (c.checks or {}).items()}
            parts = {str(k): _jsonable(v) for k, v in (c.quality_parts or {}).items()}
            passed = [k for k in ("sweep", "pda", "volume", "session", "targets", "smt") if checks.get(k)]
            thesis = (
                f"CISD {c.direction} · grade {c.grade} · "
                f"{' / '.join(passed) if passed else 'base confluence'}"
            )
            out.append(TradeSignal(
                id=f"SIG-{symbol}-{bar_time}-{digest}",
                symbol=symbol,
                interval=self.interval,
                bar_time=bar_time,
                created_at=utc_ms(),
                direction=direction,
                entry=entry,
                stop=stop,
                target=target,
                rr=rr,
                grade=int(c.grade),
                quality=float(c.quality),
                status="ARMED" if actionable else "HISTORICAL",
                actionable=actionable,
                executed=False,
                reason=thesis,
                checks=checks,
                quality_parts=parts,
                setup_level=float(c.level),
            ))
        return out

    def _remember(self, signal: TradeSignal) -> None:
        if signal.id in self._seen:
            return
        self._seen.add(signal.id)
        if len(self._seen) > 20000:
            self._seen = set(list(self._seen)[-10000:])
        self.signals.append(signal)
        self._signal_index[signal.id] = signal
        self.signals = sorted(self.signals, key=lambda s: (s.bar_time, s.created_at), reverse=True)[:80]
        keep = {s.id for s in self.signals}
        self._signal_index = {k: v for k, v in self._signal_index.items() if k in keep}

    def update_signal(self, signal_id: str, *, status: str | None = None,
                      executed: bool | None = None, order_id: str | None = None,
                      reason: str | None = None) -> None:
        signal = self._signal_index.get(signal_id)
        if signal is None:
            return
        if status:
            signal.status = status
        if executed is not None:
            signal.executed = executed
        if order_id is not None:
            signal.order_id = order_id
        if reason:
            signal.reason = f"{signal.reason} · {reason}" if signal.reason else reason

    # ------------------------------------------------------------------
    def frame(self, symbol: str, limit: int = 240) -> list[Kline]:
        hist = self.histories.get(symbol.upper())
        if not hist:
            return []
        return list(hist)[-limit:]

    def public_klines(self, symbol: str, limit: int = 240) -> list[dict[str, Any]]:
        bars = self.frame(symbol, limit)
        return [b.to_public() for b in bars]

    def watchlist(self) -> list[dict[str, Any]]:
        step = interval_ms(self.interval)
        day_bars = max(2, int(86_400_000 / step))
        out = []
        for symbol in self.settings.symbols:
            bars = self.histories.get(symbol)
            if not bars:
                out.append({"symbol": symbol, "price": 0.0, "change_pct": 0.0,
                            "spark": [], "high": 0.0, "low": 0.0})
                continue
            arr = np.fromiter((b.close for b in bars), dtype=float)
            window = arr[-min(len(arr), day_bars):]
            change = (arr[-1] / arr[-len(window)] - 1.0) * 100.0 if len(window) > 1 and arr[-len(window)] else 0.0
            spark = arr[-64:].round(8).tolist()
            out.append({
                "symbol": symbol,
                "price": round(float(arr[-1]), 8),
                "change_pct": round(float(change), 2),
                "spark": spark,
                "high": round(float(window.max()), 8),
                "low": round(float(window.min()), 8),
            })
        return out

    def snapshot(self) -> dict[str, Any]:
        return {
            "interval": self.interval,
            "stack": self.settings.stack,
            "eval_window": self.eval_window,
            "quality_profile": self.settings.quality_profile,
            "events": len(self.signals),
            "eval_errors": self.eval_errors,
            "last_eval_ms": self.last_eval_ms,
            "last_eval_duration_ms": {k: round(v, 1) for k, v in self.last_eval_duration_ms.items()},
            "signals": [s.to_public() for s in self.signals[:30]],
        }
