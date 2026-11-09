"""Small in-process metrics registry (no Prometheus dependency for local use).

Every hot-path metric is a counter or a bounded ring of latency samples.  Logs
are structured events held in a bounded deque so the dashboard can show the
last N state transitions without scanning log files.
"""
from __future__ import annotations

import math
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from typing import Any, Iterable


def _quantile(sorted_values: list[float], q: float) -> float:
    if not sorted_values:
        return 0.0
    idx = min(len(sorted_values) - 1, max(0, int(math.ceil(q * len(sorted_values))) - 1))
    return float(sorted_values[idx])


class Metrics:
    def __init__(self, event_limit: int = 180) -> None:
        self._lock = threading.Lock()
        self._counters: dict[str, float] = defaultdict(float)
        self._gauges: dict[str, Any] = {}
        self._latencies: dict[str, deque[float]] = {
            "pipeline_ms": deque(maxlen=4096),
            "parse_ms": deque(maxlen=4096),
            "redis_ms": deque(maxlen=4096),
            "clickhouse_batch_ms": deque(maxlen=512),
            "strategy_eval_ms": deque(maxlen=512),
            "strategy_wait_ms": deque(maxlen=512),
            "close_age_ms": deque(maxlen=4096),
            "gap_recovery_ms": deque(maxlen=256),
            "loop_lag_ms": deque(maxlen=2048),
            "engine_latency_ms": deque(maxlen=1024),
        }
        self._events: deque[dict[str, Any]] = deque(maxlen=event_limit)
        self._rates: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=8192))
        self.started_monotonic = time.monotonic()
        self.started_at = datetime.now(timezone.utc).isoformat()

    # ------------------------------------------------------------------
    def inc(self, name: str, value: float = 1.0) -> None:
        with self._lock:
            self._counters[name] += value
            if name in {"ws_message", "closed_bar", "ch_row", "trade"}:
                self._rates[name].append(time.monotonic())

    def set(self, name: str, value: Any) -> None:
        with self._lock:
            self._gauges[name] = value

    def gauge(self, name: str, default: Any = None) -> Any:
        with self._lock:
            return self._gauges.get(name, default)

    def observe(self, name: str, value_ms: float) -> None:
        if name not in self._latencies:
            with self._lock:
                self._latencies[name] = deque(maxlen=1024)
        with self._lock:
            self._latencies[name].append(float(value_ms))

    def event(self, level: str, kind: str, message: str, **fields: Any) -> None:
        item = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "level": level.upper(),
            "kind": kind,
            "message": message,
            **fields,
        }
        with self._lock:
            self._events.appendleft(item)

    def rate(self, name: str, window_seconds: float = 60.0) -> float:
        cutoff = time.monotonic() - window_seconds
        with self._lock:
            q = self._rates.get(name)
            if not q:
                return 0.0
            while q and q[0] < cutoff:
                q.popleft()
            return len(q) / max(window_seconds, 1.0)

    # ------------------------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            latency = {}
            for name, values in self._latencies.items():
                ordered = sorted(values)
                latency[name] = {
                    "p50": round(_quantile(ordered, 0.50), 3),
                    "p99": round(_quantile(ordered, 0.99), 3),
                    "max": round(max(ordered), 3) if ordered else 0.0,
                    "n": len(ordered),
                }
            counters = {k: int(v) if float(v).is_integer() else round(v, 3)
                        for k, v in self._counters.items()}
            gauges = dict(self._gauges)
            events = list(self._events)[:40]
        return {
            "uptime_seconds": round(time.monotonic() - self.started_monotonic, 1),
            "started_at": self.started_at,
            "counters": counters,
            "gauges": gauges,
            "latency": latency,
            "rates": {
                "ws_msg_per_sec": round(self.rate("ws_message", 30.0), 2),
                "closed_bars_per_min": round(self.rate("closed_bar", 60.0), 2),
            },
            "events": events,
        }


def pct(numerator: float, denominator: float, digits: int = 2) -> float:
    if not denominator:
        return 0.0
    return round(100.0 * numerator / denominator, digits)


def summarize_trades(trades: Iterable[dict[str, Any]]) -> dict[str, Any]:
    items = list(trades)
    wins = [t for t in items if float(t.get("pnl", 0.0)) > 0]
    losses = [t for t in items if float(t.get("pnl", 0.0)) < 0]
    gross_win = sum(float(t.get("pnl", 0.0)) for t in wins)
    gross_loss = abs(sum(float(t.get("pnl", 0.0)) for t in losses))
    r_values = [float(t.get("r_multiple", 0.0)) for t in items]
    return {
        "trades": len(items),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(100.0 * len(wins) / len(items), 1) if items else 0.0,
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else (99.0 if gross_win else 0.0),
        "avg_r": round(sum(r_values) / len(r_values), 2) if r_values else 0.0,
        "net_pnl": round(sum(float(t.get("pnl", 0.0)) for t in items), 2),
    }
