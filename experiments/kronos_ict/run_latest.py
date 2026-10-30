"""Single-point forecast as of the last closed bar (real Kronos, GPU).

Usage (on the dev box):

    python run_latest.py --csv btc_15m.csv --repo /root/ict/Kronos \
        --device cuda:0 --n-samples 32
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pandas as pd  # noqa: E402

from adapter import KronosAdapter  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", required=True)
    ap.add_argument("--repo", required=True)
    ap.add_argument("--device", default="cuda:0")
    ap.add_argument("--n-samples", type=int, default=32)
    ap.add_argument("--context", type=int, default=512)
    ap.add_argument("--horizon", type=int, default=12)
    args = ap.parse_args()

    df = pd.read_csv(args.csv, parse_dates=["date"])
    adapter = KronosAdapter(
        context_len=args.context, horizon=args.horizon,
        n_samples=args.n_samples, device=args.device, repo_path=args.repo,
    )
    f = adapter.predict_latest(df)
    print(json.dumps(f.as_row(), ensure_ascii=False))


if __name__ == "__main__":
    main()
