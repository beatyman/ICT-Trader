"""Small-sample statistics for trade lists.

The engine reports win rates on 20-150 trades, where a point estimate is close
to meaningless.  These helpers answer the only two questions that matter before
trusting a win rate: how wide is the interval, and how many trades would it take
to tell two win rates apart.
"""
from __future__ import annotations

import math


def wilson_interval(wins: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval for a proportion (better than Wald for small n)."""
    if n <= 0:
        return (0.0, 1.0)
    p = wins / n
    denom = 1.0 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = (z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def mean_interval(values: list[float], z: float = 1.96) -> tuple[float, float]:
    """Normal approximation interval for the mean of `values` (0,0 when n<2)."""
    n = len(values)
    if n < 2:
        return (float(values[0]) if n else 0.0, float(values[0]) if n else 0.0)
    mean = sum(values) / n
    var = sum((v - mean) ** 2 for v in values) / (n - 1)
    half = z * math.sqrt(var / n)
    return (mean - half, mean + half)


def n_for_margin(p: float = 0.5, margin: float = 0.05, z: float = 1.96) -> int:
    """Trades needed for a win-rate estimate within +/- ``margin`` at 95%."""
    if not 0 < margin < 1:
        raise ValueError("margin must be in (0, 1)")
    return int(math.ceil((z * z * p * (1 - p)) / (margin * margin)))


def n_to_detect(p1: float, p2: float, alpha: float = 0.05, power: float = 0.8) -> int:
    """Trades PER ARM needed to detect a difference between two win rates."""
    if p1 == p2:
        raise ValueError("the two rates are identical")
    z_a = 1.959964 if alpha == 0.05 else 1.644854  # 95% / 90% two-sided
    z_b = 0.841621 if power == 0.8 else 1.281552   # 80% / 90%
    pbar = (p1 + p2) / 2
    num = (z_a * math.sqrt(2 * pbar * (1 - pbar)) + z_b * math.sqrt(p1 * (1 - p1) + p2 * (1 - p2))) ** 2
    return int(math.ceil(num / ((p1 - p2) ** 2)))
