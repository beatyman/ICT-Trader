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

## Next candidates (not implemented)


These need separate A/B tests before becoming defaults:

1. premium/discount gate from a frozen dealing range;
2. OTE 0.62–0.79 as an entry filter;
3. killzone/session gate with per-market IANA time zones;
4. EQH/EQL tagging and inducement;
5. Breaker/Mitigation/Rejection as separate PD arrays;
6. execution journal fields (planned vs actual fill, rule deviation, cooling-off).
