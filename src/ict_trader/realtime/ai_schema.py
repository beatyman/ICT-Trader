"""Pydantic validation for DeepSeek review output."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class AITradePlan(BaseModel):
    model_config = ConfigDict(extra="ignore")
    open: bool = False
    direction: Literal["long", "short"] | None = None
    entry_low: float | None = None
    entry_high: float | None = None
    stop: float | None = None
    targets: list[float] = Field(default_factory=list, max_length=3)
    size_pct: float = Field(default=0.0, ge=0.0, le=0.30)
    reason: str = ""


class ReviewDecision(BaseModel):
    model_config = ConfigDict(extra="ignore")

    verdict: Literal["support", "caution", "reject"]
    confidence: int = Field(ge=0, le=100)
    headline: str = "No headline"
    summary: str = ""
    risk_flags: list[str] = Field(default_factory=list, max_length=8)
    execution_notes: list[str] = Field(default_factory=list, max_length=8)
    invalidation: str = ""
    data_quality: str = ""
    trade_plan: AITradePlan | None = None

    @field_validator("headline")
    @classmethod
    def nonempty(cls, value: str) -> str:
        return value or "No headline"
