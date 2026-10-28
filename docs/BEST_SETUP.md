# Best setup found (research candidate)

## Candidate

```text
stack      1D bias + 1H confirmation + 15m CISD
origin     FVG delivery only
OTE        0.00–0.30 shallow retracement
target     fixed 2R from the actual fill
direction  long only
risk       existing 1% risk model, costs included
```

Command:

```bash
ict-trader backtest \
  --csv data/15m.csv \
  --daily-csv data/daily.csv \
  --market cn \
  --t-plus-1 \
  --stack 1d_1h_15m \
  --origin-filter fvg \
  --ote-gate --ote-min 0 --ote-max 0.30 \
  --min-target-rr 2.0
```

## 8-case result

| metric | value |
|---|---:|
| trades | 45 |
| win rate | 60.0% |
| weighted avg R | +0.376 |
| total R | +16.91 |

Chronological 60/40:

```text
train: 27 trades, 59.3%, +0.352R
test:  18 trades, 61.1%, +0.432R
```

This is the best combination in the 8-case grid.

## Why it is not the default

Full BTC 2025–2026, same rules:

| metric | value |
|---|---:|
| trades | 56 |
| win rate | 41.1% |
| avg R | -0.136 |
| total R | -7.64 |

The 18-trade hold-out was a favorable regime/slice.  The full history is negative.

## Rule

Do not call this a live edge.  It is the **best research candidate** until it survives
a full-history, multi-symbol, purged walk-forward with at least 100 out-of-sample trades.
