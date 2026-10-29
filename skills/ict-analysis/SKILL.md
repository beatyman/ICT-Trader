---
name: ict-analysis
description: Analyze OHLCV data with the ICT Trader engine for key levels, liquidity sweeps, FVG/IFVG, order blocks, CISD, multi-timeframe alignment and a risk-defined trade card. Use when the user asks for ICT/SMC analysis, a trade plan, setup quality review, or backtest/walk-forward interpretation. Do not use for live order placement or screenshot-only inference.
license: MIT
---

# ICT Analysis Skill

## What this skill does

Turns raw OHLCV into one auditable answer:

```text
key levels → sweeps → FVG/IFVG/OB → CISD → 1D/4H alignment → trade card
```

The engine is in this repository. The model **must not** invent prices,
liquidity levels, sweeps, or signals.

## When to use

Use for:

- "Does this chart have a valid ICT setup?"
- "Where are the key levels / liquidity pools?"
- "Did price sweep liquidity?"
- "Give me a trade plan with entry, stop, target and size."
- "Review this backtest / walk-forward result."
- "Is this CISD / FVG / IFVG / OB valid?"

Do not use for:

- live order execution;
- predicting news;
- guaranteed win-rate claims;
- analyzing a screenshot without numeric OHLCV.

## Non-negotiable rules

1. **Run the engine.** Every numeric level must come from
   `ict-trader plan`, `ict-trader analyze` or a Python call into
   `ict_trader`.
2. **Closed candles only.** A signal exists on the confirmation bar's close.
   Never use a forming bar.
3. **No future levels.** Key levels expose `known_at`; a level cannot be used
   before that bar.
4. **`NO_TRADE` is a valid answer.** If 1D/4H/sweep/quality gates fail, say so.
5. **No screenshot inference.** If the user provides only an image, ask for the
   OHLCV CSV or a data source.
6. **No probability claims.** Do not say "80% chance" unless a validated model
   produced a calibrated probability.
7. **Risk first.** Every trade card must include stop, target, R:R and a
   position-size method. Default caps: 1% equity per trade, 3% daily loss,
   6% rolling weekly loss, and a 10% drawdown pause. Treat these as hard
   process gates, not suggestions.
8. **Not financial advice.** State the research/education nature when relevant.
9. **Record provenance.** Report source, `as_of`, timezone, bar count and whether
   the data is verified or synthetic. A forming bar is not closed.
10. **Classify evidence.** Tag every material statement as `observed`, `derived`,
    `interpretive` or `missing`. Do not promote an interpretation to a fact.
11. **Use one state.** `confirmed`, `developing`, `invalidated` or `no-trade`.
    `confirmed` requires closed-bar evidence and **net** RR >= 2; a high quality
    score alone is not confirmation.
12. **One primary PD array.** Pick the entry array the model can actually test
    (CISD, FVG, OB, breaker or rejection). Other arrays are confluence, not extra
    requirements. Stacking every array produces fewer, not better, trades.
12. **Respect sample size.** Never optimize or claim an edge below 100 trades.
    Report `sample_class` from the backtest and separate process errors from P&L.

## Required inputs

Minimum:

```text
date, open, high, low, close, volume
```

Recommended:

- 15m or 5m execution timeframe: at least 500 bars;
- 1D data: at least 120 bars (for Ichimoku warm-up);
- timestamps in one timezone, monotonic, no duplicate bars.

For crypto, pass `--market crypto` so daily bars use UTC 00:00 close semantics.

## Data and evidence contract

Read [`references/evidence_contract.md`](references/evidence_contract.md) before
issuing a trade card. Minimum provenance:

```text
source, as_of (last closed bar), timezone, bars, data_verified, synthetic
```

Rules:

- If the last bar is still forming, drop it or ask for a closed-bar dataset.
- If the CSV is a generated demo, set `synthetic: true`; never call it live evidence.
- A screenshot is not numeric OHLCV. Refuse to infer levels from it.
- Separate support, conflict and missing evidence. A conflict is not resolved by
  picking the side you prefer.
- `confirmed` is reserved for closed-bar evidence + net RR >= 2. Otherwise use
  `developing`, `invalidated` or `no-trade`.

## Workflow

### 1. Validate data

Check:

```text
columns exist
dates parse
no duplicate timestamps
high >= max(open, close)
low <= min(open, close)
volume >= 0
last bar is closed
```

If validation fails, stop and report the exact issue.

### 2. Ask for the trade card

```bash
ict-trader plan \
  --csv data/15m.csv \
  --daily-csv data/daily.csv \
  --market cn \
  --json-out output/trade_plan.json
```

Read the JSON. Do not re-derive levels from the chart.

Validate the card before presenting it:

```bash
python skills/ict-analysis/scripts/validate_plan.py output/trade_plan.json
```

The validator rejects a `TRADE` that is missing stop/target/R multiple, has
`rejected != null`, is already more than `0.5` ATR from entry, or has
`net_rr < 2.0`.

For a production-style review, use the strict gate:

```bash
ict-trader plan \
  --csv data/15m.csv \
  --daily-csv data/daily.csv \
  --market hk \
  --stack 1d_1h_15m \
  --target-mode dol \
  --session-gate hk \
  --strict-audit \
  --json-out output/trade_plan.json
```

`--strict-audit` requires a Kill Zone, DOL target, sweep origin and net RR.

If `decision == "NO_TRADE"` and `rejected` is set, report that reason verbatim
(for example `price_too_far_from_entry`); do not reinterpret it as a trade.

### 3. If `decision == "NO_TRADE"`

Run:

```bash
ict-trader analyze --csv data/15m.csv --bars 260 \
  --out output/chart.png --signals output/signals.csv \
  --json-out output/analysis.json
```

Then report:

```text
Reason: which gate failed (1D score / 4H score / sweep / quality)
Nearest key levels: above and below
Recent sweeps
What would change the decision
```

Do not turn a `NO_TRADE` into a discretionary buy.

### 3b. Charting contract

When the user asks for a chart, use:

```bash
python scripts/plot_ict_signal.py \
  --csv data/15m.csv \
  --symbol SYMBOL \
  --last-bars 300 \
  --out output/ict/SYMBOL_chart.html
```

**A trade plan is drawn by default** (`--plan auto`).  It must be labelled
`AUTO CONDITIONAL` and is derived from real structure, in this order:

```text
last CISD within 40 bars
else nearest active FVG/OB
else nearest key level in the 15m trend direction
```

Rules:

- never call an auto plan confirmed;
- if the source CISD is older than 20 bars, mark it `(stale)`;
- if the user supplies `direction/entry/sl/tp/trigger_time`, that explicit plan
  overrides auto;
- `--plan none` is allowed only for a structure-only chart;
- every Entry/SL/TP must come from the CSV or a real detected level;
- show gross RR and net RR, including commission/slippage/sell tax.

### 4. If `decision == "TRADE"`

Verify:

```text
direction
level
stop
target
gross RR >= 1.5 (plan.r_multiple is gross)
net RR >= 2.0 after spread, commission, tax and slippage before calling confirmed
entry_distance_atr <= 0.5
setup.quality
setup.grade
sweep within 15 bars (and reclaim_bars <= 3 by default)
1D/4H scores
clear Draw on Liquidity (nearest unswept opposite pool, not an invented target)
optional quality_parts: pd / ote / pda_arrays / inducement_candidates
```

If the plan does not expose a verified `net_rr`, compute it from the planned entry,
stop, target and the user's cost model. If any input is missing, leave the state
`developing` and say what is missing; do not call it `confirmed`.

Then produce a trade card. If the plan is stale, say `no-trade`, not “wait for a
better price” unless the user asked for a conditional plan.

### 5. Backtest or walk-forward questions

For A-shares always pass `--t-plus-1`.

```bash
ict-trader backtest --csv data/15m.csv --daily-csv data/daily.csv \
  --market cn --t-plus-1 --out output/trades.csv --metrics output/metrics.json

ict-trader walk-forward --trades output/trades.csv --folds 5 --embargo-days 5

ict-trader journal --trades output/trades.csv --out output/journal.csv \
  --summary output/journal_summary.json
```

Report sample size, win rate, average R, PF, max drawdown and fold stability.
Also report `process_errors` from the journal when discussing execution, not just P&L.
Never present a 4-trade sample as proof.

### 6. Strategy optimization requests

When the user asks to improve setup quality or RR:

1. Diagnose which gates currently let low-quality or low-RR trades through.
2. Propose 1–3 stricter filters, not looser ones:
   - higher quality/grade/confluence;
   - require sweep + reclaim;
   - require PDA (FVG/OB) or point-in-time DOL;
   - higher min net RR;
   - displacement and entry-distance gates.
3. Prefer ablation-style changes that can be tested with the engine.
4. Never claim an improved edge below 100 trades.
5. Default recommendation: raise quality/RR thresholds first; do not loosen
   structure or sweep requirements.
6. State whether the binding constraint is bias, DOL, structure, sweep, PDA,
   displacement, entry distance or net RR. "More trades" is not a quality goal.

Do not respond to an optimization request by adding parameters without a
diagnosis. A stricter checklist that produces more `NO_TRADE` is a valid result.


## Output contract

Return one JSON object followed by a short explanation.

```json
{
  "symbol": "SZ000858",
  "as_of": "2026-10-28T15:00:00",
  "source": "Futu OpenD / 15m + 1D",
  "data_verified": true,
  "synthetic": false,
  "state": "no-trade",
  "decision": "NO_TRADE",
  "reason": "1D score 2/3, close below cloud top",
  "rejected": null,
  "entry_distance_atr": null,
  "key_levels": {
    "above": [{"name": "BSL", "price": 73.39}],
    "below": [{"name": "SSL", "price": 70.6}]
  },
  "recent_sweeps": [],
  "setup": null,
  "evidence": {
    "observed": ["last closed bar 2026-10-28 15:00"],
    "derived": ["1D score 2/3"],
    "interpretive": [],
    "missing": ["no sweep in 15 bars"]
  }
}
```

For a trade:

```json
{
  "state": "developing",
  "decision": "TRADE",
  "setup": {
    "direction": "bull",
    "entry_reference": 71.21,
    "stop": 70.6,
    "target": 76.0,
    "r_multiple": 2.0,
    "entry_distance_atr": 0.2,
    "grade": 4,
    "quality": 0.71,
    "checks": {"sweep": true, "pda": true, "volume": true}
  },
  "risk": {
    "risk_pct": 1.0,
    "position_size": "risk_amount / (entry - stop)"
  },
  "evidence": {
    "observed": ["sweep at bar 412", "CISD close at bar 415"],
    "derived": ["gross RR 2.0", "entry distance 0.2 ATR"],
    "interpretive": ["sell-side liquidity taken"],
    "missing": ["net RR after fees unverified"]
  }
}
```

`state` is `confirmed` only when the evidence and net RR gates both pass;
otherwise use `developing` or `no-trade`.

Every `TRADE` card must expose `gross_rr`, `net_rr` and `cost_model`; a missing
`net_rr` is treated as unverified, not as a pass.

If a field cannot be computed, use `null` and explain why.

## Decision boundaries

| Situation | Action |
|---|---|
| 1D < 3/3 | NO_TRADE |
| 4H < 2/3 | NO_TRADE |
| no sweep in 15 bars | NO_TRADE by default |
| no PDA | NO_TRADE by default |
| no clear Draw on Liquidity | NO_TRADE |
| quality < 0.55 | NO_TRADE by default |
| grade below `min_grade` or confluence below `min_confluence` | NO_TRADE |
| net RR < 2.0 | NO_TRADE；不得标为 `confirmed` |
| price inside 1D cloud | NO_TRADE for trend setup |
| data ends before a closed bar | NO_TRADE / ask for data |
| user asks to "optimize strategy" | tighten gates first; do not add trade count |

## Style

- Use English technical terms: BSL, SSL, FVG, IFVG, OB, CISD, MSS, BOS.
- Keep the explanation short and evidence-based.
- Separate **fact** (engine output) from **interpretation**.
- State uncertainty explicitly.

## Supporting files

- `references/ict_playbook.md` — definitions and setup logic
- `references/evidence_contract.md` — provenance, evidence classes, state and sample gates
- `schemas/trade_plan.schema.json` — output schema
- `prompts/system.md` — role and tone
- `scripts/analyze.py` — thin CLI wrapper
- `scripts/validate_plan.py` — strict pre-flight check for a trade card
- `../../examples/no_trade.json` / `trade_card.example.json` — output examples
