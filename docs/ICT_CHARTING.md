# ICT signal chart

There are two approved renderers:

```text
scripts/plot_ict_clean.py    static PNG, clean light layout (preferred)
scripts/plot_ict_signal.py   interactive Plotly HTML
```

Both use the repository's detectors and never invent an FVG, OB, sweep or CISD.

## Clean PNG

```bash
python scripts/plot_ict_clean.py \
  --csv output/ict/hk09988_15m_2025_2026.csv \
  --symbol HK.09988 \
  --last-bars 96 \
  --out output/ict/hk09988_plan.png
```

It draws K-lines, volume, key levels, active FVG/OB, sweeps, and a plan.

A plan is drawn by default.  It can come from:

```text
--plan-json ict-trader plan JSON (only decision=TRADE)
--direction/--entry/--sl/--tp/--trigger-time
otherwise AUTO CONDITIONAL from last CISD / FVG / key level
```

Example with an engine plan:

```bash
python scripts/plot_ict_clean.py \
  --csv us_googl_15m_closed.csv \
  --symbol US.GOOGL \
  --plan-json output/ict/googl_plan_latest.json \
  --out output/ict/us_googl_clean.png
```

## Interactive HTML

`scripts/plot_ict_signal.py` uses Plotly and supports the same plan flags.

## Plan is drawn by default

The chart renders a **conditional trade plan** unless explicitly disabled:

```text
default: --plan auto
```

`auto` chooses, in order:

```text
1. the last CISD within 40 bars
2. otherwise the nearest active bullish/bearish FVG or OB
3. otherwise the nearest key level in the 15m trend direction
```

The plan is always labelled:

```text
AUTO CONDITIONAL
```

It is **not** a confirmed entry.  If the source is a CISD older than 20 bars,
the label includes `(stale)`.

To draw structures only:

```bash
python scripts/plot_ict_signal.py ... --plan none
```

An explicit plan always overrides `--plan auto`:

```bash
python scripts/plot_ict_signal.py ... \
  --direction long --entry 96.15 --sl 95.25 --tp 99.30 \
  --trigger-time "2026-10-29 16:00:00"
```

## Install

```bash
python -m pip install pandas plotly
```

Run from the repository root, or install the package with `pip install -e .`.

## Structure-only chart

```bash
python scripts/plot_ict_signal.py \
  --csv output/ict/hk03690_15m_2025_2026.csv \
  --symbol HK.03690 \
  --last-bars 300 \
  --hide-weekends \
  --plan none \
  --out output/ict/hk03690_ict.html
```

If no trade plan is supplied, the chart shows:

```text
NO TRADE watermark
key levels
active FVGs + CE
active order blocks
recent sweeps
recent CISD
structure events
```

## Chart with a confirmed plan

```bash
python scripts/plot_ict_signal.py \
  --csv data/15m.csv \
  --symbol BTCUSDT \
  --direction long \
  --entry 79933.7 \
  --sl 79666.9 \
  --tp 81344 \
  --trigger-time "2026-10-29 03:00:00" \
  --commission-bps 5 \
  --slippage-bps 2 \
  --sell-tax-bps 0 \
  --out output/ict/btc_plan.html
```

The script validates:

```text
long:  SL < Entry < TP
short: TP < Entry < SL
trigger time exists in the CSV
```

It computes:

```text
gross RR = reward / risk
net RR   = (reward - entry/exit costs) / (risk + entry/exit costs)
```

The chart draws:

```text
Entry line
SL line
TP line
red risk rectangle
green reward rectangle
RR / Net RR annotation
```

## Data contract

Required columns:

```text
date, open, high, low, close
```

`volume` is optional.  `date` may be `YYYY-MM-DD HH:MM:SS` or ISO timestamps.
Use the same `--timeframe` label as the CSV (default `15m`).

## No-lookahead rule

The detector uses only the loaded bars up to the last row.  The optional trade
plan is drawn on the right side of the trigger bar only; it does not feed future
prices back into the signal logic.

## Files

```text
scripts/plot_ict_signal.py
```

The output HTML is self-contained, so it can be opened without a Plotly CDN.

## One renderer only

`scripts/plot_ict_signal.py` is the approved renderer.  Do not hand-write a
different chart in an answer: that produces inconsistent styles and cannot be
reproduced.  If the style needs to change, change this script first.

