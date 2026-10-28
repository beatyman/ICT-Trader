from dataclasses import dataclass

import pandas as pd

from ict_trader.pd_arrays import (
    PDAArray,
    count_path_liquidity,
    detect_breakers,
    detect_mitigations,
    detect_rejections,
    near_arrays,
)


@dataclass
class S:
    kind: str
    index: int
    price: float
    confirmed_at: int


@dataclass
class OB:
    direction: str
    top: float
    bottom: float
    mitigated: bool
    right: int


def test_rejection_block_from_long_upper_wick():
    df = pd.DataFrame({
        "open": [100, 100, 100.2],
        "high": [101, 105, 101],
        "low": [99, 99.5, 99.8],
        "close": [100, 100.2, 100],
    })
    arrays = detect_rejections(df, [S("H", 1, 105, 2)], wick_ratio=0.5)
    assert len(arrays) == 1
    a = arrays[0]
    assert a.kind == "rejection" and a.direction == "bear"
    assert a.top == 105 and a.bottom == 100.2


def test_breaker_from_mitigated_ob():
    arrays = detect_breakers([OB("bull", 105, 100, True, 3)])
    assert len(arrays) == 1
    assert arrays[0].kind == "breaker" and arrays[0].direction == "bear"


def test_mitigation_block_from_failed_swing():
    df = pd.DataFrame({
        "open": [110, 100, 108, 99, 99],
        "high": [110, 101, 109, 100, 100],
        "low": [109, 99, 107, 98, 98],
        "close": [110, 100, 108, 99, 99],
    })
    swings = [S("H", 0, 110, 1), S("L", 1, 100, 2), S("H", 2, 108, 3)]
    arrays = detect_mitigations(df, swings)
    assert len(arrays) == 1
    assert arrays[0].kind == "mitigation" and arrays[0].direction == "bear"


def test_near_arrays_respects_known_at_and_distance():
    a = PDAArray("rejection", "bull", 101, 99, 1, 5)
    assert near_arrays([a], 100, "bull", atr=1, at=5)
    assert not near_arrays([a], 100, "bull", atr=1, at=4)
    assert not near_arrays([a], 110, "bull", atr=1, at=5)


def test_count_path_liquidity():
    swings = [S("H", 1, 105, 2), S("H", 2, 107, 3), S("L", 3, 100, 4)]
    assert count_path_liquidity(swings, 100, 110, "bull") == 2
