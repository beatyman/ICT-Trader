# Data notes

## Duplicate A-share sample

`examples/15m.csv` and `output/ict/sz000858_15m_2025_2026.csv` are the same 7,024-bar
series (same dates, open, close).  Do not count both as independent datasets.

The 8-case basket in the early studies therefore double-counted this sample.  Deduplicate
before quoting a trade count or a weighted average.

## Short crypto slices

`btc_12k.csv`, `eth_12k.csv`, etc. are recent 12k-bar slices, not independent histories.
They are useful for smoke tests but not for out-of-sample claims.  The full files
(`*_15m_2025_2026.csv`) are the better evidence.

## Missing daily file

`sh600519` has no matching daily CSV in this workspace; the run used the resampled 1D
fallback.  That is acceptable for a 1D+1H+15m smoke test but should be replaced with a
real daily series before comparing it with the other symbols.

## Full-sample result

Full BTC FVG-only: 118 trades, 35.6% win rate, -0.227R average.  The full sample
overrides the positive recent-slice result; do not cherry-pick the 12k slice.
