"""Run a real Kronos forecast on the GPU machine and cache to CSV.

Usage (on the dev box, inside the venv):

    python run_forecast.py --csv btc_15m.csv --out cache/btc_kronos.csv \
        --repo /root/ict/Kronos --device cuda:0 --n-samples 24 --step 16
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pandas as pd  # noqa: E402

from adapter import KronosAdapter  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--repo", required=True, help="Kronos repo root")
    ap.add_argument("--device", default="cuda:0")
    ap.add_argument("--n-samples", type=int, default=24)
    ap.add_argument("--step", type=int, default=16)
    ap.add_argument("--context", type=int, default=512)
    ap.add_argument("--horizon", type=int, default=12)
    ap.add_argument("--model", default="NeoQuasar/Kronos-small")
    ap.add_argument("--tokenizer", default="NeoQuasar/Kronos-Tokenizer-base")
    args = ap.parse_args()

    df = pd.read_csv(args.csv, parse_dates=["date"])
    print(f"bars={len(df)} last={df.date.iloc[-1]} close={df.close.iloc[-1]}")

    adapter = KronosAdapter(
        tokenizer_name=args.tokenizer,
        model_name=args.model,
        context_len=args.context,
        horizon=args.horizon,
        n_samples=args.n_samples,
        device=args.device,
        repo_path=args.repo,
    )
    out = adapter.generate_offline_forecasts(df, args.out, step=args.step)
    print(f"saved {args.out} rows={len(out)}")
    print(out.tail(3).to_string())


if __name__ == "__main__":
    main()
