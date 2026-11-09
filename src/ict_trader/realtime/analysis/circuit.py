"""Small per-engine circuit breaker.

A broken engine must not consume a worker slot on every closed bar.  The
breaker is intentionally simple: open after N consecutive failures, remain open
for a cooldown, then allow one probe.  It is process-local state; persistent
engine health belongs in ``engine_performance``.
"""
from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass
class _State:
    consecutive_failures: int = 0
    opened_at: float = 0.0
    open_until: float = 0.0
    total_failures: int = 0
    total_successes: int = 0


class CircuitBreaker:
    def __init__(self, *, failure_threshold: int = 3,
                 cooldown_seconds: float = 60.0) -> None:
        if failure_threshold < 1:
            raise ValueError("failure_threshold must be >= 1")
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = max(1.0, cooldown_seconds)
        self._states: dict[str, _State] = {}

    def _state(self, engine: str) -> _State:
        return self._states.setdefault(engine, _State())

    def allow(self, engine: str, *, now: float | None = None) -> bool:
        state = self._state(engine)
        if state.open_until <= 0:
            return True
        return (now if now is not None else time.monotonic()) >= state.open_until

    def success(self, engine: str) -> None:
        state = self._state(engine)
        state.consecutive_failures = 0
        state.open_until = 0.0
        state.opened_at = 0.0
        state.total_successes += 1

    def failure(self, engine: str, *, now: float | None = None) -> None:
        state = self._state(engine)
        current = now if now is not None else time.monotonic()
        state.consecutive_failures += 1
        state.total_failures += 1
        if state.consecutive_failures >= self.failure_threshold:
            state.opened_at = current
            state.open_until = current + self.cooldown_seconds

    def snapshot(self) -> dict[str, dict[str, float | int | bool]]:
        now = time.monotonic()
        return {
            name: {
                "open": state.open_until > now,
                "consecutive_failures": state.consecutive_failures,
                "total_failures": state.total_failures,
                "total_successes": state.total_successes,
                "retry_in_seconds": round(max(0.0, state.open_until - now), 1),
            }
            for name, state in self._states.items()
        }
