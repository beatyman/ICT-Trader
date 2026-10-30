# ICT chart standard

This is the fixed chart contract for ICT analysis in this repository.

## Canonical renderer

```text
scripts/plot_ict_plotly.py
```

No other chart is produced ad hoc.  If the layout must change, change this
script first, then regenerate.

Deprecated / secondary:

```text
scripts/plot_ict_clean.py    static PNG only, not the analysis standard
scripts/plot_ict_signal.py   legacy Plotly renderer
```

## Confluence gate

A chart may show a **conditional** plan, but a trade is only valid when all
applicable checks pass.  Each check must be marked `PASS`, `FAIL` or `N/A`;
`N/A` is never treated as a pass.

```text
1. Liquidity sweep      price took a prior high/low, then reclaimed
2. HTF PDA delivery    1H/4H FVG or OB is being delivered into
3. Volume              sweep/displacement bar >= 1.5x 20-bar average
4. Session sweep       sweep occurs in the active Asia/London/NY session
5. Clear targets       unswept DOL exists; net RR >= 2.5
6. SMT                 correlated asset divergence, only with a second CSV
7. Trigger             15m CISD / displacement confirms the direction
```

Rules:

- do not draw a missing FVG/OB/sweep;
- do not claim SMT without a correlated series;
- do not claim session sweep when the data has no session/premarket bars;
- if any required check is `FAIL`, the decision remains `NO_TRADE`;
- `N/A` blocks a trade unless the user explicitly accepts the missing input.

## Required window

```text
15m
latest 30 calendar days
all real bars available in that window
```

For 24h markets that is about 2880 bars.  For US/HK cash equities it is about
500–550 bars because only RTH exists.  The script never pads or fabricates bars
to reach 2880.

## Required layout

```text
row 1 (75%): candlestick + ICT structures + trade plan
row 2 (25%): volume bars
shared, aligned x-axis
white background by default (--theme dark optional)
rangeslider off
weekends / overnight hidden for cash markets
```

## Required structures

Draw only when present in the data:

```text
PDH / PDL / EQH / EQL
Liquidity sweep
OB / Breaker
FVG + CE
BOS / CHoCH / MSS
CISD
```

Absent structures are omitted, never invented.

## Required plan

```text
Entry / SL / TP
risk zone from trigger to latest bar
reward zone from trigger to latest bar
RR and Net RR box
```

Plan source priority:

```text
--plan-json (only decision=TRADE)
--direction / --entry / --sl / --tp / --trigger-time
AUTO CONDITIONAL from real structure (labelled)
```

## Standard command

```bash
ict-trader plan \
  --csv data/15m.csv \
  --daily-csv data/daily.csv \
  --market hk \
  --symbol HK.09988 \
  --stack 1d_1h_15m \
  --json-out output/ict/plan.json

python scripts/plot_ict_plotly.py \
  --csv data/15m.csv \
  --symbol HK.09988 \
  --timeframe 15m \
  --days 30 \
  --market hk \
  --plan-json output/ict/plan.json \
  --out output/ict/hk09988.html
```

If `decision=NO_TRADE`, the chart draws a labelled `AUTO CONDITIONAL` plan.
It is a chart annotation, not a trade recommendation.

## Self-check

```text
[ ] 30-day 15m window, real bars only
[ ] two rows, shared x-axis, volume below
[ ] rangeslider off
[ ] no border on FVG/OB boxes
[ ] sweep uses arrows, not circles
[ ] CISD vertical line only where detected
[ ] risk/reward zones start at trigger and end at latest bar
[ ] RR and Net RR shown
[ ] no future data used for Entry/SL/TP
[ ] absent structure not drawn
```
