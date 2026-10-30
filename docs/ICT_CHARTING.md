# ICT signal chart

The fixed analysis standard is:

```text
scripts/plot_ict_plotly.py   30-day 15m Plotly dual-panel
```

It draws the last 30 calendar days of 15m bars, a 75/25 price/volume split,
shared time axis, real structures only, and a labelled plan.

`scripts/plot_ict_clean.py` (static PNG) and `scripts/plot_ict_signal.py`
(legacy Plotly) are secondary and must not be used for the standard analysis
chart.  See [ICT_CHART_STANDARD.md](ICT_CHART_STANDARD.md) for the full contract.

## Standard command

```bash
ict-trader plan \
  --csv data/15m.csv \
  --daily-csv data/daily.csv \
  --market us \
  --symbol US.MSFT \
  --stack 1d_1h_15m \
  --json-out output/ict/msft_plan.json

python scripts/plot_ict_plotly.py \
  --csv data/15m.csv \
  --symbol US.MSFT \
  --timeframe 15m \
  --days 30 \
  --market us \
  --plan-json output/ict/msft_plan.json \
  --out output/ict/us_msft.html
```

## Explicit plan

```bash
python scripts/plot_ict_plotly.py \
  --csv data/15m.csv \
  --symbol US.MSFT \
  --days 30 \
  --market us \
  --direction long \
  --entry 522.50 \
  --sl 518.80 \
  --tp 534.57 \
  --trigger-time "2026-10-29 10:45:00" \
  --out output/ict/us_msft.html
```

## Plan rules

Priority:

```text
--plan-json (only decision=TRADE)
--direction/--entry/--sl/--tp/--trigger-time
AUTO CONDITIONAL from real structure
```

An auto plan is always labelled `AUTO CONDITIONAL`; it is not a confirmed
trade.  The source CISD is marked `(stale)` when older than 20 bars.

## Window rules

```text
15m
last 30 calendar days
all real bars in that window
no padding, no synthetic bars
```

For US/HK cash equities the window usually contains about 500–550 bars, not
2880, because only regular trading hours exist.

## Visual rules

```text
white background by default (--theme dark optional)
row 1: candles + structures + plan (75%)
row 2: volume (25%)
shared x-axis
rangeslider off
weekends/overnight hidden for cash markets
FVG/OB rectangles: no border, low opacity
sweep: arrow annotation
CISD: one vertical dashed line
```

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
