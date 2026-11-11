"""Dealing range, premium/discount, OTE and OB validation rules.

Sources (ICT_Corpus):
  doc 601 liquiditysweeps.com "ICT Dealing Range"
  doc 556 innercircletrader.net "Order Block Explained"
  doc 491 innercircletrader.net "Fair Value Gap"
  doc 546 innercircletrader.net "Liquidity Sweep vs Liquidity Run"

Pure functions only. No future data and no execution side effects.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class DealingRange:
    high: float
    low: float

    @property
    def ce(self) -> float:
        return (self.high + self.low) / 2.0

    @property
    def q1(self) -> float:
        return (self.low + self.ce) / 2.0

    @property
    def q3(self) -> float:
        return (self.high + self.ce) / 2.0

    def zone(self, price: float) -> str:
        if price >= self.high:
            return "above_range"
        if price >= self.q3:
            return "deep_premium"
        if price >= self.ce:
            return "premium"
        if price >= self.q1:
            return "discount"
        if price >= self.low:
            return "deep_discount"
        return "below_range"

    def ote(self, direction: str) -> tuple[float, float]:
        """0.62-0.79 retracement of the range, mapped to price."""
        if direction == "long":
            return (self.high - 0.79 * (self.high - self.low),
                    self.high - 0.62 * (self.high - self.low))
        return (self.low + 0.62 * (self.high - self.low),
                self.low + 0.79 * (self.high - self.low))


def ob_grab(direction: str, c1_high: float, c1_low: float,
            c2_high: float, c2_low: float) -> bool:
    """Doc 556 condition 1: the impulse candle grabs the OB candle's extreme.

    Bullish: ``low(c2) < low(c1)``; bearish: ``high(c2) > high(c1)``.
    Doc 556 L78: "The second candle (bullish) must grab the low of the previous
    bearish candle - price goes below the low of the bearish OB before reversing."
    """
    if direction == "long":
        return float(c2_low) < float(c1_low)
    if direction == "short":
        return float(c2_high) > float(c1_high)
    return False


def valid_ob(direction: str, *, engulf: bool, fvg: bool, mss: bool,
             grab: bool = True) -> tuple[bool, tuple[str, ...]]:
    """Four-condition OB validation from doc 556; no numeric thresholds added.

    Doc 556 L76: "To validate a bullish Order Block, four conditions must be true"
    (grab / engulf / lower-timeframe FVG / market structure shift).  ``grab``
    defaults to True so an unverified grab fails the check rather than being
    silently ignored; pass ``grab=True`` only after evaluating :func:`ob_grab`.
    """
    reasons = []
    if not grab:
        reasons.append("no_grab")
    if not engulf:
        reasons.append("no_engulf")
    if not fvg:
        reasons.append("no_fvg")
    if not mss:
        reasons.append("no_mss")
    if direction not in ("long", "short"):
        reasons.append("invalid_direction")
    return (not reasons, tuple(reasons))

