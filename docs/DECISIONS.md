# Decisions log

Pre-registered experiment outcomes. A variant is promoted only when it passes
the gate in `OPTIMIZATION_PLAN.md` and survives a walk-forward with 100+
out-of-sample trades. A failed experiment is a result.

---

## E1 — Candle 2/3 daily bias filter

**Date:** 2026-10-29
**Hypothesis:** a Candle-2/3 fake-break closure on the daily bias TF filters
the signal direction and improves OOS expectancy.

**Change:**
- `context.candle2_closure()` / `context.candle3_closure()`
- `SetupConfig.daily_bias_filter = none|c2|c3`
- `--daily-bias-filter` CLI flag; `quality_parts.daily_bias`
- `optimize_server` experiments `c2` / `c3`

**Data:** BTC/ETH/SOL 15m, train 2020-2023 + val 2024, base config
(`1d_1h_15m` / FVG-only / OTE 0.00-0.30 / 2R from fill).

**Result:**

| variant | trades | win rate | avg R | total R |
|---|---:|---:|---:|---:|
| baseline | 452 | 38.5% | -0.073 | -30.93 |
| c2       |  99 | 23.9% | -0.370 | -24.88 |
| c3       |   0 |   —    |   —    |   0.00 |

**Pass gate (avg R +0.10, DD not worse >20%, trades >=50%):** FAIL.

- `c2` halves the win rate (38.5% -> 23.9%) and turns avg R from -0.073 to
  -0.370. The daily fake-break closure is anti-predictive on this basket, not
  a bias filter.
- `c3` produces zero signals. A C2-failure-then-engulf (C3 engulfs C2's body
  and closes through it) occurs 0 times in 1460 BTC daily bars — the rule is
  too strict to be a filter at all.

**Decision:** REJECT. `daily_bias_filter` stays `none` (default). The code
ships behind the flag for future data, but it is not wired into the default
and must not be presented as an improvement.

---

## E1b — High-quality / high-RR profile (corpus synthesis)

**Date:** 2026-10-29
**Source:** 383 full-text articles (14 sources), read by source family. The
cross-source consensus on "high-quality setup + high RR" is in
[`HIGH_QUALITY_SETUP.md`](HIGH_QUALITY_SETUP.md).

**Claim tested:** the corpus says the edge is structural RR, not win rate —
enter at CE with a structural stop, target the HTF DOL (3R–7R), and a 30% win
rate at 3R is profitable where 35% at 2R is not.

**What was measured (BTC 15m train 2020–2023, base = 1d_1h_15m / FVG / OTE≤0.30):**

| config | trades | win rate | avg R |
|---|---:|---:|---:|
| baseline (fixed 2R) | 126 | 38.9% | -0.140 |
| + displacement ≥0.65 | 77 | 42.9% | **-0.086** |
| + fixed RR 2.5 | 77 | 41.6% | -0.137 |
| + fixed RR 3.0 | 77 | 39.0% | -0.249 |
| + DOL target 2–6R | 42 | 31.0% | -0.260 |
| + DOL target 2–10R | 48 | 35.4% | -0.203 |
| + grade ≥4 / confluence ≥3 | 0 | — | — |

**Why the corpus lever does not translate:**

1. **The engine's signal ceiling is below the corpus A-grade.** Grade tops out
   at 3 and confluence at 2 on this basket, so `min_grade=4` /
   `min_confluence=3` reject every signal. The corpus A-grade (HTF bias + DOL
   + sweep + CISD + displacement + premium/discount) is a *human* checklist
   the mechanical CISD never reaches.
2. **Raising the fixed RR multiplier makes it worse** (-0.086 → -0.249 at 3R).
   These signals do not reach 3R; forcing a higher fixed target just turns
   2R winners into time-outs/losses. This is exactly the corpus warning
   against "forcing RR by shrinking the stop" — here it is forcing RR by
   stretching the target.
3. **DOL target is worse** than fixed 2R on crypto. The nearest-unswept-pool
   target is not the corpus DOL (EQH→PDH→PWH with HTF direction); it is a
   local pool that the 15m CISD rarely reaches.

**Decision:** KEEP the corpus synthesis as documentation. The `high_rr`
profile is reduced to the one lever that measurably helped — displacement
body/range ≥ 0.65 (-0.140 → -0.086) — and it still does not turn the basket
positive. No corpus rule is promoted to default on this data. The honest
production output remains `NO_TRADE` until the engine's grade/confluence
ceiling is raised or a structurally different entry (per the corpus, a true
HTF DOL) is implemented and survives a walk-forward.

---

## E1c — HTF DOL target (time-anchored liquidity)

**Date:** 2026-10-29
**Hypothesis:** the corpus DOL is not the nearest arbitrary swing pool but a
time-anchored HTF level (EQH→PDH→PWH above, EQL→PDL→PWL below). Targetting
that instead of the local pool should raise RR without the nearest-pool noise.

**Change:** `target_mode="htf_dol"` — prefers `source in {"session","pool"}`
levels (PDH/PWH/EQH, PDL/PWL/EQL) over arbitrary swing BSL/SSL, falls back to
all levels when none anchor. `--target-mode htf_dol` exposed.

**Result (BTC 15m train 2020–2023, base = 1d_1h_15m / FVG / OTE≤0.30):**

| target | trades | win rate | avg R |
|---|---:|---:|---:|
| fixed 2R | 126 | 38.9% | -0.140 |
| dol (nearest pool) | 66 | 25.8% | -0.337 |
| htf_dol (anchored) | 56 | 25.0% | -0.395 |

**Decision:** REJECT. Both DOL variants halve the win rate (38.9% → 25%)
without lifting the average winner: the 15m CISD entry rarely reaches *any*
distant pool, local or time-anchored. The entry is a shallow sweep-reversal
that does not carry to HTF liquidity, so stretching the target only turns 2R
winners into losses. `htf_dol` stays available as a research flag; the default
target remains `fixed`.

---

## E1d — Multiple entry points (FVG CE / OB mean / OTE)

**Date:** 2026-10-29
**Motivation:** the corpus teaches more than one entry (FVG CE, OB mean
threshold, OTE 70.5%, breaker retest), and the engine was hard-wired to
`next_open`. Test each PD-array entry against the baseline.

**Change:** `OrderBlock.mean`; `entry_mode` extended with `ob_me` (OB 50%
mean threshold) and `ote` (70.5% retracement of the signal range); both reuse
the retest-fill path.

**Result (BTC/ETH/SOL 15m, train+val, base = 1d_1h_15m / FVG / OTE≤0.30):**

| entry | trades | win rate | avg R | total R | not triggered |
|---|---:|---:|---:|---:|---:|
| next_open | 452 | 38.5% | -0.073 | -30.93 | 0 |
| fvg_ce | 388 | 35.8% | -0.127 | -50.30 | 69 |
| ob_me | **6** | 61.1% | **+1.058** | +7.98 | 44 |
| ote | 222 | 35.8% | -0.130 | -48.73 | 260 |

**What this says:**

- `ob_me` is the only positive-expectancy entry (+1.058 avg R), confirming the
  corpus claim that the OB is the deepest entry with the tightest stop and the
  highest RR. The tradeoff is real: it fills only **6 times** in 4 years across
  3 symbols (44 signals never retraced to the OB mean and were skipped as
  "miss the move").
- `fvg_ce` and `ote` are *worse* than `next_open` on this basket. The shallow
  FVG CE and the 70.5% OTE are too far from the actual reversal to improve R.

**Decision:** PROMOTE-ONLY-AS-RESEARCH. `ob_me` is directionally correct but
has 6 trades — far below the 100-trade gate. It is exposed as `--entry-mode
ob_me` and recorded here as the strongest entry candidate, but the default
stays `next_open` until a larger multi-symbol history confirms the edge.

---

## E2 — HTF resample boundary bug (fixed)

**Date:** 2026-10-29
**Hypothesis:** the HTF bias (1D/4H/1H) was built by resampling the 15m bars,
but if the resample boundary is wrong the whole HTF read is wrong.

**Finding:** `indicators.resample_htf` and `enhancements.detect_htf_cisds`
used `resample(rule, label="right", closed="right")`.  That shifts every HTF
bar by one 15m bar: the 1D open/close were read from the wrong candle (e.g.
2020-01-02 open = 7195.11 vs the true Binance open 7200.77).  `high`/`low`
looked right only because max/min survive the shift.

**Fix:** `label="left", closed="left"` — aligns to the exchange bar-open
boundary (UTC midnight for 1D, 00:00/04:00/... for 4H).  Verified against
downloaded Binance 1d/4h/1h klines: 1h matches exactly (max diff 0), 1d/4h
match to within one bar.

**Effect (8 symbols, train+val):** baseline `next_open` goes from
**-0.049 avg R** (buggy HTF) to **+0.044 avg R**.  Every earlier negative
experiment (E1b, E1c, E1d) was measured against a wrong HTF read.

**Decision:** KEEP. The fix is the single biggest lever found so far — it turns
the baseline positive without any filter.  All prior experiment conclusions
must be re-read against the corrected HTF.

---

## E3 — Full setup/entry matrix (corrected HTF)

**Date:** 2026-10-29
**Data:** BTC/ETH/SOL/DOGE/XRP/AVAX/LINK/BNB 15m, train+val, base
(`1d_1h_15m` / FVG / OTE≤0.30 / 2R from fill).

**In-sample (avg R, sorted):**

| config | trades | win rate | avg R |
|---|---:|---:|---:|
| ob_me + fixed | 8 | 35% | +0.533 |
| next_open + disp≥0.65 + structure gate | 337 | 47% | +0.094 |
| next_open + structure gate | 543 | 43% | +0.047 |
| next_open (baseline) | 1420 | 43% | +0.044 |
| fvg_ce / retest_key / ote | 260-1241 | 35-41% | all negative |

**Walk-forward (5 folds, purged):**

| config | n | OOS avg R | folds > 0 |
|---|---:|---:|---:|
| next_open (baseline) | 1420 | +0.049 | 4/5 |
| next_open + disp≥0.65 + structure gate | 337 | +0.046 | 3/5 |
| next_open + structure gate | 543 | +0.023 | 4/5 |

**Findings:**

1. The corrected baseline is the most robust result: +0.049 OOS, 4/5 folds
   positive, 1420 trades.  The best entry is `next_open` (signal-bar close →
   next open).  Every "wait for a retrace" entry (fvg_ce / ob_me / ote /
   retest_key) is worse — the CISD is a shallow sweep-reversal and waiting
   for a pullback is waiting for the reversal to fail.
2. Every filter overfits.  `disp≥0.65 + structure gate` lifts in-sample avg R
   from +0.044 to +0.094, but OOS drops to +0.046 with only 3/5 folds
   positive.  The "improvement" is an artifact of fewer, noisier samples.
3. `ob_me` (+0.533) is real but structurally unusable: 8 fills in 4 years
   across 8 symbols, ~122 signals skipped as "miss the move".  It is the
   deepest entry with the tightest stop, but its frequency is a non-strategy.

**Decision:** REJECT all filters, keep the corrected baseline as the only
promoted configuration.  No `min_grade`/`min_confluence`/`min_disp`/DOL/
structure gate survives walk-forward.  The engine's honest edge after fixing
the HTF boundary is a weak +0.049 avg R — positive but not the strong edge
the corpus A-grade promises.
