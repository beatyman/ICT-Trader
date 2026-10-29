# ICT signal chart

`scripts/plot_ict_signal.py` draws a real-bar ICT chart with Plotly.  It reads a
CSV and uses the repository's detectors; it never invents an FVG, OB, sweep or
CISD.

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
