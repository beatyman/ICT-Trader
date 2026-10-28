---
name: ict-analysis
description: Analyze OHLCV data with the ICT Trader engine for key levels, liquidity sweeps, FVG/IFVG, order blocks, CISD, multi-timeframe alignment and a risk-defined trade card. Use when the user asks for ICT/SMC analysis, a trade plan, setup quality review, or backtest/walk-forward interpretation. Do not use for live order placement.
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

- "Dose this chart have a valid ICT setup?"
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
   position-size method.
8. **Not financial advice.** State the research/education nature when relevant.

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

### 4. If `decision == "TRADE"`

Verify:

```text
direction
level
stop
target
R:R >= 1.5
setup.quality
setup.grade
sweep within 15 bars
1D/4H scores
```

Then produce a trade card.

### 5. Backtest or walk-forward questions

For A-shares always pass `--t-plus-1`.

```bash
ict-trader backtest --csv data/15m.csv --daily-csv data/daily.csv \
  --market cn --t-plus-1 --out output/trades.csv --metrics output/metrics.json

ict-trader walk-forward --trades output/trades.csv --folds 5 --embargo-days 5
```

Report sample size, win rate, average R, PF, max drawdown and fold stability.
Never present a 4-trade sample as proof.

## Output contract

Return one JSON object followed by a short explanation.

```json
{
  "symbol": "SZ000858",
  "as_of": "2026-10-28T15:00:00",
  "decision": "NO_TRADE",
  "reason": "1D score 2/3, close below cloud top",
  "key_levels": {
    "above": [{"name": "BSL", "price": 73.39}],
    "below": [{"name": "SSL", "price": 70.6}]
  },
  "recent_sweeps": [],
  "setup": null
}
```

For a trade:

```json
{
  "decision": "TRADE",
  "setup": {
    "direction": "bull",
    "entry_reference": 71.21,
    "stop": 70.6,
    "target": 76.0,
    "r_multiple": 2.0,
    "grade": 4,
    "quality": 0.71,
    "checks": {"sweep": true, "pda": true, "volume": true}
  },
  "risk": {
    "risk_pct": 1.0,
    "position_size": "risk_amount / (entry - stop)"
  }
}
```

If a field cannot be computed, use `null` and explain why.

## Decision boundaries

| Situation | Action |
|---|---|
| 1D < 3/3 | NO_TRADE |
| 4H < 2/3 | NO_TRADE |
| no sweep in 15 bars | NO_TRADE by default |
| no PDA | NO_TRADE by default |
| quality < 0.55 | NO_TRADE by default |
| price inside 1D cloud | NO_TRADE for trend setup |
| data ends before a closed bar | NO_TRADE / ask for data |

## Style

- Use English technical terms: BSL, SSL, FVG, IFVG, OB, CISD, MSS, BOS.
- Keep the explanation short and evidence-based.
- Separate **fact** (engine output) from **interpretation**.
- State uncertainty explicitly.

## Supporting files

- `references/ict_playbook.md` — definitions and setup logic
- `schemas/trade_plan.schema.json` — output schema
- `prompts/system.md` — role and tone
- `scripts/analyze.py` — thin CLI wrapper
- `../../examples/no_trade.json` / `trade_card.example.json` — output examples
