# Optimization notes — sweep/reclaim, entry mode, plan gate

This change set is driven by the recurring themes in the ICT/SMC material reviewed:

1. a sweep does **not** have to reclaim on the same candle;
2. entering at the next open is not the same model as waiting for a retest of the key level;
3. a plan must reject a signal whose entry has already run away;
4. the backtest must report what it skipped, not only the trades it took.

The ten external projects and the ideas accepted/rejected from each are summarised in
[`REFERENCE_REVIEW.md`](REFERENCE_REVIEW.md).

## What changed

### 1. Multi-bar sweep / reclaim

`detect_sweeps()` now accepts `reclaim_window` (default `3`):

```
level tested -> close beyond it -> close back through it within N bars = sweep
no reclaim within N bars = break, level is retired
```

The event is timestamped at the **reclaim bar**, so a CISD before the reclaim cannot use it.
`LiquiditySweep.reclaim_bars` records the delay; sweep strength gets a speed bonus.

Exposed through:

```bash
ict-trader analyze ... --sweep-reclaim-bars 3
ict-trader plan ...    --sweep-reclaim-bars 3
ict-trader backtest ... --sweep-reclaim-bars 3
```

### 2. Optional retest entry (`entry_mode="retest_key"`)

The default remains `next_open`. The optional mode waits up to `retest_max_bars` for price
to return to the CISD key level and fills there. If the level is never revisited, the signal
is counted as `not_triggered` instead of being silently dropped.

The ATM article describes this behaviour; it is now testable rather than assumed.

### 3. Plan distance gate

`plan` now rejects a setup when:

- price is already beyond the target or invalidated beyond the stop; or
- price is more than `--max-entry-distance-atr` (default `0.5`) from the entry.

Before this, the demo file returned `TRADE` at a signal whose price was ~2.85 ATR from
entry. It now returns `NO_TRADE` with `rejected="price_too_far_from_entry"`.

### 4. Discretionary target mode

`--target-mode fixed|liquidity|hybrid`:

- `fixed`: existing fixed-R target (default);
- `liquidity`: nearest unswept opposing pool, skip if it does not meet `min_target_rr`;
- `hybrid`: use the pool when valid, otherwise fixed R.

The default stays `fixed`: the liquidity-only test reduced the HK sample to zero trades and
was mixed elsewhere. It is available for research, not promoted as an improvement.

### 5. Execution diagnostics

`simulate_cisd()` now reports:

```text
signals, skipped_busy, skipped_direction, skipped_grade, skipped_confluence,
skipped_htf, skipped_risk, skipped_day_limit, skipped_daily_loss, skipped_gap,
not_triggered
```

This makes execution failures visible and prevents “no trade” from being confused with
“no signal”.

### 6. Optional ATR trailing and daily loss gate

Borrowed from the LGR / risk-manager reference implementations, but off by default:

```text
--trail-activate-r 1.5 --trail-atr-mult 1.0
--max-daily-loss-r 2.0
```

- trailing only tightens the stop, never widens it;
- `trail` exits are labelled separately from `sl`;
- daily loss is measured on **closed R**, not floating P&L;
- no default change until a separate A/B justifies it.

### 7. Skill evidence contract

`skills/ict-analysis/` now has `references/evidence_contract.md` and
`scripts/validate_plan.py`. The skill must record provenance (`source`, `as_of`,
timezone, verified/synthetic), classify evidence as observed/derived/interpretive/missing,
and use one final state: `confirmed | developing | invalidated | no-trade`.
`confirmed` requires closed-bar evidence and **net** RR >= 2.

## A/B basket (not a profitability claim)


Eight datasets: CN 15m, HK 09988, HK 00700, BTC/ETH/SOL/FIL/TUT 12k bars.
Default setup, long-only; CN uses T+1 and 5 bps sell tax.

| reclaim | entry | cases | trades | weighted avg R | total R | not triggered |
|---:|---|---:|---:|---:|---:|---:|
| 0 | next_open | 8 | 51 | -0.218 | -11.12 | 0 |
| 0 | retest_key | 8 | 50 | -0.257 | -12.83 | 3 |
| 3 | next_open | 8 | 93 | -0.143 | -13.30 | 0 |
| 3 | retest_key | 8 | 83 | -0.159 | -13.22 | 13 |

Aggregate by entry mode:

| entry | trades | win rate | weighted avg R | total R | not triggered |
|---|---:|---:|---:|---:|---:|
| next_open | 144 | 41.7% | -0.170 | -24.42 | 0 |
| retest_key | 133 | 33.8% | -0.196 | -26.05 | 16 |

Interpretation:

- multi-bar reclaim finds **more** valid sweeps (51 → 93 next-open trades) and improves the
  weighted average R, but the basket is still negative;
- retest entry is **not** a universal improvement; it reduces fills and is worse on this
  basket. It remains opt-in;
- results are diagnostic only: 8 datasets, small samples, one parameter change, no
  multiple-testing correction.

## Context-gate A/B (optional, default off)

Three optional gates were added in `context.py`:

```text
--session-gate auto     regular cash session for CN/HK; none for crypto
--pd-gate               buy only discount / sell only premium of the last confirmed range
--ote-gate               require 0.62-0.79 retracement of the signal's own range
```

Results on the same 8 datasets:

| Gate | Effect | Verdict |
|---|---|---|
| `--pd-gate` | trades fall in every case; FIL improves (17→9, avg R .194→.412), BTC becomes flat, HK 00700 less negative, but CN/SOL/TUT get worse | **not default** |
| `--ote-gate` | 0 trades on all 8 datasets; current CISD levels sit mostly at 0.30–0.62 retracement, not 0.62–0.79 | **not default**; needs a different entry model |
| `--session-gate auto` | no change on these datasets (CN/HK bars are already in cash session; crypto has no gate) | **no evidence yet**; needs off-session data |

The zero OTE result is useful: it shows the current CISD entry is not the same as a deep
OTE entry. Forcing OTE onto this setup would change the model, not improve it.

## PD arrays and inducement (optional, default off)

`pd_arrays.py` adds three independent arrays:

```text
rejection    long-wick rejection at a confirmed swing
breaker      a mitigated order block that flipped sides
mitigation   failed swing (lower high / higher low) without a sweep
```

`--require-pda-array` requires one of them near the CISD level; it is off by default.

`count_path_liquidity()` counts minor swing highs (for a long) or lows (for a short)
between entry and target. `--max-inducements N` can reject a setup with too many
obstacles on the path. This is a **path/inducement proxy**, not a claim that every
minor swing is an institutional trap.

## Execution journal

`journal.py` and the `ict-trader journal` subcommand normalise each trade into:

```text
planned vs actual entry, stop and target
entry_deviation_r, stop_deviation_r
reason, grade, quality, sample_class, rule_version
```

The backtest always fills at the plan plus modelled slippage, so it cannot detect a
discretionary rule break. The schema exists to make a live log comparable; the
`process_errors` count is only a price-deviation audit, not a complete process score.

## Quality / win-rate study (anti-overfit)

`min_target_rr` is now enforced against the **actual fill**, not the signal level.
This raises the realised R of winners because sub-2R measured targets fall back to
2R. It also lowers win rate. On the 8-dataset basket:

| Setting | trades | win rate | weighted avg R | total R |
|---|---:|---:|---:|---:|
| measured target from signal (old) | 93 | 43.0% | -0.143 | -13.30 |
| 2R from actual fill (new default) | 91 | 35.2% | -0.120 | -10.90 |

The displacement feature was the strongest in-sample separator:

| min body/range | trades | win rate | weighted avg R |
|---:|---:|---:|---:|
| 0.00 | 91 | 35.2% | -0.120 |
| 0.65 | 59 | 42.4% | +0.030 |
| 0.75 | 42 | 45.2% | +0.088 |

That looks attractive, but it is **not robust**:

- on a chronological 60/40 split, the 0.75 filter made train avg R +0.26 and
  hold-out avg R -0.24;
- leave-one-market-out results were mixed; HK 00700 stayed negative while a few
  small samples became positive;
- 30–42 trades after filtering is far below the 100-trade gate.

Therefore:

```text
--quality-profile high_value
    min_grade=4, min_confluence=3,
    min_disp_body_ratio=0.65, min_target_rr=2
```

is available as an **experimental preset only**. It is not the default, and it is not
presented as a win-rate guarantee. The default remains `standard` + 2R target.

In-sample profile result (8 datasets):

| profile | trades | win rate | weighted avg R | total R |
|---|---:|---:|---:|---:|
| standard | 91 | 35.2% | -0.120 | -10.90 |
| high_value (.65) | 30 | 50.0% | +0.033 | +0.99 |

The 30-trade profile is below the 100-trade gate. The chronological 0.65 hold-out was
negative, so the higher win rate is not evidence of a durable edge.

## Timeframe-stack A/B

The setup can now be run as `1d_4h_15m` (old default), `1d_1h_15m`, `1d_15m`,
`4h_15m` or `4h_1h_15m`.  8 datasets, long-only, 2R from actual fill:

| stack | trades | win rate | weighted avg R | total R |
|---|---:|---:|---:|---:|
| `1d_1h_15m` | 65 | 36.9% | **-0.068** | -4.45 |
| `1d_15m` | 183 | 38.8% | -0.078 | -14.36 |
| `1d_4h_15m` | 91 | 35.2% | -0.120 | -10.90 |
| `4h_15m` | 221 | 32.6% | -0.230 | -50.86 |
| `4h_1h_15m` | 96 | 32.3% | -0.244 | -23.45 |

The 4H confirmation layer was the worst of the tested stacks on this basket. 1D+1H
had the best weighted average R, while 1D+15m had far more samples and a similar
expectancy.

Chronological 60/40 hold-out:

```text
1d_1h_15m:  train -0.06 / test -0.09
1d_15m:     train -0.06 / test -0.11
1d_4h_15m:  train +0.01 / test -0.31
```

None is profitable out of sample. The stack option is therefore retained for research;
the default remains `1d_4h_15m` until a 100+ trade, walk-forward result supports a change.

## Pine origin mode: sweep_only vs sweep_or_fvg

The 1D+1H+15m stack, 8 datasets, 2R from actual fill:

| origin | trades | win rate | weighted avg R | total R |
|---|---:|---:|---:|---:|
| strict sweep (`--origin-mode strict_sweep`) | 65 | 36.9% | -0.068 | -4.45 |
| Pine `sweep or FVG` (`--origin-mode pine`) | 144 | 44.4% | **+0.110** | +15.88 |

Pine's `validLongOrigin()` allows a current-TF FVG delivery to substitute for a sweep.
That is the most plausible source of the higher-RR behaviour: it roughly doubles the
sample and improves the in-sample expectancy.

Chronological 60/40 hold-out:

```text
strict sweep:  test -0.09
Pine origin:   test -0.05
```

The Pine origin mode is therefore a **research option**, not the default. It is not a
proven edge, and in the hold-out it is still negative. The default keeps the stricter
"liquidity sweep first" rule because that is the stated core of the strategy.

### Where the Pine-origin return actually comes from

Splitting the unfiltered Pine-origin trades by the CISD checklist gives 84 FVG-only,
58 sweep+FVG and 2 sweep-only.  Running the actual `--origin-filter fvg` portfolio
(which frees capacity for later FVG signals) gives 90 trades:

| origin filter | trades | win rate | weighted avg R | total R |
|---|---:|---:|---:|---:|
| none (sweep or FVG) | 144 | 44.4% | +0.110 | +15.88 |
| `fvg` | 90 | 48.9% | **+0.236** | +21.26 |

So the positive in-sample result is almost entirely **FVG delivery**, not the
sweep+FVG combination. Chronological 60/40 hold-out of the FVG-only portfolio:

```text
train: 54 trades, 55.6%, +0.423R
test:  36 trades, 38.9%, -0.044R
```

The earlier 84-trade subset happened to be +0.02R in hold-out; the capacity-adjusted
90-trade portfolio is negative. Do not treat either 34–36 trade hold-out as proof.
It is available as:

```bash
--origin-filter fvg
```

This is a research filter, not the default. It also relaxes the sweep requirement by
design: FVG delivery is the origin. If the goal is strictly "sweep first", do not use it.

### HTF FVG vs current-TF FVG

Each FVG-origin trade now records whether an HTF FVG (1H/4H/1D) was also in delivery:

| PDA source | trades | win rate | weighted avg R | total R |
|---|---:|---:|---:|---:|
| current + HTF FVG | 82 | 48.8% | +0.237 | +19.46 |
| current FVG only | 8 | 50.0% | +0.225 | +1.80 |
| HTF FVG only | 0 | — | — | — |

The split is **not** a useful separator: 82 of 90 FVG-origin trades already have an HTF
FVG in delivery, and their expectancy is essentially the same as the 8 current-only
trades.  There is no HTF-FVG-only origin because Pine's `validOrigin` uses the current
TF FVG; the HTF FVG is a grade/PDA input, not an origin.  The 1D HTF subset is only 4
trades (+1.15R average) and is not evidence.

Conclusion: do not add an "HTF FVG only" filter.  If HTF FVG is used, it should be a
grade input, not another origin layer.

### Shallow OTE is the strongest separator so far

The classic OTE band 0.62–0.79 was tested first and produced **zero** FVG-only trades,
because FVG-delivery CISDs sit at shallow retracements.  Testing the opposite end:

```text
--ote-gate --ote-min 0 --ote-max 0.30
```

on top of `--origin-filter fvg`:

| config | trades | win rate | weighted avg R | total R |
|---|---:|---:|---:|---:|
| FVG-only | 90 | 48.9% | +0.236 | +21.26 |
| FVG-only + OTE ≤ 0.30 | 45 | **60.0%** | **+0.384** | +17.29 |

Chronological 60/40:

```text
train: 27 trades, 59.3%, +0.352R
test:  18 trades, 61.1%, +0.432R
```

On the original 8-case basket this was the first configuration that was positive in
both train and test, but 18 hold-out trades is far below 100.  It is not a default.
It also shows that "OTE" is not one universal band: for FVG-delivery entries, shallow
retracement performs better than the classic deep OTE.

### Expanded-sample check: the 18-trade result did not hold

The 8-dataset basket used short 12k crypto slices.  Re-running on the full 2025–2026
BTC history and adding SH600519 changes the picture:

| dataset / config | trades | win rate | avg R | total R |
|---|---:|---:|---:|---:|
| BTC full, FVG | 118 | 35.6% | -0.227 | -26.78 |
| BTC full, FVG + OTE ≤ 0.30 | 56 | 41.1% | -0.136 | -7.64 |
| SH600519, FVG | 11 | 45.5% | +0.123 | +1.35 |
| SH600519, FVG + OTE ≤ 0.30 | 8 | 37.5% | +0.029 | +0.23 |

On full BTC, shallow OTE improves the loss rate but **does not turn the strategy
positive**.  Monthly BTC results swing from strongly positive (2025-05, 2025-08,
2026-08, 2026-09) to strongly negative (2025-06, 2026-01, 2026-05, 2026-07), so the
earlier +0.43R hold-out was a regime/sample artifact, not a stable edge.

**Conclusion: do not promote shallow OTE to default.**  Keep it as a research
condition and require a larger, walk-forward sample before drawing a conclusion.

### Regime split: trend/volatility is not stable either

`regime.py` now records trend (`up/down/flat`) and volatility (`low/normal/high`)
from the base timeframe.  Full BTC FVG-only:

| regime | trades | win rate | avg R |
|---|---:|---:|---:|
| up + high vol | 21 | 57.1% | **+0.432** |
| up, all vol | 76 | 40.8% | -0.056 |

The `up + high` subset looks strong, but the 12k basket gives the opposite result:

| basket | up + high trades | avg R |
|---|---:|---:|
| full BTC, FVG | 21 | +0.432 |
| 12k basket, FVG | 18 | -0.077 |
| 12k basket, FVG + OTE ≤0.30 | 8 | -0.041 |

This is a regime artifact, not a stable gate.  The optional gates exist for research:

```text
--regime-trend-gate none|up|down|aligned
--regime-vol-gate none|low|normal|high
```

They are **off by default**.  Do not promote `up + high volatility` from 21 BTC trades.

### FVG Consequent Encroachment and First Presented

Course 51–52 introduces two FVG refinements:

```text
CE = (top + bottom) / 2
First Presented FVG = first gap of the displacement cluster
```

Implemented:

```text
FVG.ce
FVG.first_presented
quality_parts.fvg_ce / fvg_size_atr / fvg_first
--require-first-fvg
```

A/B on the FVG-only + OTE≤0.30 candidate:

| filter | trades | win rate | avg R |
|---|---:|---:|---:|
| none | 45 | 60.0% | +0.384 |
| require first FVG | **0** | — | — |

The strict `--require-first-fvg` rejected every trade: the nearest active FVG was
usually a later gap after the first one had already been mitigated.  Keeping it
optional; a better next step is to rank by CE/first-presented rather than hard-reject.

### FVG freshness

`--require-fresh-fvg` uses `FVG.fresh` (active and never touched by a later wick).
On the same 8-case FVG-only candidate it changed **nothing**: 45 trades before and
45 after, same win rate and R.  The FVGs that actually trigger already tend to be
fresh; the filter is therefore a guard for future data, not a source of edge.

### FVG CE entry (optional)

`--entry-mode fvg_ce` waits for the 50% CE of the nearest valid FVG instead of
entering at the next open.  On the same 8-case FVG-only + OTE≤0.30 candidate:

| entry | trades | win rate | weighted avg R | total R |
|---|---:|---:|---:|---:|
| next open | 45 | 60.0% | +0.384 | +17.29 |
| FVG CE | 33 | 57.6% | +0.413 | +13.64 |

CE slightly improves average R but loses total R and 12 trades; HK 09988 gets worse,
CN and ETH get better.  Not a default.  It is a valid execution variant to test on a
larger, out-of-sample sample.

### Silver Bullet windows (optional)

The Versus Trade / ICT article specifies New York local windows:

```text
03:00–04:00  London open
10:00–11:00  NY AM
14:00–15:00  NY PM
```

Added `--session-gate silver_bullet`.  On the FVG-only + OTE≤0.30 candidate:

| gate | trades | win rate | weighted avg R | total R |
|---|---:|---:|---:|---:|
| none | 45 | 60.0% | +0.384 | +17.29 |
| silver_bullet | 8 | 50.0% | +0.508 | +4.07 |

This is a larger average R but a **lower win rate** and only 8 trades.  The article
itself warns that time windows reduce eligible setups and do not necessarily raise
win rate or RR.  It remains an optional research gate, not the default.

An earlier run showed 12 trades / 75% because naive crypto timestamps were interpreted
as New York time.  Binance 15m timestamps are UTC; after fixing the timezone conversion
the corrected result is the 8-trade table above.


### Grid result: the best sample combo

A 16-config grid over two stacks, two origins, two OTE modes and 2.0/2.5R was run on
the 8-case basket.  The best combination was:

```text
1D+1H+15m / FVG-only / OTE 0.00-0.30 / 2R
45 trades, 60.0% win, +0.376R average, +16.91R total
```

Full details and the full-BTC failure are in [`BEST_SETUP.md`](BEST_SETUP.md).  It is a
research candidate, not a default.

### Draw-on-liquidity target

`--target-mode dol` now selects the nearest unswept liquidity level known at the signal
bar, not a level that gets swept later in the file.  The earlier target filter used the
final `swept` state and was retroactively deleting valid targets; `sweep_bar` and
`broken_at` fix that.

| instrument | fixed 2R | DOL |
|---|---:|---:|
| Xiaomi HK.01810 | 30 trades, +0.132R | 15 trades, +0.255R |
| Zijin HK.02899 | 26 trades, -0.009R | 10 trades, +0.137R |
| 8-case basket | 45 trades, +0.384R | 29 trades, +0.037R |

DOL improves the two HK names but is much worse across the crypto-heavy basket.  It is
optional; the default target remains fixed 2R.  See [`ICT_CHECKLIST.md`](ICT_CHECKLIST.md).










## Next candidates (not implemented)




These need separate A/B tests before becoming defaults:

1. premium/discount gate from a frozen dealing range;
2. OTE 0.62–0.79 as an entry filter;
3. killzone/session gate with per-market IANA time zones;
4. EQH/EQL tagging and inducement;
5. Breaker/Mitigation/Rejection as separate PD arrays;
6. execution journal fields (planned vs actual fill, rule deviation, cooling-off).
