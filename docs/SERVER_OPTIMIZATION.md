# Server optimization runner

`scripts/optimize_server.py` runs a batch of ICT experiments on a server.  It is a
**runner**, not an automatic optimizer: it does not choose parameters for you, and it
does not change the production default.

## What it does

- reads an explicit dataset manifest (or discovers `*train*` / `*val*` CSVs);
- applies one experiment override at a time;
- refuses the `test` split unless `--allow-test`;
- runs tasks in parallel;
- resumes with `--resume`;
- writes per-task metrics, per-trade CSV, hashes and a run manifest.

## Manifest

```json
{
  "datasets": [
    {
      "name": "btc_train",
      "path": "/data/ict/btc_15m_train_2020_2023.csv",
      "split": "train",
      "symbol": "BTC",
      "market": "crypto",
      "daily": null
    },
    {
      "name": "btc_val",
      "path": "/data/ict/btc_15m_val_2024.csv",
      "split": "val",
      "symbol": "BTC",
      "market": "crypto",
      "daily": null
    }
  ]
}
```

Relative paths are resolved from the manifest's directory.

## Experiments

```json
{
  "baseline": {},
  "entry_ce": {"entry_mode": "fvg_ce"},
  "fresh_fvg": {"require_fresh_fvg": true},
  "rr_2_5": {"rr": 2.5, "min_target_rr": 2.5}
}
```

Built-ins are used if `--experiments` is omitted:

```text
baseline
entry_ce
fresh_fvg
rr_2_5
dol
silver_bullet
```

`daily_bias_filter=c2/c3` is intentionally absent until E1 is implemented.  Do not
add it to the runner before the pre-registered gate exists.

## Run

```bash
cd /path/to/ICT-Trader
python scripts/optimize_server.py \
  --manifest configs/server_manifest.json \
  --experiments configs/experiments.json \
  --out output/ict/server_run \
  --workers 16
```

Useful flags:

```text
--resume              skip completed task keys
--dry-run             print tasks and params, do not run
--allow-test          explicitly include split=test
--limit-datasets N    first N datasets
--max-tasks N         first N tasks
```

## Output

```text
out/
  run_manifest.json    host, commit, args, hashes, experiments
  results.csv          one row per task
  summary.json         weighted aggregates by experiment / split
  completed.json       resume cache
  errors.log           traceback per failed task
  trades/
    <experiment>__<dataset>__<split>.csv
```

`summary.json` includes:

```text
trades, win_rate, avg_r, total_r, max_dd
right_tail_share     top 10% R / total R
max_month_share      largest month R / total R
```

## Recommended server workflow

1. Freeze the config and dataset manifest.
2. Run `baseline` only; verify hashes and sample counts.
3. Run one experiment at a time; do not put E1 and E2 in the same batch.
4. Check `right_tail_share` and `max_month_share` before looking at avg R.
5. Only after train/validation gates, run `--allow-test` once.

## Resource notes

- Each worker loads its own data; memory scales with `workers * dataset size`.
- For 15m crypto history, 8–16 workers is usually enough.
- The FVG/structure scans were vectorised, but very long histories still take seconds
  per task; use `--resume`.
- Do not run multiple batches that write to the same `--out` directory concurrently.

## Anti-overfitting rules

```text
one variable per batch
no threshold tuning on validation
no test split until the final gate
no promotion without 100+ trades or 50+ plus a second market
no promotion if max_month_share > 0.50
no promotion if right_tail_share > 0.60 and a filter is what changed
```
