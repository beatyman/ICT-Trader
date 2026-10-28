# LuxAlgo ICT Concepts — Pine parity notes

Source: `ICT Concepts [LuxAlgo]`, CC BY-NC-SA 4.0. This is a **detector/indicator**,
not a strategy: it contains no `strategy.entry`, stop, target, position sizing or risk
model. It therefore cannot produce an RR by itself.

## What the Pine actually contains

| Pine block | Purpose | Our equivalent |
|---|---|---|
| `aZZ` / `draw()` | pivot zigzag, MSS/BOS | `find_pivots`, `build_zigzag`, `detect_structure` |
| `L_bodyUP/DN` | body dominance + small wicks | `detect_fvgs` displacement filter, `measure_displacement` |
| `bFVG_UP/DN` | 3-candle FVG with body displacement | `detect_fvgs`, `FVG` |
| `bBPR_UP/DN` | overlap of latest bull/bear FVG | `detect_bpr` |
| `bullish_ob` / `bearish_ob` | OB from a swing break, then mitigated/breaker | `detect_order_blocks`, `pd_arrays.detect_breakers` |
| `b_liq_B/S` | >=3 equal pivots near a level | `detect_liquidity` clusters, `EQH/EQL` tags |
| NWOG/NDOG | Monday/week open gaps | `detect_gaps`, `NWOG/NDOG` |
| Killzones | NY / London / Asia sessions | `context.in_session` (different defaults) |
| `plotshape(sDispl)` | visual displacement marker | diagnostic only |

## Deliberate differences

- Pine's liquidity margin is `atr/4` (`a=4`); our default is `liq_margin_atr=2.5`.
- Pine uses OB swing length 10; our setup uses `swing_len=5` by default.
- Pine's killzones are NY 07–09, London 07–10, London close 15–17, Asia 10–14 in their
  native time zones. Our `context.py` uses a different, configurable set. Do not mix
  them without converting to one timezone.
- Pine's "Present" mode only plots the last 500 bars; that is a display window, not a
  backtest filter.
- Pine's MSS/BOS is a close-through-pivot event; it does not require a sweep. Our
  `SetupConfig` can require a sweep independently.

## Origin semantics

Pine's `validLongOrigin()` is:

```text
(sweep OR current-TF FVG delivery) AND grade filter
```

It does **not** allow an SMT alone as an origin; SMT is one of the grade checks.
Our `cisd.py` now uses the same origin rule:

```python
origin_ok = swept or pda
```

The default strategy is stricter than Pine because `SetupConfig.require_sweep=True`
also requires a recent sweep in `build_setup`. To test the Pine origin:

```bash
ict-trader backtest ... --origin-mode pine --stack 1d_1h_15m
```

`--origin-mode pine` is equivalent to `--allow-no-sweep` but states the intent.
It still requires either a sweep or an FVG delivery inside the CISD; it is not a
"sweep is optional for every setup" switch.

The 144 Pine-origin trades split into 84 FVG-only, 58 sweep+FVG and 2 sweep-only.
`--origin-filter fvg` keeps the FVG-only origin; because it frees portfolio capacity
for later FVG signals, the actual filtered run has 90 trades (not 84). It is a research
filter, not the default. See `docs/OPTIMIZATION.md` for the A/B and the hold-out caveat.

## How to backtest a Pine strategy

1. Export OHLCV in the Pine timeframe (or resample from 15m).
2. Port the **entry**, **stop**, **target** and **management** rules — not just the
   indicator overlays.
3. Check every `request.security` call for `lookahead_off` and closed-bar semantics.
4. Compare on identical bars, costs and position sizing.
5. Keep the Pine and Python results side by side; a visual match is not an execution
   match.

If you provide the specific strategy `.pine` (with entries/exits), the port can be
done rule-by-rule. Without entry/exit rules, the only honest output is a detector
parity review, not an RR backtest.
