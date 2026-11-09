from __future__ import annotations

from ict_trader.realtime.gap_detector import GapDetector
from ict_trader.realtime.models import Kline, interval_ms


def bar(open_time: int, interval: str = "15m", *, closed: bool = True) -> Kline:
    step = interval_ms(interval)
    return Kline(
        symbol="BTCUSDT", interval=interval, open_time=open_time,
        close_time=open_time + step - 1, open=100.0, high=102.0, low=99.0,
        close=101.0, volume=10.0, is_closed=closed,
    )


BASE = 1_700_000_000_000 // 900_000 * 900_000


def test_init_then_accept_and_duplicate():
    d = GapDetector("BTCUSDT", "15m")
    assert d.observe(bar(BASE)).kind == "init"
    d.commit(bar(BASE))
    assert d.observe(bar(BASE + 900_000)).kind == "accept"
    d.commit(bar(BASE + 900_000))
    assert d.observe(bar(BASE + 900_000)).kind == "duplicate"
    assert d.duplicates == 1
    assert d.last_closed_open_time == BASE + 900_000


def test_gap_decision_and_recovery_commit():
    d = GapDetector("BTCUSDT", "15m")
    d.commit(bar(BASE))
    decision = d.observe(bar(BASE + 4 * 900_000))
    assert decision.kind == "gap"
    assert decision.missing_bars == 3
    assert decision.gap_start == BASE + 900_000
    assert decision.gap_end == BASE + 3 * 900_000
    assert d.gaps == 1

    missing = [bar(BASE + i * 900_000) for i in range(1, 4)]
    assert d.commit_bulk(missing, recovered=True) == 3
    d.commit(bar(BASE + 4 * 900_000))
    assert d.last_closed_open_time == BASE + 4 * 900_000
    assert d.recovered_bars == 3


def test_unclosed_never_advances_state():
    d = GapDetector("BTCUSDT", "15m")
    d.commit(bar(BASE))
    live = bar(BASE + 900_000, closed=False)
    assert d.observe(live).kind == "accept"
    assert d.expected_open_time == BASE + 900_000
    assert d.observe(bar(BASE + 2 * 900_000)).kind == "gap"


def test_commit_bulk_is_idempotent_on_overlap():
    d = GapDetector("BTCUSDT", "15m")
    bars = [bar(BASE + i * 900_000) for i in range(5)]
    assert d.commit_bulk(bars) == 5
    assert d.commit_bulk(bars) == 0
    assert d.last_closed_open_time == BASE + 4 * 900_000
