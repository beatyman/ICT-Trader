# ICT chart standard

This is the fixed chart contract for ICT analysis in this repository.

## Canonical renderer

```text
scripts/plot_ict_clean.py
```

It produces the analysis deliverable:

```text
single 16:9 PNG, 300 dpi
last 2-3 days of 15m candles
volume panel
right-side label gutter with leader lines
dual-direction Entry / SL / TP
Risk / Reward zones
layer toggles for FVG / OB / structure / sweep / killzone / EQ
```

Do not produce an ad-hoc chart. Change `plot_ict_clean.py` first, then
regenerate.

Secondary:

```text
scripts/plot_ict_plotly.py   interactive HTML, not the analysis deliverable
scripts/plot_ict_signal.py   legacy renderer
```

## Fixed layer order

```text
10 labels
9  plan
8  DOL
7  sweep
6  candles
5  structure
4  liquidity
3  swing markers
2  FVG / OB
1  EQ / premium-discount
0  killzone / true-day background
```

## Fixed window

```text
15m candles
last 2-3 trading days
150-220 bars
```

For US/HK cash markets, do not include overnight synthetic bars. For crypto,
use the real 24h bars.

## Required elements

```text
K-line + volume
FVG / OB (active only)
BSL / SSL
Sweep marker
Swing HH/HL/LH/LL
BOS / MSS
Killzone background when enabled
EQ 50% when enabled
Entry / SL / TP for both directions when defined
Risk / Reward zones
right-gutter labels with leader lines
```

## Plan source

```text
--plan-json only if decision=TRADE
--direction/--entry/--sl/--tp/--trigger-time for explicit plan
```

A conditional plan must pass `--conditional`.

`NO_TRADE` is not a reason to omit TP. If a scenario TP exists, draw it and
label it `scenario`.

## Standard command

```bash
python scripts/plot_ict_clean.py \
  --csv data/15m.csv \
  --symbol SYMBOL \
  --timeframe 15m \
  --start "YYYY-MM-DD HH:MM" \
  --end "YYYY-MM-DD HH:MM" \
  --last-bars 220 \
  --direction short \
  --entry 0 --sl 0 --tp 0 \
  --trigger-time "YYYY-MM-DD HH:MM" \
  --conditional \
  --alt-direction long \
  --alt-entry 0 --alt-sl 0 --alt-tp 0 \
  --show-fvg --show-ob --show-sweeps --show-structure \
  --show-killzones --show-eq \
  --out output/ict/SYMBOL_analysis.png
```

## Self-check

```text
candles visible
Entry / SL / TP visible
Risk / Reward zones visible
labels in the right gutter
no label overlap
chart values equal text values
NO_TRADE / CONDITIONAL status visible
```
