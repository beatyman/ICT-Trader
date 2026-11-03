# BTCUSDT perpetual - can the engine produce >50% win at >=2R net?

Short answer: **no, not on this data**.  This file records what was measured, so
the next person does not repeat it.  Every number here is reproducible with the
two scripts named in the commands.

## 1. Question

Target: a configuration that wins **more than 50%** of trades while each winner
pays **at least 2R net** (taker 0.05%/side + 1 bps slippage/side), long **and**
short.  If such a configuration existed at a usable sample size, the expectancy
would be `0.5*2R - 0.5*1R = +0.5R` per trade, i.e. an extraordinary edge.

## 2. Data and cost basis

| item | value |
|---|---|
| data | `output/ict/btcusdt_15m_full.csv` 64,999 bars, 2024-12-24 15:15 -> 2026-11-01 16:45 UTC |
| HTF | `btcusdt_1d_full.csv` 999 bars, `btcusdt_1h_full.csv` 16,999, `btcusdt_4h_full.csv` 4,999 |
| cost | `commission_bps=10` + `slippage_bps=1` = 12 bps round trip exactly |

**Read the cost convention carefully.** `commission_bps` is charged as **one round
trip** (`(entry+exit)*commission_bps/2` in the simulator, the same in `net_rr`
since the fix described below); `slippage_bps` is charged **per side**.  The
engine default of `5` therefore means 0.05% *round trip*, which is half of
Binance futures' 0.05% **per side**; to model 0.05%/side pass `10`.  The studies
in this file ran with `slippage_bps=2` (2 bps/side, i.e. 14 bps round trip),
which is 2 bps more conservative than the 12 bps of 0.05%/side + 1 bps/side.

`net_rr` used to charge the full `commission_bps` on **both** legs while the
simulator charges half per leg, so the same config cost twice as much in the
plan card as in the backtest (0.59R vs 0.29R on a 262-point stop at 77k).  Fixed:
the two now agree to the point, pinned by
`tests/test_btc_gates.py::test_commission_is_one_round_trip_of_commission_bps`.

## 3. What the cost does to a 15m stop

At BTC 78,150 with ATR(14, 15m) = 90.3, the round-trip cost is **93.78 USD**, so
the cost in R depends entirely on how wide the stop is:

| stop | risk (pts) | cost in R | break-even win @2.5R |
|---|---:|---:|---:|
| 0.5 ATR | 45.1 | **2.08R** | 87.9% |
| 1.0 ATR | 90.3 | **1.04R** | 58.3% |
| 1.4 ATR | 126.4 | **0.74R** | 49.8% |
| 2.0 ATR | 180.5 | **0.52R** | 43.4% |
| 3.0 ATR | 270.8 | **0.35R** | 38.5% |

With a sub-1-ATR stop on a 15m chart the trade is **already lost to fees** before
the direction is decided.  This single line explains most of the negative
results below, and it is the strongest argument for either a wider stop or a
higher base timeframe.

## 4. Method (no in-sample storytelling)

1. `scripts/optimize_btc.py` builds **one** signal pool with the *frozen* setup
   (no gate relaxed - an earlier draft relaxed gates and produced rules that a
   `quality_profile` could not reproduce), simulates every signal with identical
   execution rules, and attaches the signal-time features to each trade.
2. `scripts/rule_search.py` tests **1,793** conjunctions of at most three
   thresholds on the TRAIN slice (2024-12 -> 2026-04) and applies the surviving
   rules, with **fixed** thresholds, to the HOLDOUT slice (2026-04 -> 2026-11).
3. A guard rejects any column that is only known after the trade
   (`mfe_r`, `mae_r`, `bars`, ...).  This mattered: an early version used
   `mfe_r > 1` and "discovered" a 94% holdout win rate - pure look-ahead.

## 5. Results

Pool, frozen setup, 15m base, net of the costs above:

| slice | trades | win | avg R | PF |
|---|---:|---:|---:|---:|
| train | 101 | 34.7% | -0.211 | 0.69 |
| holdout | 39 | 28.2% | -0.251 | 0.65 |
| full 22 months | 146 | 30.8% | **-0.358** | 0.55 |

That is the base rate: **at 2.5R, a break-even trade needs ~50% wins with these
costs, and the engine delivers 28-35%.**

Of 1,793 rules, 19 cleared the in-sample bar (>=25 trades, >=50% win, >=+0.2R).
The best three on the holdout:

| rule | train n/win/avgR | holdout n/win/avgR |
|---|---|---|
| `sweep_penetration>=0.3 and stop>1.4ATR and grade>=4` | 25 / 52.0% / +0.204 | 10 / **30.0%** / -0.235 |
| same with `confluence>=3` | 25 / 52.0% / +0.204 | 10 / **30.0%** / -0.235 |
| `sweep_penetration>=0.3 and stop>1.4ATR and disp_multiple>1.5` | 37 / 51.3% / +0.264 | 13 / **15.4%** / -0.687 |

**Every in-sample winner failed out of sample.**  One rule survived its *first*
holdout (displacement >= 1.45 ATR + stop >= 1.4 ATR: train 21 / 52.4% / +0.19R,
holdout 6 / 50.0% / +0.45R) and then failed the wider test:

| sample | trades | win | avg R | PF |
|---|---:|---:|---:|---:|
| full 22 months, same rule | 23 | 39.1% | -0.084 | 0.85 |
| full 22 months, CLI reproduction | 23 | 43.5% | +0.028 | 1.05 |

Six holdout trades and a 0.03R full-sample expectancy are noise, not an edge.

Other directions tried and rejected (all 15m unless stated):

| hypothesis | train | holdout | verdict |
|---|---|---|---|
| strong sweep `strength>=3` | 54.9% / +0.28R | 13.3% / -0.59R | overfit |
| `stop>2ATR` only | 43% / -0.02R | 16% / -0.58R | worse |
| POSITION: PD gate + OTE 0.5-0.9 | 29% / -0.56R | 3 trades | too few |
| regime aligned + high vol | 20% / -0.60R | 2 trades | too few |
| daily-bias filter / first+fresh FVG / inducement<=0 | **0 signals** | 0 | kills everything |
| HTF-CISD alignment (`require_htf`) | 8 trades | 2 trades | too few (4 HTF CISDs) |
| 1h base, frozen setup, rr=2.5 | - | - | n=51, 39.2% win, **+0.072R**, PF 1.12 |
| 1h base + `sweep>=3` | 32% / -0.21R | 35% / -0.06R | the +0.246R full-sample reading was a windowing artefact |

The 1h base is the only place where the *base* configuration is not negative,
and it is still below the 50% win rate: wider stops do not create an edge, they
stop paying fees for the privilege of finding out.

### 5.1 The win-rate / RR frontier (why the two requirements fight)

`btc_15m` gates, full 22 months, net costs, `max_bars=48`:

| base | RR | n | win | avg R | total R | PF |
|---|---:|---:|---:|---:|---:|---:|
| 15m | 1.0 | 23 | **52.2%** | -0.090 | -2.1 | 0.82 |
| 15m | 1.5 | 23 | 47.8% | -0.029 | -0.7 | 0.94 |
| 15m | 2.0 | 23 | 43.5% | +0.028 | +0.6 | 1.05 |
| 15m | 2.5 | 23 | 39.1% | -0.084 | -1.9 | 0.85 |
| 15m | 3.0 | 23 | 39.1% | +0.003 | +0.1 | 1.00 |
| 1h | 1.0 | 14 | **71.4%** | +0.268 | +3.7 | 1.86 |
| 1h | 2.0 | 14 | 50.0% | -0.026 | -0.4 | 0.95 |
| 1h | 2.5 | 14 | 50.0% | -0.065 | -0.9 | 0.88 |

A win rate above 50% appears only when RR drops to about 1R - which is exactly
the region the fee table in section 3 makes unprofitable on the 15m chart.  The
two requirements in the original goal ("win > 50%" and "RR > 2") are not
independent wishes; on this instrument they point in opposite directions.

### 5.2 The 1h base with a 1.4 ATR stop (the follow-up test)

Widening to the 1h base and forcing `stop >= 1.4 ATR` at RR 2.5 - the one
direction the fee table supports - was measured and **does not work either**:

| base | gates | n | win | avg R | PF |
|---|---|---:|---:|---:|---:|
| 1h | frozen setup, nothing added | 51 | 39.2% | **+0.072** | 1.12 |
| 1h | `stop >= 1.4 ATR` | 38 | 36.8% | -0.066 | 0.89 |
| 1h | `stop >= 2.0 ATR` | 24 | 41.7% | -0.045 | 0.92 |
| 4h | `stop >= 1.4 ATR` | 8 | 12.5% | -0.431 | 0.36 |

The gate that helped on 15m **hurts** on 1h, and the 1h base itself is not
robust: split into halves it gives -0.241R (first 10,000 bars) and -0.080R (last
6,999), so the +0.072R full-sample figure is not reproducible within its own
history.  The 4h base has 10 signals in two years - not a sample at all.

### 5.3 How much sample would "push for more sample" actually need?

`src/ict_trader/stats.py` now ships the arithmetic, and every backtest reports
it (`win_rate_ci95`, `avg_r_ci95`, `n_for_5pct_win_rate`):

| question | answer |
|---|---|
| win rate within +/-10 points at 95% | **97 trades** |
| win rate within +/-5 points at 95% | **385 trades** |
| distinguish 50% from the measured 39.2% (80% power) | **332 trades per arm** |
| BTC 1h produces | **~25 trades / year** |
| so +/-5 points on 1h needs | **~15 years of BTC 1h history** |

Measured intervals:

| config | n | win (95% CI) | avg R (95% CI) |
|---|---:|---|---|
| 15m `btc_15m` | 23 | 43.5% (25.6% - 63.2%) | +0.028 (-0.479 - +0.535) |
| 1h frozen | 51 | 39.2% (27.0% - 52.9%) | +0.072 (-0.335 - +0.480) |

Both average-R intervals straddle zero.  **No honest claim about a 50%+ win rate
can be made from this dataset at any timeframe**, and the amount of data needed
to make one does not exist on a single symbol: it needs ~8 correlated symbols
over the same two years, or 15 years of BTC.

### 5.4 Cross-symbol test: the rules work - in exactly one regime

Binance is unreachable from this environment (its DNS answer is unroutable), so
`scripts/fetch_gate.py` pulls the same instruments from Gate.io futures, and
`scripts/cross_symbol_validate.py` applies the BTC-derived rules **unchanged**
to nine symbols it never saw: BTC, ETH, SOL, BNB, XRP, DOGE, ADA, AVAX, LINK.

Data: 1h 10,001 bars/symbol (2025-09-12 -> 2026-11-03) and 15m 9,801 bars/symbol
(2026-07-24 -> 2026-11-03; Gate serves only the last 10,000 candles).  Costs and
RR as everywhere else (0.05%/side + 1bps/side, RR 2.5).

Pooled over nine symbols:

| rule | n | win (95% CI) | avg R (95% CI) | PF |
|---|---:|---|---|---:|
| frozen setup | 303 | 38.0% (32.7-43.5%) | -0.006 (-0.167 - +0.154) | 0.99 |
| `sweep>=3 & stop>2ATR & grade>=4` | 56 | **57.1%** (44.1-69.2%) | **+0.397** (+0.067 - +0.728) | 2.12 |
| `pen>=0.3 & stop>1.4ATR & grade>=4` | 72 | **55.6%** (44.1-66.5%) | **+0.417** (+0.117 - +0.718) | 2.14 |
| `sweep>=3` alone | 236 | 39.0% (33.0-45.3%) | +0.020 (-0.160 - +0.200) | 1.03 |

Both requirements of the original goal (win > 50%, RR 2.5 net) hold pooled, and
the average-R interval excludes zero.  **Then the period split destroys it:**

| rule | period | n | win | avg R | PF |
|---|---|---:|---:|---:|---:|
| `sweep>=3 & stop>2ATR & grade>=4` | 1h, before 2026-07-24 | 15 | **20.0%** | **-0.400** | 0.40 |
| same | 1h, after 2026-07-24 | 16 | 75.0% | +0.748 | 4.75 |
| same | 15m, Oct-Nov 2026 | 25 | 68.0% | +0.651 | 3.44 |
| `pen>=0.3 & stop>1.4ATR & grade>=4` | 1h, before 2026-07-24 | 23 | **30.4%** | **-0.254** | 0.58 |
| same | 1h, after 2026-07-24 | 16 | 62.5% | +0.793 | 3.76 |
| same | 15m, Oct-Nov 2026 | 33 | 69.7% | +0.703 | 4.00 |

Everything positive sits in the window after 2026-07-24; before it the same
rules win 20-30% and lose 0.25-0.40R.  Three more reasons not to call it an edge:

- the 44 bear / 12 bull split of the winners means the sample is a single
  downtrend, not a balanced book;
- **BTC - the instrument the rules were derived from - is the worst symbol**
  in the pool (-0.26R / -0.06R over 9 trades each);
- the 15m window is effectively **one month**, not three: `senkou_span_b` needs
  52 bars and the cloud is shifted 26 forward, so a 1D frame resampled from 15m
  is blank for the first ~78 days (first trade 2026-10-09 on a file starting
  2026-07-24).

The honest reading: these gates describe **trend continuation**, so they print
money in a trend and bleed in a range.  That is a regime bet, not an edge - and
it is exactly why a `regime_trend_gate` exists in the engine.  Testing
`regime_trend_gate="aligned"` on this cross-symbol pool is the next experiment,
not a conclusion.

### 5.5 Adding the trend gate: the answer is "no, and it hurts"

`regime_trend_gate="aligned"` requires the 20-bar EMA slope (measured in ATR) to
agree with the trade direction.  Applied to the cross-symbol pool
(`scripts/cross_symbol_validate.py`, `VARIANTS`):

| variant | period | n | win | avg R | PF |
|---|---|---:|---:|---:|---:|
| frozen setup | all | 303 | 38.0% | -0.006 | 0.99 |
| frozen setup | before 2026-07-24 | 86 | 31.4% | -0.083 | 0.88 |
| frozen setup | from 2026-07-24 | 217 | 40.6% | +0.024 | 1.04 |
| **frozen + trend gate** | **all** | **62** | **40.3%** | **+0.165** | **1.32** |
| frozen + trend gate | before 2026-07-24 | 24 | 37.5% | **+0.128** | 1.23 |
| frozen + trend gate | from 2026-07-24 | 38 | 42.1% | +0.189 | 1.38 |
| R2 gates (sweep>=3 & stop>2ATR & grade>=4) | all | 56 | 57.1% | +0.397 | 2.12 |
| **R2 gates + trend gate** | **all** | **16** | **25.0%** | **-0.335** | **0.43** |
| R2 gates + trend gate | before 2026-07-24 | 7 | 14.3% | -0.459 | 0.26 |
| R2 gates + trend gate | from 2026-07-24 | 9 | 33.3% | -0.238 | 0.57 |

Two conclusions, both negative for the original goal:

1. **The trend gate does not rescue the 57% rule.**  It cuts the population from
   56 to 16 trades and the expectancy from +0.397R to -0.335R.  The 57% was not
"trend continuation that needed a trend filter" - it was a window.
2. **The trend gate does help the raw setup**, and it is the only variant in this
   entire study that is positive in *both* periods: -0.083R -> +0.128R before the
   split, +0.024R -> +0.189R after, PF 0.99 -> 1.32.  But 40.3% win with an
   average-R interval of -0.18 to +0.51 is a candidate, not an edge - and 40% win
   is below the >50% the original goal asked for.

Net: **"win > 50% at RR >= 2R net, long and short, in a way that survives a
period split" was not achieved.**  The best surviving configuration wins 40.3% at
2.5R for +0.165R per trade on 62 cross-symbol trades, which is `review`-grade
evidence at best.

### 5.6 The exit side (the half that was never tested)

`scripts/exit_study.py` replays the *same* signals under different exit rules, so
any difference is the exit only.  9 symbols, 1h, costs as above.

**Diagnosis, baseline (RR 2.5, time stop 48 bars):**

| metric | value |
|---|---|
| MFE offered / profit kept | 226.0R / 102.7R -> **46% capture** |
| mean MFE per trade | 1.34R (median 0.94R) |
| trades that ever reached 1R / 1.5R / 2R / 2.5R / 3R | 58% / 41% / 29% / 24% / 20% |
| losers that were once >=1R / >=1.5R / >=2R up | 27 / 13 / 8 of 111 |
| exits by reason | sl 97, tp 35, **time 37 (22%)** |

Half of every favourable excursion is given back, and 22% of the book is closed
by the clock rather than by a decision.

**Exit sweep** (`frozen + trend_aligned`, which is the surviving entry variant):

| exit rule | n | win | avg R | PF |
|---|---:|---:|---:|---:|
| time stop 48 (baseline) | 38 | 39.5% | +0.151 | 1.28 |
| time stop 96 | 37 | 46.0% | +0.271 | 1.50 |
| time stop 200 | 37 | 46.0% | +0.414 | 1.72 |
| **time stop 500** | 37 | 46.0% | **+0.465** | **1.81** |
| breakeven at 1R | 38 | 28.9% | +0.048 | 1.11 |
| ATR trail from 1R (1 ATR) | 39 | 51.3% | +0.159 | 1.37 |
| ATR trail from 1R (1 ATR) + stop 500 | 38 | **60.5%** | +0.288 | 1.73 |

1. **The time stop was the biggest leak in the engine.**  Raising it from 48 to
   500 bars improves every metric monotonically, and it improves **both** halves
   of the period split (+0.128R -> +0.253R before 2026-07-24, +0.189R -> +0.856R
   after) - the only change in this whole study that is not a window artefact.
   `max_bars=40` (the default) is worse still.  A 40-bar cap means 10 hours on
   15m and 40 hours on 1h; the 1h book needs days, not hours.
2. **Moving the stop to break-even at 1R destroys the book** (39.5% -> 28.9% win,
   +0.151R -> +0.048R): it converts the normal stop-out-then-continue path into
   a flat exit.  Break-even is only defensible *after* a partial is banked.

**Partial scale-out** - the mechanism the engine did not have (now
`t1_at_r` / `t1_fraction` / `t1_move_be`, `r_multiple` is the size-weighted
blend of both legs):

| entry | RR | exit | n | win | avg R | PF | realised RR |
|---|---:|---|---:|---:|---:|---:|---:|
| trend_aligned | 2.5 | hold 500 | 37 | 46.0% | +0.465 | 1.81 | **2.13** |
| trend_aligned | 2.5 | half at 1R, BE, hold 500 | 37 | **64.9%** | +0.218 | 1.59 | 0.87 |
| trend_aligned | 2.5 | half at 1.5R, BE, hold 500 | 37 | **54.0%** | +0.330 | 1.68 | 1.43 |
| trend_aligned | 3.0 | hold 500 | 37 | 46.0% | **+0.614** | **2.07** | **2.44** |
| trend_aligned | 3.0 | half at 2R, BE, hold 500 | 37 | 46.0% | +0.418 | 1.73 | 2.03 |

The partial does exactly what it was designed to do - it lifts the win rate from
46% to 54-65% - **but realised RR falls with it**, and the RR>2 point sits at
46% win.  The two targets still do not meet, and now the reason is explicit:
**win rate > 50% requires banking a level that more than half the trades reach,
and only ~41% of these trades ever reach 1.5R.**  With entries this shallow, a
2R average winner and a >50% win rate cannot coexist - no exit rule can create
excursion that the entries never produce.

The target sweep confirms the ceiling: at RR 3.0 with a 500-bar hold the book
earns +0.614R/trade at 46% win (PF 2.07).  Raising the target further (3.5R)
starts to cost (+0.411R), and lowering it to 1R buys a 63% win rate but only
+0.191R.  The optimum for expectancy is RR 3.0, not the 2.5 the task assumed.

## 6. What was changed in the code

- `SetupConfig.min_sweep_strength`, `SetupConfig.min_stop_atr` (new gates, off by
  default), `SetupConfig.commission_bps` / `slippage_bps` (were hard-coded in
  `run_one` and unreachable from the CLI).
- `quality_profile="btc_15m"`: displacement >= 1.45 ATR + stop >= 1.4 ATR + 2.5R.
  Documented as a **study profile with no proven edge**; the docstring carries
  the train/holdout/full-sample numbers so nobody can quote the 50% without the
  six-trade footnote.
- CLI: `--allow-short` (the engine had **no way to enable shorts** from the CLI
  at all), `--min-sweep-strength`, `--min-stop-atr`, `--commission-bps`,
  `--slippage-bps` now reach `StrategyConfig`.
- Performance: `build_setup` 12.8 s -> **0.65 s** on 20k bars (a precomputed
  struct-of-arrays pass replaces the per-CISD swing/FVG/HTF scans).  Verified
  bit-identical: the same 8 configurations return the same 18/41/23/4/41/17/18/104
  signals with identical levels, stops, targets and quality scores.
- `src/ict_trader/stats.py`: `wilson_interval`, `mean_interval`, `n_for_margin`,
  `n_to_detect`; `BacktestResult.metrics` now carries `win_rate_ci95`,
  `avg_r_ci95` and `n_for_5pct_win_rate`, so a win rate can never be quoted
  without its interval again.
- **Partial scale-out** (`StrategyConfig.t1_at_r`, `t1_fraction`, `t1_move_be`;
  `SetupConfig` and the CLI expose the same three).  It is the mechanism the
  engine lacked, `r_multiple` is now the size-weighted blend of both legs with
  per-leg costs, `Trade.t1_filled` / `Trade.t1_price` are journaled, and
  `reason="t1_be"` marks a runner scratched at break-even.  Off by default, so
  every earlier number in this document still reproduces.
- `scripts/exit_study.py`: replays fixed signals under 17 exit rules and reports
  MFE capture, `pct_reach_{1,1.5,2,2.5,3}R`, realised RR and the exit-reason
  mix (`--rr-sweep` adds the target curve).

## 7. Reproduction

```bash
# pool + simulations, then the train/holdout rule search
python scripts/optimize_btc.py --train-bars 45000 --test-bars 20000
python scripts/rule_search.py --min-train-n 25

# nine symbols the rules never saw (Gate.io futures; Binance is unreachable here)
python scripts/fetch_gate.py --symbols BTC ETH SOL BNB XRP DOGE ADA AVAX LINK \
    --timeframes 1h 15m --out-dir output/ict/gate
python scripts/cross_symbol_validate.py --timeframes 1h 15m \
    --json-out output/ict/cross_symbol_all.json

# exit study: MFE capture, exit rules, target curve
python scripts/exit_study.py --timeframes 1h --rr-sweep \
    --json-out output/ict/exit_study_1h.json

# the study profile through the CLI (this is the honest headline number)
python -m ict_trader backtest \
  --csv output/ict/btcusdt_15m_full.csv --daily-csv output/ict/btcusdt_1d_full.csv \
  --market crypto --allow-short --sell-tax-bps 0 --commission-bps 10 --slippage-bps 2 \
  --min-1d-score 2 --min-15m-score 1 --max-bars 48 --quality-profile btc_15m \
  --out output/ict/btc_profile_trades.csv --metrics output/ict/btc_profile_metrics.json
```

## 8. Recommendation

1. **Do not quote a >50% win rate for this engine on BTC 15m.**  The measured
   base rate at 2.5R net is 28-35%; the honest expectation is negative.
2. **Widen the stop before anything else** (>=1.4 ATR, better >=2 ATR) or move to
   the 1h base: fee-in-R halves or quarters.
3. If 50%+ win rate is a hard requirement, then **RR has to come down**, or the
   edge has to come from somewhere the engine does not model (funding, order
   flow, cross-asset SMT with real correlated symbols).  The corpus rule that
   matters here is the one about sample size: 23 trades is `observation`.
4. Any future "improvement" must be validated on the full 22-month sample **and**
   on a symbol it was not tuned on.  Both tests killed everything above.
