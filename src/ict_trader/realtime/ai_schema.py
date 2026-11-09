"""Pydantic validation for DeepSeek review output."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


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

    @field_validator("headline")
    @classmethod
    def nonempty(cls, value: str) -> str:
        return value or "No headline"
