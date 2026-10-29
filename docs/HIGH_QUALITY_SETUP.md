# High-quality setup & high-RR synthesis

Synthesized from 383 full-text articles (14 sources), read by source family.
This is the rule consensus, not a backtest. Only rules that are both
cross-source and operationally reproducible survive to become a filter.

## 1. What makes a high-quality setup (cross-source consensus)

A setup earns its A-grade only when all of these hold. Each is a separate,
ablated gate in the engine.

| # | Condition | Quantifiable | Source coverage |
|---|---|---|---|
| 1 | HTF bias aligned | 1D/4H structure + DOL direction matches the entry | 12/14 |
| 2 | Draw on Liquidity exists | a structural pool (EQH/PDH/PWH or EQL/PDL/PWL) is the target | 10/14 |
| 3 | Liquidity swept | price took an obvious level before reversing | 14/14 |
| 4 | MSS/CHoCH/CISD confirmed | body-close through the opposing candle series, not a wick | 12/14 |
| 5 | Displacement present | large-bodied candle, small wicks, leaves an FVG | 11/14 |
| 6 | Correct premium/discount half | long only < 50% EQ, short only > 50% EQ | high |

Freshness is a sub-condition of the FVG/OB: a mitigated FVG/OB is deleted, not
traded (`retest_count <= 1`, virgin mitigation only).

## 2. What makes a high-RR trade (cross-source consensus)

Three levers, used together:

1. **LTF entry + HTF target.** Stop and entry come from the execution
   timeframe; target comes from the HTF Draw on Liquidity. Shrinks the risk
   denominator and stretches the reward numerator.
2. **CE / mean-threshold entry.** Enter at the 50% level of the FVG gap or the
   OB body, not the outer edge. Halves the stop distance, roughly doubles RR.
3. **Structural stop.** Stop at the swept extreme / protected swing / C2
   extreme, never a fixed pip distance, never widened to force a number.

## 3. RR thresholds (cross-source)

- Hard floor **2.0** (breakeven win rate ~34%).
- Preferred **2.5**.
- **3.0+** is the A-grade expectation, not a universal rule (conflicting
  sources; 2.0 stays the floor).
- DOL targets routinely deliver **3R–7R** (previous-day level), **10R+** on
  held expansion closes.
- Breakeven table: 1:2 → 34%, 1:3 → 25%, 1:5 → 17%. The edge is structural RR,
  not win rate.

## 4. Risk & execution discipline (cross-source, non-negotiable)

```text
risk per trade      1% (2% hard cap)
weekly risk budget  3–5% (correlated positions summed)
drawdown breaker    soft -8% half size; hard -15% stop 2 weeks
stop                ATR-or-structure, never arbitrary, never widened
no martingale/grid  2x ladder blows up in trend (4xPip)
```

## 5. Crypto adaptation (Altrady, explicit)

- No clean kill zone in 24/7 crypto → session filters must be re-tested per
  pair, not copied from forex.
- Sweeps run further → wider stops + smaller size, or the stop is wicked out
  while the idea is still valid.
- High leverage + correct direction still gets liquidated on the sweep. Size
  from stop distance, not available leverage.
- Only liquid pairs (BTC/ETH/SOL) for structure plays; thin alts dropped.

## 6. What this means for the engine

The baseline backtests were all negative at a fixed 2R target with ~35–41%
win rate. The corpus says the edge is **not** in win rate; it is in structural
RR: enter at CE with a structural stop and target the HTF DOL (3R–7R). A 30%
win rate at 3R is profitable; 35% at 2R is not.

Optimization direction (implemented as `quality_profile="high_rr"`):

```text
HTF bias aligned (stack)
  + DOL target (target_mode=dol, HTF-direction pool)
  + sweep + CISD (body-close)
  + displacement body/range >= 0.65
  + fresh FVG (unmitigated)
  + min target RR >= 2.5
```

Each lever is already an independent gate; the profile wires them together so
the A/B is one change, not six.
