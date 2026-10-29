# ICT Setup Research: evidence-weighted synthesis

Last updated: 2026-10-29

This is not a “51 setups” document.  It is an evidence-weighted synthesis of **383
full-text articles across 14 sources**, filtered down to a small number of testable
setups.  Article count is not evidence; source quality and reproducibility are.

## 0. Scope and source tiers

| Tier | Sources | Full articles | Use |
|---|---|---:|---|
| A — primary ICT / model | Inner Circle Traders, TTrades, Stoic Edge, Trading Strategy Guides | 127 | rules and model logic |
| B — applied / crypto / tools | ePlanet, Altrady, Volity, Flux, Trade The Pool, ITBFX | 110 | confluence and market adaptation |
| C — generic / execution | Vantage, Tradervue, Finxsol, 4xPip | 146 | risk, journaling, warnings |
| Blocked | ChartingLens | 0 | summary only; not evidence |

The Vantage/Tradervue/4xPip counts are large but generic.  They do **not** vote on
what an ICT entry is.  ChartingLens is Cloudflare-blocked and is not counted as read.

## 1. High-consensus rules

Coverage is by source family, not by article count.  “High” means the rule appears in
the primary sources and is stated operationally.

| Rule | Coverage | Consistency | Decision |
|---|---|---|---|
| HTF / daily bias before entry | 12/14 | high | keep |
| Draw on Liquidity (DOL) as target | 10/14 | high | keep |
| Liquidity sweep of an obvious level | 14/14 | high | keep |
| Displacement / forceful candle | 11/14 | high | keep |
| MSS / CHoCH / CISD confirmation | 12/14 | high | keep |
| FVG as an entry zone | 13/14 | high | keep, but not as the only reason |
| Order Block | 12/14 | high | keep |
| Kill Zone / session filter | 9/14 | medium-high | keep configurable |
| OTE 0.62–0.79 | 7/14 | medium | optional |
| Breaker / mitigation | 8/14 | medium | optional confluence |
| Judas / AMD | 6/14 | medium | research filter |
| SMT divergence | 4/14 | low | data-dependent, not a hard gate |
| Candle 2 / Candle 3 closure | 1/14 (TTrades) | low | test as bias filter |
| Range sweep / break-retest | 1/14 (Stoic) | low | test as separate model |
| Protected swing stop | 2/14 | low | test against structural stop |

## 2. Isolated or contested rules

| Rule | Source | Why it is not adopted directly |
|---|---|---|
| “Sweep” and “grab” are different setups | Finxsol, Flux | Official ICT and Inner Circle Traders treat them as synonyms; no standard definition |
| Asian range 20:00–00:00 NY | ChartingLens summary | conflicts with 19:00–00:00 and 19:00–02:00 versions |
| Asian range 23:00–06:00 UTC | ITBFX | a UTC rendering, not a universal session |
| Minimum RR 1:3 or 1:5 | ICT course / Silver Bullet articles | no cross-market evidence; current 2R baseline is more stable |
| Stop at Candle 2 extreme | Trading Strategy Guides | conflicts with sweep-extreme and protected-swing stops; needs A/B |
| FVG must be “first presented” | ICT course | strict gate rejected 45/45 trades in our sample |
| FVG must be “fresh” | ePlanet, TSG | plausible, but only metadata so far; needs A/B |
| ICT = SMC plus layers | ePlanet, Altrady | useful taxonomy, not a trading rule |
| “70–80% of FVGs fill” | ePlanet | unsourced statistic; do not encode |
| “6-hour FVG validity” | scrape summary | not in the ePlanet article; rejected |
| Earnings-gap FVG | Trade The Pool | different object from a 3-candle intraday FVG |
| Unicorn = breaker + FVG | Flux | useful label, no measured edge |
| Turtle Soup = sweep + MSS | Flux | overlaps with core sweep+CISD; not a separate edge |

### Conflict rulings

1. **Sweep vs grab**: treat “sweep” as the price event.  Do not build a separate grab
   strategy without a frozen, testable definition.
2. **Asian window**: keep one configured window, record it in the journal, and A/B
   the alternatives.  Never average the definitions.
3. **Minimum RR**: 2.0 is the hard floor; 2.5 is preferred; 3.0 is not universal.
4. **Stop**: sweep extreme is the default for the core model.  Candle-2 and
   protected-swing stops are challengers, not defaults.
5. **FVG**: rank by freshness and CE; do not hard-reject non-first FVGs.
6. **MSS/CISD**: required for the strict model; the simplified Keyshot model is a
   separate model and must not inherit strict-model results.

## 3. Current strategy audit

Current engine:

```text
stack        1D/1H/15m or 1D/4H/15m
bias         HTF cloud + TK + MACD
liquidity    key levels + sweeps
entry        sweep + CISD, optional FVG/OB
target       fixed R or DOL
costs        commission + slippage + sell tax
gate         net RR >= 2, session, optional PD/OTE
default      long-only
```

Strengths:

- closed-bar signals, point-in-time levels, no future sweep deletion;
- net RR and costs are explicit;
- strict audit can return `NO_TRADE`;
- FVG/OB/breaker/mitigation/rejection are separate modules;
- sample-size gate exists.

Gaps:

- FVG freshness is metadata only, not ranked;
- CE is metadata only, not an entry mode;
- no Candle 2/3 bias filter;
- no range break-retest model;
- no protected-swing stop mode;
- no partial-at-DOL management;
- no MFE/MAE journal fields;
- no correlation/SMT data feed.

In-sample results do **not** establish an edge:

| Test | Trades | Win rate | Avg R | Status |
|---|---:|---:|---:|---|
| Full BTC FVG-only | 118 | 35.6% | -0.227 | negative |
| Full BTC FVG + shallow OTE | 56 | 41.1% | -0.136 | negative |
| 8-case FVG + shallow OTE | 45 | 60.0% | +0.384 | in-sample only |
| 8-case Silver Bullet (UTC fixed) | 8 | 50.0% | +0.508 | too few |
| 8-case strict first FVG | 0 | — | — | rejects all |

## 4. Candidate setups

### Setup A — Sweep + CISD reversal (priority 1)

One sentence: liquidity is taken at an obvious level, then a closed-bar CISD proves
delivery changed before entry.

| Field | Rule |
|---|---|
| Context | HTF bias and DOL aligned; session active; price at a real pool |
| Trigger | sweep of swing/PDH/PDL/EQH/EQL, close back inside |
| Confirmation | CISD closes through the opposing candle series; displacement present |
| Entry | next bar open or retest of the CISD origin |
| Stop | beyond the sweep extreme |
| Target | opposing DOL; partial at DOL |
| Filters | net RR >= 2; no counter-HTF trade; entry distance <= 0.5 ATR |
| Invalidation | close back through CISD origin, or sweep extreme violated |
| Evidence | rule consensus high; no published backtest |
| Expected | unproven; plan for 35–45% at 2R, confidence low |
| Score | clarity 5, evidence 2, executable 5, fit 5 |

### Setup B — Fresh FVG CE retest (priority 2)

One sentence: after displacement, wait for a fresh FVG and enter at its 50% CE, not at
the outer edge.

| Field | Rule |
|---|---|
| Context | bias + DOL + displacement; FVG unmitigated |
| Trigger | price returns to CE |
| Confirmation | rejection close at CE; structure not broken |
| Entry | CE limit or confirmation close |
| Stop | beyond FVG far edge or Candle-2 extreme (A/B) |
| Target | opposing liquidity / DOL |
| Filters | fresh FVG; CE distance <= 0.5 ATR; net RR >= 2 |
| Invalidation | full fill and close beyond the gap |
| Evidence | execution detail strong; no stats |
| Expected | unproven; do not size as a proven edge |
| Score | clarity 4, evidence 2, executable 4, fit 4 |

### Setup C — Candle 2/3 closure bias filter (priority 3)

One sentence: use a sweep-and-close-back-inside on the daily/4H candle as a bias
filter, then execute with Setup A.

| Field | Rule |
|---|---|
| Context | daily or 4H; previous candle high/low |
| Trigger | Candle 2 sweeps prior low/high and closes back inside |
| Alternative | if C2 fails, C3 engulfs C2 and closes through its body |
| Confirmation | lower-timeframe CISD |
| Entry | Setup A |
| Stop | protected swing or sweep extreme |
| Target | DOL |
| Invalidation | failure swing: deep retracement without taking the extreme |
| Evidence | one primary source; no stats |
| Expected | filter, not standalone; confidence low |
| Score | clarity 3, evidence 1, executable 3, fit 4 |

### Setup D — Range sweep / break-retest (priority 4)

One sentence: mark a defendable consolidation range, then trade either its failed
boundary (sweep-retest) or its held breakout (break-retest).

| Field | Rule |
|---|---|
| Context | clear range with at least two touches per side; expansion regime |
| Trigger A | sweep boundary, close back inside, retest |
| Trigger B | break boundary, hold retest, continue |
| Confirmation | CISD or structure shift at the boundary |
| Entry | retest of boundary/CE |
| Stop | other side of the trigger bar or range boundary |
| Target | range midpoint first, then external liquidity |
| Filters | range width >= 1 ATR; no mid-range entries |
| Invalidation | close back through the boundary after breakout |
| Evidence | one high-quality source; no stats |
| Expected | separate model; confidence low |
| Score | clarity 4, evidence 2, executable 4, fit 3 |

### Observation-only, not setups

```text
Unicorn (breaker + FVG)
Turtle Soup (sweep + MSS)
SMT divergence
Judas Swing
```

They are labels/confluence.  No hard-coded entry until a frozen definition and a
sample exist.

## 5. Stability and risk design

```text
risk per trade       1.0% equity
daily loss cap       3.0%
rolling weekly cap   6.0%
drawdown pause      10.0%
max open risk        3.0%
consecutive losses   3 -> half size; 5 -> stop for the day
```

Signal grades:

| Grade | Requirements | Size |
|---|---|---|
| A | HTF + DOL + session + sweep + CISD + net RR >= 2.5 | 1.0R |
| B | all except one confluence; net RR >= 2.0 | 0.5R |
| C | research/observation only | 0 (paper) |

Journal fields:

```text
signal_id, date, symbol, timeframe, session, setup, grade
htf_bias, daily_bias, dol, key_level, sweep_time, sweep_extreme
cisd_time, cisd_origin, fvg_ce, fvg_fresh, entry, stop, target
gross_rr, net_rr, risk_pct, position_size, result_r
mfe_r, mae_r, exit_reason, rule_break, screenshot_path
```

## 6. One-page strategy

```text
HTF bias
  -> DOL
  -> session / kill zone
  -> liquidity sweep at a real level
  -> displacement + CISD confirmation
  -> entry at CISD retest or fresh FVG CE
  -> stop beyond sweep / protected swing
  -> target opposing DOL
  -> partial at DOL, trail remainder
  -> journal MFE/MAE and rule breaks
```

If any line is missing:

```text
NO_TRADE
```

## 7. Diff against current strategy

| Item | Action | Why | Expected effect |
|---|---|---|---|
| HTF bias / DOL | keep | core consensus | stability |
| Sweep + CISD | keep | highest rule consensus | fewer false reversals |
| FVG mandatory | delete | FVG alone has no edge | fewer forced trades |
| FVG fresh / CE | add/rank | repeated execution detail | better entry precision |
| First-presented hard gate | keep optional only | it rejected all 45 | avoid over-filtering |
| Candle 2/3 | add as filter | TTrades model | better daily bias |
| Protected swing stop | A/B | TTrades / Stoic | tighter invalidation |
| Candle-2 stop | A/B | TSG | compare with sweep stop |
| Partial at DOL | add | ICT 2022 / TSG | lock profit, keep runner |
| Range break-retest | add optional | Stoic | new regime coverage |
| SMT hard gate | do not add | no data feed, low consensus | avoid fake precision |
| Minimum RR 3 | do not adopt | no cross-market evidence | keep 2 floor |
| Unicorn / Turtle | labels only | overlap with core | avoid double counting |

## 8. Validation plan

### Experiments

1. **Fresh FVG A/B**: baseline Sweep+CISD vs baseline + fresh-FVG requirement.
2. **CE entry A/B**: CISD-next-open vs FVG-CE limit.
3. **Stop A/B**: sweep extreme vs Candle-2 extreme vs protected swing.
4. **Candle 2/3 filter**: baseline vs C2/C3-aligned only.
5. **Partial management**: fixed 2R vs partial at DOL + trail.

### Data

```text
BTC/ETH/SOL 15m, 2020–2026
HK 00700/09988 15m
CN example 15m
optional FX 15m if a clean feed exists
```

### Metrics

```text
trades, win rate, avg R, net expectancy, profit factor, max drawdown
MFE/MAE, signal frequency, no-trade rate, rule-break count
```

### Gates

```text
<20 trades       observation only
20–50            review execution, no edge claim
50–100           one parameter change maximum
>100             purged walk-forward before promotion
```

No promotion on in-sample results.  Report the sample class every time.

## 9. Next three actions

1. Add a `fresh_fvg` gate and run the 8-case A/B.
2. Add `entry_mode=fvg_ce` without changing the default.
3. Add a Candle-2 closure column to the daily context, then test as a filter.

If those fail, stop adding features.  The correct output remains `NO_TRADE`.
