"""Backfill Binance spot klines into ict.klines for the live top-N symbols.

Target counts are analysis windows, not strategy parameters:
  15m 36,000 / 1h 9,000 / 4h 3,000 / 1d 1,000.
"""
from __future__ import annotations

import argparse
import json
import time
from typing import Iterable

import clickhouse_connect
import requests

from ict_trader.realtime.models import CH_COLUMNS, Kline, interval_ms

TARGETS = {"15m": 36_000, "1h": 9_000, "4h": 3_000, "1d": 1_000}
BASE = "https://api.binance.com/api/v3/klines"


def fetch(session: requests.Session, symbol: str, interval: str,
          start_ms: int, end_ms: int, pause: float) -> list[Kline]:
    step = interval_ms(interval)
    out: list[Kline] = []
    cursor = start_ms
    while cursor <= end_ms:
        chunk_end = min(end_ms, cursor + step * 999)
        params = {"symbol": symbol, "interval": interval,
                  "startTime": cursor, "endTime": chunk_end, "limit": 1000}
        for attempt in range(6):
            r = session.get(BASE, params=params, timeout=30)
            if r.status_code == 429:
                time.sleep(max(2.0, float(r.headers.get("Retry-After", 2))))
                continue
            r.raise_for_status()
            payload = r.json()
            break
        else:
            raise RuntimeError(f"failed {symbol} {interval}")
        if not payload:
            break
        for row in payload:
            bar = Kline.from_rest(symbol, interval, row)
            bar.source = "backfill"
            out.append(bar)
        newest = max(int(row[0]) for row in payload)
        if newest < cursor:
            break
        cursor = newest + step
        time.sleep(pause)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default="")
    ap.add_argument("--pause", type=float, default=0.12)
    args = ap.parse_args()
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    if not symbols:
        snap = requests.get("http://127.0.0.1:8080/api/snapshot", timeout=10).json()
        symbols = [s.upper() for s in snap["config"]["symbols"]]
    now = int(time.time() * 1000)
    client = clickhouse_connect.get_client(host="127.0.0.1", port=8123, database="ict")
    session = requests.Session()
    session.headers["User-Agent"] = "ict-trader-backfill/1.0"
    for symbol in symbols:
        for interval, target in TARGETS.items():
            step = interval_ms(interval)
            start = now - target * step
            bars = fetch(session, symbol, interval, start, now, args.pause)
            if bars:
                client.insert("ict.klines", [b.to_ch_row() for b in bars],
                              column_names=CH_COLUMNS)
            print(json.dumps({"symbol": symbol, "interval": interval,
                              "target": target, "fetched": len(bars),
                              "first": bars[0].open_time if bars else None,
                              "last": bars[-1].open_time if bars else None}),
                  flush=True)
    client.close()


if __name__ == "__main__":
    main()
