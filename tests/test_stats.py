"""Wilson / mean intervals and the sample-size helpers."""
import math

import pytest

from ict_trader.stats import mean_interval, n_for_margin, n_to_detect, wilson_interval


def test_wilson_brackets_the_point_estimate():
    lo, hi = wilson_interval(23, 39)
    assert lo < 23 / 39 < hi
    assert 0.0 <= lo and hi <= 1.0


def test_wilson_narrows_with_sample_size():
    narrow = wilson_interval(500, 1000)
    wide = wilson_interval(5, 10)
    assert (narrow[1] - narrow[0]) < (wide[1] - wide[0])
    # 50% of 1000 is ~ +/-3.1 points at 95%
    assert narrow[0] == pytest.approx(0.469, abs=0.005)
    assert narrow[1] == pytest.approx(0.531, abs=0.005)


def test_wilson_edges():
    assert wilson_interval(0, 0) == (0.0, 1.0)
    lo, hi = wilson_interval(0, 10)
    assert lo == 0.0 and 0.0 < hi < 0.35
    lo, hi = wilson_interval(10, 10)
    assert hi == 1.0 and 0.65 < lo < 1.0


def test_mean_interval_is_zero_width_for_tiny_samples():
    assert mean_interval([]) == (0.0, 0.0)
    assert mean_interval([1.5]) == (1.5, 1.5)


def test_mean_interval_contains_the_mean():
    lo, hi = mean_interval([1.0, -1.0, 2.0, -2.0, 0.5])
    assert lo < 0.1 < hi


def test_n_for_margin_matches_the_textbook_value():
    # 95% confidence, +/-5 points on a 50% rate -> 385
    assert n_for_margin(0.5, 0.05) == 385
    assert n_for_margin(0.5, 0.10) == 97
    with pytest.raises(ValueError):
        n_for_margin(0.5, 1.5)


def test_n_to_detect_grows_as_the_gap_shrinks():
    wide = n_to_detect(0.55, 0.40)
    narrow = n_to_detect(0.55, 0.50)
    assert narrow > wide > 0
    # 55% vs 40% at 80% power is a few hundred per arm, not a few dozen
    assert 100 < wide < 400
    with pytest.raises(ValueError):
        n_to_detect(0.5, 0.5)
