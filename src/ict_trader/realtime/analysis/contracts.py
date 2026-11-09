"""Immutable contracts shared by every analysis engine.

The rule is deliberately strict: an engine returns :class:`EngineSignal`
objects and nothing else.  Persistence, AI calls, setup creation and execution
belong to higher layers.  This keeps the four engines independently testable
and prevents accidental event-loop I/O from leaking into compute code.
"""
from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping

from ..models import INTERVAL_MS, utc_ms


class SignalDirection(str, Enum):
    LONG = "long"
    SHORT = "short"
    NEUTRAL = "neutral"


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(k): _freeze(v) for k, v in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(v) for v in value)
    if isinstance(value, set):
        return frozenset(_freeze(v) for v in value)
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(k): _thaw(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [_thaw(v) for v in value]
    if isinstance(value, (set, frozenset)):
        return [_thaw(v) for v in value]
    return value


def _finite(name: str, value: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be numeric") from exc
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite")
    return number


@dataclass(frozen=True, slots=True)
class PriceZone:
    low: float
    high: float

    def __post_init__(self) -> None:
        low = _finite("zone.low", self.low)
        high = _finite("zone.high", self.high)
        if low > high:
            raise ValueError(f"inverted price zone: low={low} > high={high}")
        if low <= 0:
            raise ValueError("price zone must be positive")
        object.__setattr__(self, "low", low)
        object.__setattr__(self, "high", high)

    @property
    def mid(self) -> float:
        return (self.low + self.high) / 2.0

    def contains(self, price: float) -> bool:
        return self.low <= float(price) <= self.high

    def to_public(self) -> dict[str, float]:
        return {"low": self.low, "high": self.high}


@dataclass(frozen=True, slots=True)
class AnalysisContext:
    """Read-only context handed to an engine for one closed-bar evaluation."""

    symbol: str
    interval: str
    as_of_ms: int
    params: Mapping[str, Any] = field(default_factory=dict)
    htf: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        symbol = self.symbol.upper().strip()
        if not symbol:
            raise ValueError("symbol is required")
        if self.interval not in INTERVAL_MS:
            raise ValueError(f"unsupported interval: {self.interval!r}")
        if int(self.as_of_ms) <= 0:
            raise ValueError("as_of_ms must be a UTC-ms timestamp")
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "as_of_ms", int(self.as_of_ms))
        object.__setattr__(self, "params", _freeze(self.params))
        object.__setattr__(self, "htf", _freeze(self.htf))


@dataclass(frozen=True, slots=True)
class EngineSignal:
    """Standardized, immutable output of every engine.

    ``signal_id`` is derived from the business identity so the same bar
    replayed after a restart produces exactly the same id.  It is not a random
    UUID.
    """

    engine: str
    symbol: str
    interval: str
    direction: str | SignalDirection
    signal_type: str
    trigger_price_zone: PriceZone
    invalidation_price: float
    confidence: float
    source_kline_open_time: int
    engine_version: str
    created_at: int = 0
    evidence: Mapping[str, Any] = field(default_factory=dict)
    signal_id: str = ""

    def __post_init__(self) -> None:
        engine = self.engine.strip().lower()
        symbol = self.symbol.upper().strip()
        signal_type = self.signal_type.strip().lower()
        version = self.engine_version.strip()
        if not engine or not signal_type:
            raise ValueError("engine and signal_type are required")
        if not symbol:
            raise ValueError("symbol is required")
        if self.interval not in INTERVAL_MS:
            raise ValueError(f"unsupported interval: {self.interval!r}")
        if not version:
            raise ValueError("engine_version is required")

        try:
            direction = SignalDirection(self.direction)
        except ValueError as exc:
            raise ValueError(f"invalid direction: {self.direction!r}") from exc

        if not isinstance(self.trigger_price_zone, PriceZone):
            raise TypeError("trigger_price_zone must be a PriceZone")

        invalidation = _finite("invalidation_price", self.invalidation_price)
        if invalidation <= 0:
            raise ValueError("invalidation_price must be positive")
        confidence = _finite("confidence", self.confidence)
        if not 0.0 <= confidence <= 1.0:
            raise ValueError("confidence must be within [0, 1]")

        source_time = int(self.source_kline_open_time)
        if source_time <= 0:
            raise ValueError("source_kline_open_time must be a UTC-ms timestamp")
        step = INTERVAL_MS[self.interval]
        if source_time % step != 0:
            raise ValueError("source_kline_open_time is not aligned to the interval")

        created = int(self.created_at or utc_ms())
        if created <= 0:
            raise ValueError("created_at must be a UTC-ms timestamp")

        object.__setattr__(self, "engine", engine)
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "signal_type", signal_type)
        object.__setattr__(self, "direction", direction)
        object.__setattr__(self, "engine_version", version)
        object.__setattr__(self, "invalidation_price", invalidation)
        object.__setattr__(self, "confidence", confidence)
        object.__setattr__(self, "source_kline_open_time", source_time)
        object.__setattr__(self, "created_at", created)
        object.__setattr__(self, "evidence", _freeze(self.evidence))

        if not self.signal_id:
            object.__setattr__(self, "signal_id", self._derive_id())

    @property
    def dedupe_key(self) -> tuple[str, str, str, int, str, str]:
        return (
            self.engine, self.symbol, self.interval,
            self.source_kline_open_time, self.signal_type, self.direction.value,
        )

    @property
    def trigger_price(self) -> float:
        return self.trigger_price_zone.mid

    def _derive_id(self) -> str:
        raw = "|".join([
            self.engine, self.engine_version, self.symbol, self.interval,
            self.direction.value, self.signal_type,
            f"{self.trigger_price_zone.low:.10f}",
            f"{self.trigger_price_zone.high:.10f}",
            f"{self.invalidation_price:.10f}",
            str(self.source_kline_open_time),
        ])
        return "SIG-" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:24]

    def evidence_plain(self) -> Any:
        return _thaw(self.evidence)

    def to_public(self) -> dict[str, Any]:
        return {
            "signal_id": self.signal_id,
            "engine": self.engine,
            "engine_version": self.engine_version,
            "symbol": self.symbol,
            "interval": self.interval,
            "direction": self.direction.value,
            "signal_type": self.signal_type,
            "trigger_price_zone": self.trigger_price_zone.to_public(),
            "invalidation_price": self.invalidation_price,
            "confidence": self.confidence,
            "source_kline_open_time": self.source_kline_open_time,
            "created_at": self.created_at,
            "evidence": self.evidence_plain(),
        }
