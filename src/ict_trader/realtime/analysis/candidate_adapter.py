"""Candidate -> EngineSignal adapter. The only bridge into SetupManager."""
from __future__ import annotations

from .contracts import EngineSignal, PriceZone
from ...setup_evaluator import Candidate


def to_engine_signal(candidate: Candidate, *, symbol: str, interval: str,
                     source_kline_open_time: int, engine_version: str) -> EngineSignal:
    target = float(candidate.targets[0])
    return EngineSignal(
        engine="ict",
        symbol=symbol,
        interval=interval,
        direction=candidate.direction,
        signal_type="ict_rule",
        trigger_price_zone=PriceZone(candidate.entry_low, candidate.entry_high),
        invalidation_price=candidate.stop,
        confidence=max(0.0, min(1.0, float(candidate.rr) / 4.0)),
        source_kline_open_time=int(source_kline_open_time),
        engine_version=engine_version,
        evidence={
            **candidate.to_public(),
            "target": target,
            "rule_id": candidate.rule_id,
            "net_rr": candidate.rr,
        },
    )
