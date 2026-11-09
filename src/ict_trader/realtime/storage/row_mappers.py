"""Pure row mappers shared by writers and tests."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import orjson

from ..analysis.contracts import EngineSignal
from ..models import ms_to_datetime

ENGINE_SIGNAL_COLUMNS = [
    "signal_id", "engine", "engine_version", "symbol", "interval", "direction",
    "signal_type", "trigger_zone_low", "trigger_zone_high", "invalidation_price",
    "confidence", "evidence", "source_kline_open_time", "created_at", "ingested_at",
]


def engine_signal_row(signal: EngineSignal,
                      *, ingested_at: datetime | None = None) -> tuple[Any, ...]:
    return (
        signal.signal_id,
        signal.engine,
        signal.engine_version,
        signal.symbol,
        signal.interval,
        signal.direction.value,
        signal.signal_type,
        signal.trigger_price_zone.low,
        signal.trigger_price_zone.high,
        signal.invalidation_price,
        signal.confidence,
        orjson.dumps(signal.evidence_plain()).decode(),
        ms_to_datetime(signal.source_kline_open_time),
        ms_to_datetime(signal.created_at),
        ingested_at or datetime.now(timezone.utc),
    )
