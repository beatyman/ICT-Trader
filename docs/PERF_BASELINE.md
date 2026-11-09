# P0 Performance Baseline

**Date:** 2026-11-09 (Asia/Shanghai host; all telemetry UTC)
**Host:** Windows + Docker Desktop, 8 logical CPUs, 16 GB host RAM
**Target:** AWS Tokyo `t4g.medium` (2 vCPU / 4 GB) is **not** this host.
These numbers are a local regression baseline, not a VPS acceptance result.

## Workload

- 6 symbols: BTC, ETH, SOL, BNB, XRP, DOGE
- 4 intervals: 15m, 1h, 4h, 1d (24 streams)
- Strategy interval: 15m, stack `4h_15m`
- Bootstrap: 5000 bars for 15m, 320 for the other intervals
- Mode: `--mode demo` (real historical seed + accelerated synthetic tape)
- Demo speed: 15s per logical bar; 6 sub-ticks
- ClickHouse: 24.8, Redis: 7

## Observed telemetry

| Metric | P50 | P99 | Max | Samples |
|---|---:|---:|---:|---:|
| `pipeline_ms` (receive → Redis/queue) | 17.8 ms | 312.3 ms | 379.6 ms | 1,129 |
| `redis_ms` | 17.8 ms | 312.3 ms | 379.6 ms | 1,129 |
| `clickhouse_batch_ms` | 102.4 ms | 938.0 ms | 938.0 ms | 79 |
| `strategy_eval_ms` (compute only) | 814 ms | 1,548 ms | 1,548 ms | 42 |
| `strategy_wait_ms` (semaphore wait) | 1,798 ms | 5,303 ms | 5,303 ms | 42 |
| Gap recovery | 1,312 ms | 1,312 ms | 1,312 ms | 1 event |

At capture time:

- 9.6 WS messages/s; 1.6 closed strategy bars/min;
- ClickHouse rows: 39,503; writer queue: 0; analytics queue: 0;
- Python RSS: ~187 MB; Redis container: ~12 MB; ClickHouse container: ~320 MB;
- ClickHouse `SELECT 1` latency: 8.6 ms.

## Interpretation

- Local Docker Desktop's Redis p99 is **not** the VPS same-region target. The
  `<5 ms` P99 target applies after deployment to Tokyo next to the venue.
- `strategy_eval_ms` ~0.8 s for 1,800 closed 15m bars is the current dominant
  compute cost. Six symbols are serialised by the evaluation semaphore, so
  `strategy_wait_ms` is large even though the queue drains between 15s demo
  bars. This confirms the need for the ComputeSupervisor / process pool work.
- K-line writes are batched and the writer queue is empty at steady state.
  Analytics rows now go through the separate generic batch writer.
- The observed gap recovery event completed in 1.3s; gap logic is active, not
  only theoretical.

## Reproduction

```powershell
.\.venv\Scripts\python.exe -m ict_trader.realtime --mode demo --speed 15 --port 8080
# in another shell after warm-up:
.\.venv\Scripts\python.exe scripts/smoke_realtime.py
# metrics: http://127.0.0.1:8080/api/snapshot -> metrics.latency
```

## Post-fix snapshot (same day, after P0 + HTF known-at fix)

After the analytics schema/batch writer and the left-labelled HTF fix, a fresh
warm run showed:

| Metric | P50 | P99 |
|---|---:|---:|
| `pipeline_ms` | 16.1 ms | 162.4 ms |
| `strategy_eval_ms` | 383 ms | 594 ms |
| `strategy_wait_ms` | 674 ms | 2,157 ms |

This is still local Docker Desktop, not the Tokyo target. The improvement is
consistent with evaluating only complete HTF buckets and a warm cache, but it
is not a controlled benchmark.

Record any future baseline in a new dated section; do not overwrite historical
numbers. Do not compare Demo-mode strategy results with live performance.
