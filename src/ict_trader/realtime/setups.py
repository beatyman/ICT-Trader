"""Setup lifecycle for future engine signals.

A setup is created only when a *new* EngineSignal is emitted by the live
registry.  Historical bootstrap signals are deliberately not backfilled.

State machine::

    DETECTED --trigger--> OPEN --TP--> CLOSED
        |                   |
        +--stop/expiry--> INVALIDATED/EXPIRED

Every transition is persisted to `setups` and, where relevant, `tp_sl_events`.
No order is routed here; this is the lifecycle record that a later paper/AI
layer can attach to.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from .analysis.contracts import EngineSignal


def _now_dt() -> datetime:
    return datetime.now(timezone.utc)


def _dt(ms: int | None) -> datetime | None:
    if not ms:
        return None
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)


@dataclass
class Setup:
    setup_id: str
    symbol: str
    interval: str
    direction: str
    status: str
    created_at: int
    updated_at: int
    entry_low: float
    entry_high: float
    stop: float
    target: float
    confidence: float
    engine: str
    engine_version: str
    targets: list[float] = field(default_factory=list)
    signal_ids: list[str] = field(default_factory=list)
    engine_signals: list[dict[str, Any]] = field(default_factory=list)
    opened_at: int | None = None
    closed_at: int | None = None
    exit_price: float | None = None
    ai_decision_id: str = ""
    ai_confidence: float = 0.0
    ai_decision: str = ""
    bars_alive: int = 0
    last_price: float = 0.0
    fill_price: float | None = None
    resonance_score: int = 0
    resonance_breakdown: dict[str, Any] = field(default_factory=dict)
    structural_invalidation_price: float | None = None
    structural_invalidation_status: str = ""
    structure_bias: str = "neutral"
    shadow: bool = False
    execution_mode: str = ""
    entry_pending_reason: str = ""
    events_pending_conflict: bool = False

    @property
    def entry(self) -> float:
        # A short must fill at the upper (favourable) edge; a long at the
        # lower edge.  Before a fill this is the planned limit price, after
        # the fill it is the actual paper fill.
        if self.fill_price is not None:
            return self.fill_price
        return self.entry_high if self.direction == "short" else self.entry_low

    @property
    def entry_mid(self) -> float:
        return (self.entry_low + self.entry_high) / 2.0

    def rr_for(self, target: float) -> float:
        risk = abs(self.entry - self.stop)
        return abs(float(target) - self.entry) / risk if risk > 0 else 0.0

    @property
    def rr(self) -> float:
        return self.rr_for(self.target)

    @property
    def tp_levels(self) -> list[float]:
        levels = [float(t) for t in (self.targets or [self.target])]
        levels = [t for t in levels if t > 0]
        if self.direction == "short":
            levels = [t for t in levels if t < self.entry]
            levels.sort(reverse=True)
        else:
            levels = [t for t in levels if t > self.entry]
            levels.sort()
        merged: list[float] = []
        for level in levels:
            if merged and abs(level - merged[-1]) / max(self.entry, 1e-9) < 0.0015:
                continue
            merged.append(level)
        return merged

    @property
    def is_active(self) -> bool:
        return self.status in {"DETECTED", "OPEN"}

    def to_public(self) -> dict[str, Any]:
        return {
            "setup_id": self.setup_id,
            "symbol": self.symbol,
            "interval": self.interval,
            "direction": self.direction,
            "status": self.status,
            "created_at": self.created_at,
            "opened_at": self.opened_at,
            "closed_at": self.closed_at,
            "entry_low": self.entry_low,
            "entry_high": self.entry_high,
            "entry": self.entry,
            "entry_mid": self.entry_mid,
            "fill_price": self.fill_price,
            "stop": self.stop,
            "target": self.target,
            "targets": list(self.targets),
            "tp_levels": list(self.tp_levels),
            "tp_rr": [round(self.rr_for(t), 3) for t in self.tp_levels],
            "rr": round(self.rr, 3),
            "confidence": round(self.confidence, 4),
            "engine": self.engine,
            "engine_version": self.engine_version,
            "signal_ids": list(self.signal_ids),
            "engine_signals": list(self.engine_signals),
            "ai_decision_id": self.ai_decision_id,
            "ai_confidence": self.ai_confidence,
            "ai_decision": self.ai_decision,
            "bars_alive": self.bars_alive,
            "last_price": self.last_price,
            "resonance_score": self.resonance_score,
            "resonance_breakdown": self.resonance_breakdown,
            "structural_invalidation_price": self.structural_invalidation_price,
            "structural_invalidation_status": self.structural_invalidation_status,
            "structure_bias": self.structure_bias,
            "shadow": self.shadow,
            "execution_mode": self.execution_mode,
            "entry_pending_reason": self.entry_pending_reason,
        }

    def setup_row(self, ingested_at: datetime) -> tuple:
        engine_signals = json.dumps(self.engine_signals, separators=(",", ":"))
        return (
            self.setup_id, self.symbol, self.interval, self.direction, self.status,
            _dt(self.created_at), _dt(self.opened_at), _dt(self.closed_at),
            _dt(self.updated_at), list(self.signal_ids), engine_signals,
            self.ai_decision_id, float(self.ai_confidence), self.ai_decision,
            float(self.entry), "market", 0.0,
            json.dumps(self.targets or [self.target], separators=(",", ":")), float(self.stop),
            float(self.rr), "[]", self.exit_price, self.exit_price,
            0.0, 0.0, 0.0, self.engine_version, "setup-v1", "auto-v1",
            ingested_at,
            "", self.resonance_score,
            json.dumps(self.resonance_breakdown, separators=(",", ":")),
            self.structural_invalidation_price, self.structural_invalidation_status,
        )

    def event_row(self, event_type: str, price: float, now: int,
                  reason: str = "") -> tuple:
        event_id = "EVT-" + hashlib.sha1(
            f"{self.setup_id}|{event_type}|{now}".encode()
        ).hexdigest()[:20]
        event_time = _dt(now)
        return (
            event_id, self.setup_id, self.symbol, self.interval, event_type,
            0, float(price), float(price), 100.0, 0.0, reason[:400],
            event_time, event_time, _now_dt(),
        )


class SetupManager:
    def __init__(self, persist_setup: Callable[[tuple], Awaitable[None]],
                 persist_event: Callable[[tuple], Awaitable[None]],
                 *, max_bars: int = 40,
                 on_created: Callable[["Setup"], Awaitable[None]] | None = None,
                 on_entry: Callable[["Setup"], Awaitable[bool]] | None = None,
                 settings: Any | None = None) -> None:
        self.persist_setup = persist_setup
        self.persist_event = persist_event
        self.on_created = on_created
        self.on_entry = on_entry
        self.settings = settings
        self.max_bars = max(4, int(max_bars))
        self._setups: dict[str, Setup] = {}
        self.events: list[dict[str, Any]] = []

    def all(self) -> list[Setup]:
        return sorted(self._setups.values(), key=lambda s: s.created_at, reverse=True)

    def active(self) -> list[Setup]:
        return [s for s in self.all() if s.is_active]

    async def on_signal(self, signal: EngineSignal, *, now: int | None = None,
                        resonance: Any | None = None,
                        min_rr: float = 0.0,
                        min_confidence: float = 0.0,
                        structure_bias: str | None = None) -> Setup | None:
        if self.settings and getattr(self.settings, "require_primary_setup", True):
            if signal.engine != "ict" or getattr(signal, "role", "") != "primary":
                self.events.append({
                    "setup_id": "",
                    "type": "setup_rejected",
                    "reason": "non_primary_engine",
                    "engine": signal.engine,
                    "role": getattr(signal, "role", ""),
                    "symbol": signal.symbol,
                    "signal_id": signal.signal_id,
                })
                return None
        if signal.engine == "ict" and not (
            self.settings and self.settings.ict_setup_enabled
        ):
            return None
        if signal.confidence < min_confidence:
            return None
        now = now or signal.created_at
        if structure_bias and structure_bias != "neutral":
            want = "bullish" if signal.direction.value == "long" else "bearish"
            if structure_bias != want:
                return None
        setup = self._setups.get("SET-" + signal.signal_id[4:])
        if setup is not None:
            return setup
        setup = Setup(
            setup_id="SET-" + signal.signal_id[4:],
            symbol=signal.symbol,
            interval=signal.interval,
            direction=signal.direction.value,
            status="DETECTED",
            created_at=now,
            updated_at=now,
            entry_low=signal.trigger_price_zone.low,
            entry_high=signal.trigger_price_zone.high,
            stop=signal.invalidation_price,
            target=float(signal.evidence_plain().get("target", signal.trigger_price_zone.mid)),
            targets=[],
            confidence=signal.confidence,
            engine=signal.engine,
            engine_version=signal.engine_version,
            signal_ids=[signal.signal_id],
            engine_signals=[signal.to_public()],
            resonance_score=int(getattr(resonance, "resonance_score", 0)),
            resonance_breakdown={
                **dict(getattr(resonance, "breakdown", {}) or {}),
                "ltf_structure": structure_bias or "neutral",
            },
            structural_invalidation_price=signal.evidence_plain().get(
                "structural_invalidation_price"),
            structure_bias=structure_bias or "neutral",
        )
        ev = signal.evidence_plain()
        raw_targets = ev.get("targets")
        if not raw_targets:
            raw_targets = [ev.get(k) for k in ("tp1", "tp2", "tp3") if ev.get(k)]
        setup.targets = [float(x) for x in (raw_targets or []) if float(x) > 0]
        if not setup.targets:
            setup.targets = [setup.target]
        if setup.target <= 0 or setup.rr < min_rr:
            return None
        self._setups[setup.setup_id] = setup
        await self.persist_setup(setup.setup_row(_now_dt()))
        self.events.append({"setup_id": setup.setup_id, "type": "SETUP_DETECTED",
                            "symbol": setup.symbol, "time": now, "price": setup.entry})
        if self.on_created is not None:
            await self.on_created(setup)
        return setup

    async def on_price(self, symbol: str, price: float, *,
                       interval: str | None = None,
                       high: float | None = None, low: float | None = None,
                       now: int | None = None) -> None:
        now = now or int(_now_dt().timestamp() * 1000)
        for setup in list(self._setups.values()):
            if setup.symbol != symbol or not setup.is_active:
                continue
            if interval is not None and setup.interval != interval:
                continue
            setup.last_price = float(price)
            if setup.status == "DETECTED":
                if setup.shadow:
                    continue
                if self._hits_entry(setup, float(low if low is not None else price),
                                    float(high if high is not None else price)):
                    if self.settings is not None and self.settings.ict_setup_shadow:
                        setup.shadow = True
                        setup.execution_mode = "shadow"
                        setup.updated_at = now
                        await self._transition(
                            setup, "SHADOW_ENTRY", setup.entry, now,
                            "shadow mode: broker blocked",
                        )
                        continue
                    accepted = True
                    if self.on_entry is not None:
                        accepted = await self.on_entry(setup)
                    if accepted is None:
                        setup.entry_pending_reason = "position_conflict"
                        setup.updated_at = now
                        if not setup.events_pending_conflict:
                            setup.events_pending_conflict = True
                            await self._transition(
                                setup, "ENTRY_WAIT", setup.entry, now,
                                "same-symbol paper position already open",
                            )
                        continue
                    if not accepted:
                        setup.status = "INVALIDATED"
                        setup.closed_at = now
                        setup.updated_at = now
                        await self._transition(
                            setup, "ENTRY_REJECTED", setup.entry, now,
                            "paper broker rejected entry",
                        )
                        continue
                    setup.fill_price = (setup.entry_high if setup.direction == "short"
                                        else setup.entry_low)
                    setup.status = "OPEN"
                    setup.opened_at = now
                    setup.updated_at = now
                    await self._transition(setup, "ENTRY", setup.fill_price, now,
                                           "favourable edge touched")
                    # Never evaluate TP on the same tick that opened the
                    # position: one bar's high/low can contain both prices,
                    # which would be look-ahead inside the bar.
                    continue
                elif self._hits_stop(setup, float(low if low is not None else price),
                                     float(high if high is not None else price)):
                    setup.status = "INVALIDATED"
                    setup.closed_at = now
                    setup.updated_at = now
                    await self._transition(setup, "INVALIDATED", setup.stop, now,
                                           "invalidated before entry")
            if setup.structural_invalidation_price is not None and setup.status in {"DETECTED", "OPEN"}:
                if self._hits_structural(setup, low if low is not None else price,
                                         high if high is not None else price):
                    setup.status = "INVALIDATED"
                    setup.structural_invalidation_status = "hit"
                    setup.closed_at = now
                    setup.updated_at = now
                    await self._transition(
                        setup, "STRUCTURAL_INVALIDATION",
                        setup.structural_invalidation_price, now,
                        "close crossed structural invalidation level",
                    )
                    continue
            if setup.status == "OPEN":
                if self._hits_target(setup, float(high if high is not None else price),
                                     float(low if low is not None else price)):
                    setup.status = "CLOSED"
                    setup.exit_price = setup.target
                    setup.closed_at = now
                    setup.updated_at = now
                    await self._transition(setup, "TP", setup.target, now, "target hit")
                elif self._hits_stop(setup, float(low if low is not None else price),
                                     float(high if high is not None else price)):
                    setup.status = "CLOSED"
                    setup.exit_price = setup.stop
                    setup.closed_at = now
                    setup.updated_at = now
                    await self._transition(setup, "SL", setup.stop, now, "stop hit")

    async def on_bar(self, symbol: str, *, interval: str, high: float, low: float,
                     close: float, now: int) -> None:
        await self.on_price(symbol, close, interval=interval,
                            high=high, low=low, now=now)
        for setup in list(self._setups.values()):
            if setup.symbol != symbol or not setup.is_active:
                continue
            if setup.interval != interval:
                continue
            setup.bars_alive += 1
            if setup.bars_alive > self.max_bars:
                setup.status = "EXPIRED"
                setup.closed_at = now
                setup.updated_at = now
                await self._transition(setup, "EXPIRED", close, now, "max bars exceeded")

    async def _transition(self, setup: Setup, kind: str, price: float,
                          now: int, reason: str) -> None:
        await self.persist_setup(setup.setup_row(_now_dt()))
        await self.persist_event(setup.event_row(kind, price, now, reason))
        self.events.append({"setup_id": setup.setup_id, "type": kind,
                            "symbol": setup.symbol, "time": now, "price": price})
        self.events = self.events[-100:]

    @staticmethod
    def _hits_entry(setup: Setup, low: float, high: float) -> bool:
        if setup.direction == "short":
            return high >= setup.entry_high
        return low <= setup.entry_low

    @staticmethod
    def _hits_stop(setup: Setup, low: float, high: float) -> bool:
        return low <= setup.stop if setup.direction == "long" else high >= setup.stop

    @staticmethod
    def _hits_structural(setup: Setup, low: float, high: float) -> bool:
        price = setup.structural_invalidation_price
        if price is None:
            return False
        return low <= price if setup.direction == "long" else high >= price

    @staticmethod
    def _hits_target(setup: Setup, high: float, low: float) -> bool:
        return high >= setup.target if setup.direction == "long" else low <= setup.target
