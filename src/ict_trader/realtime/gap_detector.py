"""Per-stream closed-bar continuity state machine.

Rules (deliberately exact):
  * only ``is_closed=True`` messages advance ``last_closed_open_time``;
  * ``open_time <= last_closed`` is a duplicate/out-of-order message -> drop;
  * ``open_time == last_closed + interval`` is continuous -> accept;
  * ``open_time > expected`` is a gap.  The stream worker pauses and REST-fills
    ``[expected, open_time - interval]`` before replaying the buffered message.

The detector owns no I/O and is cheap enough to unit-test exhaustively.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .models import Kline, interval_ms


@dataclass(slots=True)
class Decision:
    kind: str                     # init | accept | duplicate | gap | out_of_order
    expected_open_time: int | None
    gap_start: int | None = None
    gap_end: int | None = None
    missing_bars: int = 0

    @property
    def is_gap(self) -> bool:
        return self.kind == "gap"


@dataclass(slots=True)
class GapDetector:
    symbol: str
    interval: str
    last_closed_open_time: int | None = None
    gaps: int = 0
    duplicates: int = 0
    out_of_order: int = 0
    recovered_bars: int = 0
    last_gap_start: int | None = None
    last_gap_end: int | None = None
    gap_history: list[tuple[int, int]] = field(default_factory=list)

    @property
    def step(self) -> int:
        return interval_ms(self.interval)

    @property
    def expected_open_time(self) -> int | None:
        if self.last_closed_open_time is None:
            return None
        return self.last_closed_open_time + self.step

    def observe(self, bar: Kline) -> Decision:
        if not bar.is_closed:
            return Decision("accept", self.expected_open_time)
        if bar.symbol != self.symbol or bar.interval != self.interval:
            raise ValueError("stream mismatch in gap detector")
        if bar.open_time % self.step != 0:
            return Decision("out_of_order", self.expected_open_time)

        if self.last_closed_open_time is None:
            return Decision("init", bar.open_time)

        if bar.open_time <= self.last_closed_open_time:
            self.duplicates += 1
            return Decision("duplicate", self.expected_open_time)

        expected = self.last_closed_open_time + self.step
        if bar.open_time == expected:
            return Decision("accept", expected)
        if bar.open_time < expected:
            # Aligned timestamps cannot land here, but keep the branch explicit.
            self.out_of_order += 1
            return Decision("out_of_order", expected)

        missing = (bar.open_time - expected) // self.step
        self.gaps += 1
        self.last_gap_start = expected
        self.last_gap_end = bar.open_time - self.step
        self.gap_history.append((expected, bar.open_time - self.step))
        return Decision(
            "gap",
            expected,
            gap_start=expected,
            gap_end=bar.open_time - self.step,
            missing_bars=int(missing),
        )

    def commit(self, bar: Kline, *, recovered: bool = False) -> None:
        """Advance the state after a bar is durably accepted.

        Callers must only commit after Redis/ClickHouse enqueue succeeded (or
        the durable WAL accepted the row).  This is what makes replay after a
        crash overlap-safe rather than lossy.
        """
        if not bar.is_closed:
            raise ValueError("only closed bars advance the detector")
        if self.last_closed_open_time is not None and bar.open_time <= self.last_closed_open_time:
            return
        self.last_closed_open_time = bar.open_time
        if recovered:
            self.recovered_bars += 1

    def commit_bulk(self, bars: list[Kline], *, recovered: bool = True) -> int:
        accepted = 0
        for bar in sorted(bars, key=lambda x: x.open_time):
            if self.last_closed_open_time is not None and bar.open_time <= self.last_closed_open_time:
                continue
            self.commit(bar, recovered=recovered)
            accepted += 1
        return accepted

    def snapshot(self) -> dict:
        return {
            "symbol": self.symbol,
            "interval": self.interval,
            "last_closed_open_time": self.last_closed_open_time,
            "expected_open_time": self.expected_open_time,
            "gaps": self.gaps,
            "duplicates": self.duplicates,
            "out_of_order": self.out_of_order,
            "recovered_bars": self.recovered_bars,
            "last_gap_start": self.last_gap_start,
            "last_gap_end": self.last_gap_end,
        }
