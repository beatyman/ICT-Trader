from dataclasses import dataclass

import pandas as pd

from ict_trader.context import DealingRange, dealing_range, in_session, ote_from_signal, ote_retracement


@dataclass
class P:
    kind: str
    price: float
    index: int
    confirmed_at: int


def test_cn_session_gate():
    df = pd.DataFrame({
        "date": pd.to_datetime([
            "2026-10-28 09:45", "2026-10-28 12:00",
            "2026-10-28 13:15", "2026-10-28 15:30",
        ])
    })
    assert in_session(df, 0, "cn", "auto")
    assert not in_session(df, 1, "cn", "auto")
    assert in_session(df, 2, "cn", "auto")
    assert not in_session(df, 3, "cn", "auto")


def test_dealing_range_and_pd_position():
    swings = [P("L", 100, 10, 11), P("H", 200, 20, 21)]
    dr = dealing_range(swings, 30)
    assert dr is not None
    assert dr.eq == 150
    assert dr.position(125) == 0.25
    assert dr.position(250) == 1.0


def test_ote_retracement_bull_and_bear():
    bull = [P("L", 100, 1, 2), P("H", 200, 3, 4)]
    assert abs(ote_retracement(bull, 170, "bull") - 0.30) < 1e-9
    bear = [P("H", 200, 1, 2), P("L", 100, 3, 4)]
    assert abs(ote_retracement(bear, 130, "bear") - 0.30) < 1e-9


def test_ote_from_signal_range():
    df = pd.DataFrame({
        "high": [100, 110, 120, 130, 140],
        "low": [90, 100, 110, 120, 130],
    })
    # range 100..130, entry 121 -> 0.30 retracement
    assert abs(ote_from_signal(df, 1, 3, 121, "bull") - 0.30) < 1e-9
    # bear range 100..130, entry 109 -> 0.30
    assert abs(ote_from_signal(df, 1, 3, 109, "bear") - 0.30) < 1e-9


def test_silver_bullet_windows_are_new_york_time():
    df = pd.DataFrame({
        "date": pd.to_datetime([
            "2026-10-28 07:30", "2026-10-28 13:00",
            "2026-10-28 14:30", "2026-10-28 18:30",
        ])
    })
    assert in_session(df, 0, "crypto", "silver_bullet")   # 03:30 NY
    assert not in_session(df, 1, "crypto", "silver_bullet")  # 09:00 NY
    assert in_session(df, 2, "crypto", "silver_bullet")   # 10:30 NY
    assert in_session(df, 3, "crypto", "silver_bullet")   # 14:30 NY


def test_asian_and_london_close_windows():
    df = pd.DataFrame({
        "date": pd.to_datetime([
            "2026-10-28 00:00", "2026-10-28 22:00",
            "2026-10-28 14:30", "2026-10-28 16:30",
        ])
    })
    assert in_session(df, 0, "crypto", "asian")          # 20:00 NY
    assert not in_session(df, 1, "crypto", "asian")     # 18:00 NY
    assert in_session(df, 2, "crypto", "london_close")  # 10:30 NY
    assert not in_session(df, 3, "crypto", "london_close")  # 12:30 NY
