# Experiment log

## E0 — frozen baseline reproducibility

Status: **harness pass; baseline reference established**.

### Frozen config

```text
model       sweep + CISD
stack       1D/1H/15m
origin      FVG
OTE         0.00–0.30
target      fixed 2R
entry       next_open
stop        CISD invalidation wick
risk        1%
direction   long-only
costs       commission 5 bps + slippage 2 bps + sell tax 0
```

Config hash:

```text
276feddacb83397d7000d3815ea125c43e9aed6056b7c4341cfb1a82e4dbabbf
```

### Data

| Split | File | Rows | SHA256 prefix |
|---|---|---:|---|
| train | btc_15m_train_2020_2023.csv | 140,256 | `6376b12aeb33b16f` |
| train | eth_15m_train_2020_2023.csv | 140,256 | `f857377aaa6b79c7` |
| train | sol_15m_train_2020_2023.csv | 115,556 | `d682ef3687f17a60` |
| val | btc_15m_val_2024.csv | 35,136 | `709679b1580e056b` |
| val | eth_15m_val_2024.csv | 35,136 | `3c31ee56503468a0` |
| val | sol_15m_val_2024.csv | 35,136 | `26782d9f4409e6f8` |

Test files were not read.

### Results

| Split | Asset | Trades | Win rate | Avg R | Total R | PF | Max DD |
|---|---|---:|---:|---:|---:|---:|---:|
| train | BTC | 114 | 42.1% | -0.117 | -13.34 | 0.771 | -14.89% |
| train | ETH | 114 | 37.7% | -0.145 | -16.50 | 0.727 | -20.62% |
| train | SOL | 91 | 37.4% | -0.106 | -9.68 | 0.800 | -16.18% |
| val | BTC | 35 | 51.4% | +0.360 | +12.59 | 1.907 | -6.91% |
| val | ETH | 20 | 40.0% | +0.210 | +4.20 | 1.544 | -5.63% |
| val | SOL | 31 | 35.5% | -0.183 | -5.67 | 0.692 | -7.05% |

Aggregate:

| Split | Trades | Win rate | Avg R | Total R |
|---|---:|---:|---:|---:|
| train | 319 | 39.19% | -0.1239 | -39.52 |
| val | 86 | 43.02% | +0.1293 | +11.12 |

### Reproducibility check

BTC validation was re-run with the same config and data.  Results matched exactly:

```text
trades 35
win rate 0.5142857142857142
avg R 0.35969349707537446
total R 12.589272397638107
PF 1.9066679306796523
max DD -0.06913176283001765
```

### Interpretation

- E0 is a **reproducibility** result, not an edge.
- Train is negative on all three assets.
- Validation is positive on BTC/ETH, negative on SOL; 2024 is one regime and only
  86 trades.
- The baseline is not stable across train/validation. Do not size it as a proven edge.
- The test set remains untouched.

### Harness fix

`detect_fvgs` mitigation and `detect_structure` were vectorised/point-in-time; the
old code was O(FVG×N) and O(N×swings). This is a performance-only change; E0 config
and data are unchanged.

## E1 — Candle 2/3 filter (pre-registration)

Status: **not started**.

### Hypothesis

A daily Candle 2 or Candle 3 closure filter improves the baseline's average R on the
validation split without destroying frequency or drawdown.

### Unique variable

```text
daily_bias_filter = c2
```

Only C2 is tested in E1.  C3 is a separate follow-up (E1b), not tested in the same run.

### C2 definition

```text
bullish C2:  current daily low < prior low
             and current daily close > prior low
bearish C2:  current daily high > prior high
             and current daily close < prior high
```

Use as a daily/4H bias filter; entry remains E0.

### Pass gate

```text
validation avg R >= baseline + 0.10  (>= +0.229R)
validation trades >= 50% of baseline (>= 43)
validation max DD no worse than baseline + 20%
train direction must not flip from negative to worse than -0.25R
```

### Failure action

```text
keep daily_bias_filter = none
do not tune C2 thresholds on validation
```

## Next action

Implement C2 as a filter, run E1 on the frozen train/validation files, and do not
open the test set.
