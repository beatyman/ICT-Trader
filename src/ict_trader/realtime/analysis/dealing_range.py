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


#: PD-02: at-EQ is a **declared predicate**, not "premium".  The corpus attests
#: four readings and chooses none of them, so the implementation declares the
#: ones it can express without inventing an ATR band:
#:
#: * ``buy_side_inclusive`` -- ``price == EQ`` belongs to premium (the historical
#:   behaviour of :meth:`DealingRange.zone`, now an explicit declaration);
#: * ``strict`` -- ``price == EQ`` is neither half (labelled ``at_eq``);
#: * ``no_entry`` -- PD-05: ``price == EQ`` is no trade unless an array sits at
#:   that price (labelled ``at_eq``; the selection gate reads
#:   :meth:`DealingRange.at_eq_blocks_entry`).
#:
#: The fourth attested reading (repo band ``+-0.05*ATR``) needs an ATR input and
#: is deliberately **not** folded in here; it is a P1 location-state object.
AT_EQ_MODES = ("buy_side_inclusive", "strict", "no_entry")
DEFAULT_AT_EQ_MODE = "buy_side_inclusive"
AT_EQ_ZONE = "at_eq"
AT_EQ_SOURCES: dict[str, dict[str, str]] = {
    "buy_side_inclusive": {"label": "corpus", "pd02": "buy-side inclusive"},
    "strict": {"label": "corpus", "pd02": "neither half (strict)"},
    "no_entry": {"label": "corpus", "pd02": "EQ => no entry",
                 "pd05": "price == EQ with no array at that price => no trade"},
}


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

    def is_at_eq(self, price: float) -> bool:
        """Exact at-EQ predicate (PD-02/PD-05): ``==``, never a band."""
        return float(price) == self.ce

    def at_eq_blocks_entry(self, price: float, *,
                           at_eq: str = DEFAULT_AT_EQ_MODE) -> bool:
        """PD-05: under the declared ``no_entry`` reading, EQ is not tradable."""
        self._validate_at_eq(at_eq)
        return at_eq == "no_entry" and self.is_at_eq(price)

    @staticmethod
    def _validate_at_eq(at_eq: str) -> None:
        if at_eq not in AT_EQ_MODES:
            raise ValueError(
                f"unknown at-EQ mode {at_eq!r}; declare one of {AT_EQ_MODES} "
                f"(PD-02)")

    def zone(self, price: float, *, at_eq: str = DEFAULT_AT_EQ_MODE) -> str:
        """Premium/discount band, with at-EQ read through the declared mode.

        ``price == EQ`` is no longer silently folded into ``premium``: the
        default (``buy_side_inclusive``) is declared, and ``strict`` /
        ``no_entry`` return :data:`AT_EQ_ZONE` instead (P0-20, PD-02).
        """
        self._validate_at_eq(at_eq)
        price = float(price)
        if self.is_at_eq(price) and at_eq != "buy_side_inclusive":
            return AT_EQ_ZONE
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

