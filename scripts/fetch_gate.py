#!/usr/bin/env python
"""Fetch USDT perpetual candles from Gate.io futures into the engine's CSV format.

Binance endpoints are unreachable from this environment (DNS answers with an
unroutable address), so the cross-symbol study uses Gate.io's public futures
candlesticks, which speak the same instruments.  Columns match
``examples/15m.csv`` so everything downstream (load_csv, analyze, build_setup)
works unchanged.

  python scripts/fetch_gate.py --symbols BTC ETH SOL BNB XRP DOGE ADA AVAX LINK \
      --timeframes 15m 1h --days 730 --out-dir output/ict/gate
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

import pandas as pd

BASE = "https://api.gateio.ws/api/v4/futures/usdt/candlesticks"
TF_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "4h": 14400, "1d": 86400}
MAX_LIMIT = 2000
# Gate rejects any range older than the last 10,000 candles of the interval
# ("Maximum 10000 points recently are allowed"), so the usable lookback is
# 10,000 * interval, not whatever --days asks for.
# A 200-candle safety margin: the newest candle Gate serves lags "now" by a few
# minutes, and the limit is measured from the newest candle, not from now.
MAX_POINTS = 9_800


def fetch_page(contract: str, interval: str, frm: int, to: int, timeout: int = 25) -> list[dict]:
    query = urllib.parse.urlencode({"contract": contract, "interval": interval,
                                    "from": frm, "to": to})
    url = f"{BASE}?{query}"
    for attempt in range(5):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as resp:
                return json.loads(resp.read().decode())
        except Exception as exc:  # noqa: BLE001 - transient network, retried
            if attempt == 4:
                raise SystemExit(f"{contract} {interval}: {exc}") from exc
            # Gate starts refusing connections when hammered; back off properly.
            time.sleep(5.0 * (attempt + 1))
    return []


def fetch_range(contract: str, interval: str, start: int, end: int) -> pd.DataFrame:
    step = TF_SECONDS[interval] * MAX_LIMIT
    oldest = end - TF_SECONDS[interval] * MAX_POINTS
    if start < oldest:
        print(f"  note: {contract} {interval} history limited to the last "
              f"{MAX_POINTS} candles (from {pd.Timestamp(oldest, unit='s')})", file=sys.stderr)
        start = oldest
    rows: dict[int, dict] = {}
    cursor = start
    while cursor < end:
        chunk_end = min(cursor + step, end)
        for r in fetch_page(contract, interval, cursor, chunk_end):
            rows[int(r["t"])] = r
        cursor = chunk_end
        time.sleep(0.15)  # stay well inside the public rate limit
    if not rows:
        return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume", "turnover"])
    df = pd.DataFrame(
        [
            {
                "date": pd.Timestamp(int(t), unit="s", tz="UTC").tz_localize(None),
                "open": float(r["o"]), "high": float(r["h"]), "low": float(r["l"]),
                "close": float(r["c"]), "volume": float(r["v"]), "turnover": float(r.get("sum", 0.0)),
            }
            for t, r in rows.items()
        ]
    ).sort_values("date").reset_index(drop=True)
    return df


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--symbols", nargs="+",
                    default=["BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "ADA", "AVAX", "LINK"])
    ap.add_argument("--timeframes", nargs="+", default=["15m", "1h"])
    ap.add_argument("--days", type=int, default=730)
    ap.add_argument("--end", default="", help="ISO end time (default: now, floored to the hour)")
    ap.add_argument("--out-dir", default="output/ict/gate")
    args = ap.parse_args()

    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    end = (pd.Timestamp(args.end) if args.end
           else pd.Timestamp.now(tz="UTC").tz_localize(None)).floor("h")
    start = end - pd.Timedelta(days=args.days)
    for sym in args.symbols:
        for tf in args.timeframes:
            df = fetch_range(f"{sym}_USDT", tf, int(start.timestamp()), int(end.timestamp()))
            if df.empty:
                print(f"{sym} {tf}: EMPTY", file=sys.stderr)
                continue
            path = out / f"{sym.lower()}_{tf}.csv"
            if path.exists():
                try:
                    have = sum(1 for _ in path.open(encoding="utf-8")) - 1
                except OSError:
                    have = 0
                if have >= 0.95 * (len(df) if not df.empty else 0):
                    print(f"{sym:5} {tf:3} already present ({have} bars) - skipped")
                    continue
            df.to_csv(path, index=False, encoding="utf-8")
            print(f"{sym:5} {tf:3} {len(df):6d} bars  {df['date'].iloc[0]} -> {df['date'].iloc[-1]}  {path}")


if __name__ == "__main__":
    main()
