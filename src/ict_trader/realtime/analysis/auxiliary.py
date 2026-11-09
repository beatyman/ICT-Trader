"""Unified auxiliary context and resonance score.

The engines remain independent; this module only normalizes their directional
output into one auditable object for Setup/AI inputs.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


def _norm(value: str | None) -> str:
    value = (value or "neutral").lower()
    if value in {"long", "bull", "bullish", "up", "above"}:
        return "bullish"
    if value in {"short", "bear", "bearish", "down", "below"}:
        return "bearish"
    return "neutral"


@dataclass(slots=True)
class AuxiliaryContext:
    direction: str
    htf_bias: str = "neutral"
    chan_bias: str = "neutral"
    ichimoku_bias: str = "neutral"
    wyckoff_bias: str = "neutral"
    resonance_score: int = 0
    breakdown: dict[str, Any] = field(default_factory=dict)

    def to_public(self) -> dict[str, Any]:
        return {
            "direction": self.direction,
            "htf_bias": self.htf_bias,
            "chan_bias": self.chan_bias,
            "ichimoku_bias": self.ichimoku_bias,
            "wyckoff_bias": self.wyckoff_bias,
            "resonance_score": self.resonance_score,
            "resonance_max": 4,
            "breakdown": self.breakdown,
        }


def build_auxiliary_context(direction: str, *, htf_bias: str = "neutral",
                            chan_bias: str = "neutral",
                            ichimoku_bias: str = "neutral",
                            wyckoff_bias: str = "neutral") -> AuxiliaryContext:
    target = _norm(direction)
    factors = {
        "htf": _norm(htf_bias),
        "chan": _norm(chan_bias),
        "ichimoku": _norm(ichimoku_bias),
        "wyckoff": _norm(wyckoff_bias),
    }
    matched = [name for name, bias in factors.items() if bias == target]
    score = len(matched)
    return AuxiliaryContext(
        direction=target,
        htf_bias=factors["htf"], chan_bias=factors["chan"],
        ichimoku_bias=factors["ichimoku"], wyckoff_bias=factors["wyckoff"],
        resonance_score=score,
        breakdown={
            "target": target,
            "matched": matched,
            "score": score,
            "max": 4,
            "components": factors,
        },
    )
