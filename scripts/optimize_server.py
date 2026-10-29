#!/usr/bin/env python
"""Run reproducible ICT experiment batches on a server.

This is a batch runner, not an auto-tuner.  It:

* reads an explicit dataset manifest (or auto-discovers train/val files),
* applies one experiment override at a time,
* refuses the test split unless --allow-test is given,
* runs tasks in parallel,
* writes one row per task plus per-trade CSV files,
* records config/data hashes and a run manifest,
* supports --resume and --dry-run.

Example
-------
python scripts/optimize_server.py \
  --manifest configs/server_manifest.json \
  --experiments configs/experiments.json \
  --out output/ict/server_run \
  --workers 16

Built-in experiments are used when --experiments is omitted:

  baseline
  entry_ce
  fresh_fvg
  rr_2_5
  dol
  silver_bullet

The script never edits the production default.  Promotion is a separate,
manual decision after the pre-registered gates are checked.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import socket
import sys
import time
import traceback
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

# Make `src` importable when the script is run from the repository root.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ict_trader.setup import SetupConfig, run_one  # noqa: E402


BASE: dict[str, Any] = {
    "market": "crypto",
    "allow_short": False,
    "sweep_reclaim_bars": 3,
    "stack": "1d_1h_15m",
    "rr": 2.0,
    "min_target_rr": 2.0,
    "max_target_rr": 6.0,
    "origin_filter": "fvg",
    "ote_gate": True,
    "ote_min": 0.0,
    "ote_max": 0.30,
    "target_mode": "fixed",
    "entry_mode": "next_open",
    "htf_fvg": False,
    "t_plus_1": False,
    "sell_tax_bps": 0.0,
    "risk_pct": 0.01,
}


BUILTIN_EXPERIMENTS: dict[str, dict[str, Any]] = {
    "baseline": {},
    "entry_ce": {"entry_mode": "fvg_ce"},
    "fresh_fvg": {"require_fresh_fvg": True},
    "rr_2_5": {"rr": 2.5, "min_target_rr": 2.5},
    "dol": {"target_mode": "dol"},
    "silver_bullet": {"session_gate": "silver_bullet"},
}


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def infer_symbol(path: Path) -> str:
    stem = path.stem
    for token in ("_15m", "_1h", "_4h", "_daily", "_day"):
        if token in stem:
            stem = stem.split(token)[0]
            break
    return stem.upper()


def infer_market(symbol: str) -> str:
    s = symbol.upper()
    if any(s.startswith(x) for x in ("BTC", "ETH", "SOL", "FIL", "TUT", "XRP", "DOGE")):
        return "crypto"
    if s.startswith("HK") or s.isdigit() and len(s) == 5:
        return "hk"
    if s.startswith(("SH", "SZ")) or s.isdigit() and len(s) == 6:
        return "cn"
    return "other"


def infer_split(path: Path) -> str:
    name = path.name.lower()
    if "train" in name:
        return "train"
    if "val" in name:
        return "val"
    if "test" in name:
        return "test"
    return "unknown"


def discover(data_dir: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for p in sorted(data_dir.glob("*.csv")):
        split = infer_split(p)
        if split == "unknown":
            continue
        symbol = infer_symbol(p)
        rows.append(
            {
                "name": p.stem,
                "path": str(p),
                "split": split,
                "symbol": symbol,
                "market": infer_market(symbol),
                "daily": None,
            }
        )
    return rows


def load_manifest(path: Path | None, data_dir: Path | None) -> list[dict[str, Any]]:
    if path is not None:
        obj = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(obj, dict) and "datasets" in obj:
            obj = obj["datasets"]
        if not isinstance(obj, list):
            raise SystemExit("manifest must be a list or {'datasets': [...]} ")
        out = []
        base = path.parent
        for i, row in enumerate(obj):
            if "path" not in row:
                raise SystemExit(f"manifest row {i} has no path")
            row = dict(row)
            raw_path = Path(row["path"])
            if not raw_path.is_absolute():
                raw_path = (base / raw_path).resolve()
            row["path"] = str(raw_path)
            row.setdefault("name", raw_path.stem)
            row.setdefault("split", infer_split(raw_path))
            row.setdefault("symbol", infer_symbol(raw_path))
            row.setdefault("market", infer_market(str(row["symbol"])))
            row.setdefault("daily", None)
            out.append(row)
        return out
    if data_dir is None:
        raise SystemExit("provide --manifest or --data-dir")
    return discover(data_dir)


def load_experiments(path: Path | None) -> dict[str, dict[str, Any]]:
    if path is None:
        return dict(BUILTIN_EXPERIMENTS)
    obj = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(obj, dict) or not obj:
        raise SystemExit("experiments file must be a non-empty JSON object")
    out: dict[str, dict[str, Any]] = {}
    for name, overrides in obj.items():
        if not isinstance(overrides, dict):
            raise SystemExit(f"experiment {name!r} must be an object of overrides")
        out[str(name)] = overrides
    return out


def make_params(
    base: dict[str, Any],
    overrides: dict[str, Any],
    dataset: dict[str, Any],
) -> dict[str, Any]:
    params = dict(base)
    params.update(overrides)
    # Dataset context wins unless the experiment explicitly sets it.
    if "market" not in overrides:
        params["market"] = dataset.get("market", params["market"])
    if "t_plus_1" not in overrides:
        params["t_plus_1"] = params["market"] == "cn"
    if "sell_tax_bps" not in overrides:
        params["sell_tax_bps"] = 5.0 if params["market"] == "cn" else 0.0
    return params


def task_key(experiment: str, dataset: dict[str, Any]) -> str:
    return f"{experiment}::{dataset['name']}::{dataset['split']}"


def _json_safe(value: Any) -> Any:
    if isinstance(value, (pd.Timestamp, datetime)):
        return value.isoformat()
    if hasattr(value, "item"):
        try:
            return value.item()
        except Exception:
            pass
    return value


def trade_rows(trades: list, experiment: str, dataset: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for t in trades:
        d = asdict(t)
        d.pop("checks", None)
        d.pop("features", None)
        d = {k: _json_safe(v) for k, v in d.items()}
        d["experiment"] = experiment
        d["dataset"] = dataset["name"]
        d["split"] = dataset["split"]
        d["symbol"] = dataset.get("symbol", "")
        rows.append(d)
    return rows


def run_task(task: dict[str, Any]) -> dict[str, Any]:
    started = time.time()
    dataset = task["dataset"]
    experiment = task["experiment"]
    params = task["params"]
    try:
        cfg = SetupConfig(**params)
        trades, metrics = run_one(dataset["path"], cfg, dataset.get("daily"))
        rows = trade_rows(trades, experiment, dataset)
        return {
            "key": task["key"],
            "status": "ok",
            "experiment": experiment,
            "dataset": dataset["name"],
            "split": dataset["split"],
            "symbol": dataset.get("symbol", ""),
            "market": dataset.get("market", ""),
            "params": params,
            "metrics": metrics,
            "trades": rows,
            "elapsed_s": round(time.time() - started, 2),
            "error": None,
        }
    except Exception as exc:  # noqa: BLE001 - batch runner must continue
        return {
            "key": task["key"],
            "status": "error",
            "experiment": experiment,
            "dataset": dataset["name"],
            "split": dataset["split"],
            "symbol": dataset.get("symbol", ""),
            "market": dataset.get("market", ""),
            "params": params,
            "metrics": None,
            "trades": [],
            "elapsed_s": round(time.time() - started, 2),
            "error": f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}",
        }


def weighted_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    ok = [r for r in rows if r["status"] == "ok"]
    if not ok:
        return {"tasks": 0, "trades": 0}
    trades = sum(int(r["metrics"].get("trades", 0) or 0) for r in ok)
    total_r = sum(float(r["metrics"].get("total_r", 0.0) or 0.0) for r in ok)
    wins = sum(float(r["metrics"].get("win_rate", 0.0) or 0.0) * int(r["metrics"].get("trades", 0) or 0) for r in ok)
    return {
        "tasks": len(ok),
        "trades": trades,
        "win_rate": (wins / trades) if trades else None,
        "avg_r": (total_r / trades) if trades else None,
        "total_r": total_r,
        "errors": sum(1 for r in rows if r["status"] != "ok"),
    }


def enrich_trade_stats(trades: list[dict[str, Any]]) -> dict[str, Any]:
    """Right-tail and monthly concentration; None when there is no positive total."""
    vals = [float(t.get("r_multiple", 0.0) or 0.0) for t in trades]
    if not vals:
        return {"right_tail_share": None, "max_month_share": None}
    total = sum(vals)
    n = max(1, int(len(vals) * 0.10))
    top = sorted(vals, reverse=True)[:n]
    right_tail = (sum(top) / total) if total > 0 else None
    months: dict[str, float] = {}
    for t, r in zip(trades, vals):
        ts = t.get("exit_time") or t.get("entry_time") or t.get("signal_time")
        if ts is None:
            continue
        try:
            key = pd.Timestamp(ts).strftime("%Y-%m")
        except Exception:
            continue
        months[key] = months.get(key, 0.0) + r
    max_month = (max(months.values()) / total) if total > 0 and months else None
    return {
        "right_tail_share": right_tail,
        "max_month_share": max_month,
        "months": months,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--manifest", type=Path, default=None)
    ap.add_argument("--data-dir", type=Path, default=None)
    ap.add_argument("--experiments", type=Path, default=None)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--workers", type=int, default=min(8, os.cpu_count() or 2))
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--allow-test", action="store_true")
    ap.add_argument("--limit-datasets", type=int, default=0)
    ap.add_argument("--max-tasks", type=int, default=0)
    args = ap.parse_args()

    datasets = load_manifest(args.manifest, args.data_dir)
    if args.limit_datasets:
        datasets = datasets[: args.limit_datasets]
    experiments = load_experiments(args.experiments)

    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "trades").mkdir(exist_ok=True)

    # Manifest + hashes.  The test split is never silently included.
    accessible = []
    skipped = []
    for d in datasets:
        p = Path(d["path"])
        if not p.exists():
            skipped.append({**d, "reason": "missing file"})
            continue
        if d["split"] == "test" and not args.allow_test:
            skipped.append({**d, "reason": "test split requires --allow-test"})
            continue
        d = dict(d)
        d["sha256"] = sha256_file(p)
        d["rows"] = int(sum(1 for _ in p.open("rb")) - 1)
        accessible.append(d)

    run_manifest = {
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "host": socket.gethostname(),
        "platform": platform.platform(),
        "python": sys.version,
        "git_head": _git_head(),
        "args": {k: str(v) for k, v in vars(args).items()},
        "base": BASE,
        "experiments": experiments,
        "datasets": accessible,
        "skipped": skipped,
    }
    (args.out / "run_manifest.json").write_text(
        json.dumps(run_manifest, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    tasks = []
    for d in accessible:
        for name, overrides in experiments.items():
            params = make_params(BASE, overrides, d)
            tasks.append(
                {
                    "key": task_key(name, d),
                    "experiment": name,
                    "dataset": d,
                    "params": params,
                }
            )
    if args.max_tasks:
        tasks = tasks[: args.max_tasks]

    print(f"datasets={len(accessible)} skipped={len(skipped)} experiments={len(experiments)} tasks={len(tasks)}")
    if skipped:
        print("skipped:", json.dumps(skipped, indent=2))
    if args.dry_run:
        for t in tasks:
            print(t["key"], json.dumps(t["params"], sort_keys=True))
        return

    completed_path = args.out / "completed.json"
    completed: dict[str, Any] = {}
    if args.resume and completed_path.exists():
        completed = json.loads(completed_path.read_text(encoding="utf-8"))
    tasks = [t for t in tasks if t["key"] not in completed]
    print(f"pending={len(tasks)} workers={args.workers}")

    results: list[dict[str, Any]] = list(completed.values())
    errors: list[dict[str, Any]] = []
    started = time.time()
    with ProcessPoolExecutor(max_workers=max(1, args.workers)) as ex:
        futures = {ex.submit(run_task, t): t for t in tasks}
        for i, fut in enumerate(as_completed(futures), 1):
            task = futures[fut]
            try:
                rec = fut.result()
            except Exception as exc:  # noqa: BLE001
                rec = {
                    "key": task["key"],
                    "status": "error",
                    "experiment": task["experiment"],
                    "dataset": task["dataset"]["name"],
                    "split": task["dataset"]["split"],
                    "symbol": task["dataset"].get("symbol", ""),
                    "market": task["dataset"].get("market", ""),
                    "params": task["params"],
                    "metrics": None,
                    "trades": [],
                    "elapsed_s": None,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            results.append(rec)
            if rec["status"] == "ok":
                completed[rec["key"]] = rec
                completed_path.write_text(json.dumps(completed, indent=2), encoding="utf-8")
            else:
                errors.append(rec)
            print(
                f"[{i}/{len(tasks)}] {rec['status']:5s} {rec['key']} "
                f"elapsed={rec.get('elapsed_s')}s",
                flush=True,
            )
            if i % 10 == 0 or i == len(tasks):
                _write_results(args.out, results)

    _write_results(args.out, results)
    with (args.out / "errors.log").open("w", encoding="utf-8") as f:
        for e in errors:
            f.write(f"\n=== {e['key']} ===\n{e['error']}\n")

    summary = _summarize(results)
    (args.out / "summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"done in {round(time.time() - started, 1)}s; results={len(results)} errors={len(errors)}")
    print(json.dumps(summary, indent=2)[:4000])


def _write_results(out: Path, results: list[dict[str, Any]]) -> None:
    flat = []
    trade_dir = out / "trades"
    for r in results:
        row = {
            "key": r["key"],
            "status": r["status"],
            "experiment": r["experiment"],
            "dataset": r["dataset"],
            "split": r["split"],
            "symbol": r["symbol"],
            "market": r["market"],
            "elapsed_s": r.get("elapsed_s"),
            "params_json": json.dumps(r.get("params", {}), sort_keys=True),
        }
        m = r.get("metrics") or {}
        for k in (
            "signals", "trades", "win_rate", "avg_r", "total_r",
            "profit_factor", "max_dd", "not_triggered", "skipped_daily_loss",
        ):
            row[k] = m.get(k)
        flat.append(row)
        trades = r.get("trades") or []
        if trades:
            safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in r["key"])
            pd.DataFrame(trades).to_csv(trade_dir / f"{safe}.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(flat).to_csv(out / "results.csv", index=False, encoding="utf-8-sig")


def _summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {"by_split_experiment": {}, "by_experiment": {}, "by_split": {}}
    for r in results:
        if r["status"] != "ok":
            continue
        m = r["metrics"]
        for group, key in (
            ("by_split_experiment", f"{r['split']}::{r['experiment']}"),
            ("by_experiment", r["experiment"]),
            ("by_split", r["split"]),
        ):
            slot = out[group].setdefault(
                key, {"tasks": 0, "trades": 0, "wins": 0.0, "total_r": 0.0, "max_dd": 0.0, "trade_rows": []}
            )
            n = int(m.get("trades", 0) or 0)
            slot["tasks"] += 1
            slot["trades"] += n
            slot["wins"] += float(m.get("win_rate", 0.0) or 0.0) * n
            slot["total_r"] += float(m.get("total_r", 0.0) or 0.0)
            slot["max_dd"] = min(slot["max_dd"], float(m.get("max_dd", 0.0) or 0.0))
            slot["trade_rows"].extend(r.get("trades") or [])
    for group in out.values():
        for slot in group.values():
            n = slot["trades"]
            slot["win_rate"] = (slot["wins"] / n) if n else None
            slot["avg_r"] = (slot["total_r"] / n) if n else None
            slot.pop("wins", None)
            tail = enrich_trade_stats(slot.pop("trade_rows", []))
            slot["right_tail_share"] = tail["right_tail_share"]
            slot["max_month_share"] = tail["max_month_share"]
    return out


def _git_head() -> str | None:
    try:
        import subprocess
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True, stderr=subprocess.DEVNULL
        ).strip()
    except Exception:
        return None


if __name__ == "__main__":
    main()
