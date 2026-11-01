"""Fetch full Binance kline history for a symbol (paginated, multi-timeframe).

Usage:
    python scripts/fetch_binance.py BTCUSDT --start 2025-01-01 --timeframes 15m 1h 4h 1d
    python scripts/fetch_binance.py UNIUSDT --start 2025-01-01 --fapi
"""
from __future__ import annotations

import argparse
import datetime
import time
from pathlib import Path

import pandas as pd
import requests

BASE_SPOT = "https://api.binance.com/api/v3/klines"
BASE_FAPI = "https://fapi.binance.com/fapi/v1/klines"
OUT_DIR = Path(__file__).resolve().parents[1] / "output" / "ict"


def _get(base: str, symbol: str, interval: str, end_ms: int, retries: int = 6) -> list:
    for i in range(retries):
        try:
            r = requests.get(base, params={
                "symbol": symbol, "interval": interval,
                "limit": 1000, "endTime": end_ms,
            }, timeout=30)
            r.raise_for_status()
            return r.json()
        except Exception:
            if i == retries - 1:
                raise
            time.sleep(2 * (i + 1))
    return []


def fetch_all(symbol: str, interval: str, start: str, fapi: bool = False) -> pd.DataFrame:
    base = BASE_FAPI if fapi else BASE_SPOT
    start_ms = int(pd.Timestamp(start, tz="UTC").timestamp() * 1000)
    end_ms = int(datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000)
    frames: list[pd.DataFrame] = []
    end = end_ms
    while True:
        data = _get(base, symbol, interval, end)
        if not data:
            break
        df = pd.DataFrame(data, columns=[
            "ot", "open", "high", "low", "close", "volume",
            "ct", "qv", "n", "tb", "tq", "ig",
        ])
        df["date"] = pd.to_datetime(df["ot"], unit="ms")
        for c in ["open", "high", "low", "close", "volume"]:
            df[c] = df[c].astype(float)
        frames.append(df[["date", "open", "high", "low", "close", "volume"]])
        earliest = df["date"].min()
        if earliest <= pd.Timestamp(start) or len(data) < 1000:
            break
        end = int(earliest.timestamp() * 1000) - 1
    out = pd.concat(frames).drop_duplicates("date").sort_values("date").reset_index(drop=True)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("symbol")
    ap.add_argument("--start", default="2025-01-01")
    ap.add_argument("--timeframes", nargs="+", default=["15m", "1h", "4h", "1d"])
    ap.add_argument("--fapi", action="store_true", help="use futures endpoint")
    args = ap.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    sym = args.symbol
    for tf in args.timeframes:
        df = fetch_all(sym, tf, args.start, fapi=args.fapi)
        # drop the forming (last) bar
        df = df.iloc[:-1]
        out = OUT_DIR / f"{sym.lower()}_{tf}_full.csv"
        df.to_csv(out, index=False)
        print(f"{sym} {tf}: {len(df)} bars  {df.date.iloc[0]} -> {df.date.iloc[-1]}  last close {df.close.iloc[-1]:,.4g}")


if __name__ == "__main__":
    main()
