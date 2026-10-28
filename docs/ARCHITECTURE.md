# Architecture

## Data flow

```text
CSV / Futu
   │
   ▼
load_csv ──► validate OHLCV
   │
   ▼
concepts.analyze
   ├─ swings / zigzag
   ├─ key levels (BSL/SSL/PDH/PDL/PWH/PWL/Asia/London)
   ├─ sweeps (wick + reclaim, known_at aware)
   ├─ FVG / IFVG / OB
   ├─ CISD (Pine-style state machine)
   └─ HTF CISD / SMT / sessions
   │
   ▼
setup.build_setup
   ├─ 1D Ichimoku + MACD (3/3)
   ├─ 4H Ichimoku + MACD (>= 2/3)
   ├─ 15m CISD + recent sweep + PDA + displacement
   └─ weighted quality score
   │
   ▼
strategy.simulate_cisd
   ├─ next-bar entry
   ├─ SL / TP / time stop / BE
   ├─ A-share T+1 + stamp duty
   ├─ commission + slippage
   └─ MAE / MFE journal
   │
   ▼
walk_forward.purged_walk_forward
```

## Invariants

1. **No look-ahead.** A level has `known_at`; a signal uses only bars up to
   `c.end`; entry is `c.end + 1`.
2. **Closed-bar confirmation.** CISD is produced on the confirmation close.
3. **One position per symbol.** `busy_until` blocks overlapping entries.
4. **T+1 is a hard constraint.** Same-day exits are impossible for A-shares.
5. **Costs are explicit.** Commission, slippage, stamp duty and gap fills are
   in the R calculation.
6. **A `NO_TRADE` is first-class.** The setup must be recent and pass every
   gate; otherwise the plan is empty.

## Why not one giant script

- `concepts.py` is allowed to be feature-rich and exploratory.
- `setup.py` is the one frozen trading decision.
- `strategy.py` is the execution/accounting boundary.
- `walk_forward.py` is the validation boundary.

That separation lets us change a chart feature without silently changing the
risk model.

## Known limitations

- Current CSI300 sample uses current constituents (survivorship bias).
- Limit-up/down, suspension and borrow costs are not yet simulated.
- HTF resampling is an approximation, not TradingView `request.security`.
- The 15m A-share sample is small; results are directional only.

## Quality gates

```bash
pytest
ict-trader plan --csv examples/15m.csv --daily-csv examples/daily.csv
ict-trader backtest --csv examples/15m.csv --daily-csv examples/daily.csv --t-plus-1
ict-trader walk-forward --trades output/trades.csv
```
