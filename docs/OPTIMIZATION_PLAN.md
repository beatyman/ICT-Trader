# ICT strategy optimization plan

Status: proposed, 2026-10-29

## 1. Objective

Maximize **out-of-sample net expectancy per trade**, not win rate, under fixed risk.

```text
primary      OOS net expectancy > 0 after costs
secondary    PF >= 1.15
secondary    max drawdown <= baseline + 20%
secondary    trade count >= 50% of baseline
constraint   no single trade risk > 1%
```

If no variant passes, the correct production decision remains `NO_TRADE`.

## 2. Frozen baseline

The baseline must be fixed before any experiment.

```text
model          sweep + CISD
stack          1D/1H/15m
origin         FVG optional
OTE            0.00-0.30 (only if enabled)
target         fixed 2R
entry          next open
stop           CISD invalidation wick
risk           1% per trade
direction      long-only by default
costs          commission 5 bps, slippage 2 bps, sell tax by market
```

Baseline measurements:

| Dataset | Trades | Win rate | Avg R | Status |
|---|---:|---:|---:|---|
| Full BTC FVG-only | 118 | 35.6% | -0.227 | negative |
| Full BTC FVG + shallow OTE | 56 | 41.1% | -0.136 | negative |
| 8-case FVG + shallow OTE | 45 | 60.0% | +0.384 | in-sample only |
| 8-case FVG CE | 33 | 57.6% | +0.413 | mixed |
| 8-case Silver Bullet | 8 | 50.0% | +0.508 | too few |
| 8-case strict first FVG | 0 | — | — | rejects all |

Do not call any of these an edge.

## 3. Data split

Use a time split, not a random split.

```text
train       2020-01-01 to 2023-12-31
validation  2024-01-01 to 2024-12-31
test        2025-01-01 to 2026-10-29   (locked until final)
```

Instruments:

```text
BTC, ETH, SOL 15m
HK 00700, 09988 15m
CN example 15m
optional FX 15m only with a clean feed
```

No parameter is changed after looking at the test set.  If the test set is opened
more than once, it is no longer a test set.

## 4. Experiment program

One variable per experiment.  Each experiment has a pre-registered pass/fail gate.

| ID | Hypothesis | Change | Primary metric | Pass gate | If fail |
|---|---|---|---|---|---|
| E0 | Baseline is reproducible | freeze config + dataset hashes | trades, avg R | matches baseline | fix harness |
| E1 | Candle 2/3 filter improves bias | `daily_bias_filter=c2` then `c3` | OOS avg R, DD | avg R +0.10, DD not worse by >20%, trades >=50% | keep off |
| E2 | CE entry improves R | `entry_mode=fvg_ce` | OOS avg R, PF | avg R +0.05, PF >= baseline | keep optional |
| E3 | Stop choice matters | `sl_mode=cisd/sweep/protected` | avg R, MAE, DD | one variant beats baseline on avg R and DD | keep cisd |
| E4 | DOL partial improves PF/DD | `partial_at_dol=0.5` | PF, max DD, avg R | PF +0.10 or DD -20%, avg R not worse | keep off |
| E5 | Range break-retest adds a stream | `model=range_break` | OOS expectancy, frequency | positive expectancy, 50+ trades | keep research |
| E6 | SMT adds value | correlated data feed | incremental avg R | positive incremental, 50+ trades | do not wire |

### E1 — Candle 2/3 filter

Rules:

```text
C2 bullish: sweep prior low, close back inside prior range
C2 bearish: sweep prior high, close back inside prior range
C3: C2 fails, C3 engulfs C2 and closes through its body
```

Use as a daily/4H bias filter, not a standalone entry.  Entry remains E0.

Code:

```text
context.candle2_closure()
context.candle3_closure()
SetupConfig.daily_bias_filter = none|c2|c3
quality_parts.c2 / c3
```

### E2 — FVG CE entry

Already implemented:

```bash
--entry-mode fvg_ce
```

Current result: 33 trades, 57.6%, +0.413R vs 45, 60.0%, +0.384R.
Total R is lower.  Keep optional; re-test on the time split, not the 8-case bundle.

### E3 — Stop A/B

Variants:

```text
A  CISD invalidation wick (baseline)
B  sweep extreme
C  protected swing (last confirmed swing before CISD)
D  Candle-2 extreme
```

Record:

```text
stop distance in ATR
MAE
hit rate
avg R
```

Do not choose the stop after seeing outcomes.

### E4 — Partial at DOL

Rule:

```text
at DOL: close 50%
remainder: trail behind protected swing or 1R
```

Record:

```text
MFE captured
remaining R
PF
DD
```

### E5 — Range break-retest

Separate model, not mixed with sweep:

```text
range >= 1 ATR wide
break and close outside
retest holds
entry on retest
stop other side of boundary
target midpoint then external liquidity
```

### E6 — SMT

Only after a correlated feed exists:

```text
BTC/ETH
EUR/GBP
A50/CSI300
```

No hard gate until incremental value is measured.

## 5. Implementation layers

```text
Layer 1  Bias       1D cloud / C2-C3 / structure / 7H profile
Layer 2  Liquidity  swing / EQH-EQL / PDH-PDL / PWH-PWL / range
Layer 3  Trigger    CISD / FVG CE / OB retest / breaker / range break
Layer 4  Risk       stop mode, target mode, partial, trail, caps
```

Each layer has exactly one active primary module.  Extras are confluence only.

## 6. Promotion rule

A variant is promoted only if all are true:

```text
1. pre-registered gate passed
2. OOS positive after costs
3. 100+ trades, or 50+ with a second independent market
4. DD within baseline + 20%
5. no single month contributes >50% of total R
6. result survives one parameter perturbation (e.g. RR 2.0 vs 2.2)
```

If a variant fails, record it and keep the default.  A failed experiment is a result.

## 7. Risk design

```text
risk per trade       1.0%
daily loss cap       3.0%
rolling weekly cap   6.0%
drawdown pause      10.0%
max open risk        3.0%
consecutive losses   3 -> half size; 5 -> stop for the day
```

Signal grades:

| Grade | Requirements | Size |
|---|---|---|
| A | bias + DOL + session + sweep + CISD + net RR >= 2.5 | 1.0R |
| B | one confluence missing; net RR >= 2.0 | 0.5R |
| C | research only | 0 |

No martingale, no grid, no averaging down.

## 8. Journal fields

```text
signal_id, date, symbol, timeframe, session, model, grade
htf_bias, daily_bias, c2, c3, dol
key_level, sweep_time, sweep_extreme
cisd_time, cisd_origin
fvg_ce, fvg_fresh, fvg_first
entry, stop, target, gross_rr, net_rr
risk_pct, position_size, result_r
mfe_r, mae_r, exit_efficiency, exit_reason, rule_break
```

## 9. Current code status

| Feature | Status |
|---|---|
| FVG CE metadata | done |
| FVG first-presented metadata | done |
| FVG fresh metadata | done |
| `--require-first-fvg` | done, rejects all in sample |
| `--require-fresh-fvg` | done, no change in sample |
| `--entry-mode fvg_ce` | done, mixed A/B |
| Candle 2/3 | not implemented |
| Stop A/B | not implemented |
| Partial at DOL | not implemented |
| Range break-retest | not implemented |
| MFE/MAE | not implemented |
| SMT feed | not implemented |

## 10. Execution order

```text
Week 1  E0 baseline freeze + MFE/MAE
Week 2  E1 Candle 2/3 filter
Week 3  E2 CE on time split
Week 4  E3 stop A/B
Week 5  E4 partial at DOL
Week 6  E5 range model
Later   E6 SMT
```

The order may change only if an earlier experiment invalidates the premise.

## 11. Out of scope

```text
minimum RR 3 as a universal rule
first-FVG hard gate
SMT hard gate
Unicorn / Turtle Soup as standalone setups
Martingale / grid recovery
earnings-gap FVG treated as intraday FVG
```

## 12. Deliverables

```text
docs/OPTIMIZATION_PLAN.md       this plan
docs/OPTIMIZATION.md            measured results
output/ict/experiments.csv      one row per experiment
docs/DECISIONS.md               promote / keep optional / reject
```

The production default changes only after a promotion decision is written down.
