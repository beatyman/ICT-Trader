"""Smoke-test a running realtime terminal and its local stores.

Usage:
    python scripts/smoke_realtime.py
    python scripts/smoke_realtime.py --url http://127.0.0.1:8080
"""
from __future__ import annotations

import argparse
import sys

import httpx


def check(condition: bool, name: str, detail: str = "") -> bool:
    mark = "PASS" if condition else "FAIL"
    print(f"[{mark}] {name}" + (f"  {detail}" if detail else ""))
    return condition


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8080")
    ap.add_argument("--symbol", default="BTCUSDT")
    args = ap.parse_args()
    ok = True

    try:
        health = httpx.get(args.url + "/api/health", timeout=20.0)
    except Exception as exc:
        print(f"[FAIL] engine reachable: {exc}")
        return 1
    body = health.json()
    ok &= check(health.status_code == 200, "engine health", f"phase={body.get('phase')}")
    ok &= check(body.get("phase") == "ready", "bootstrap complete", f"{body.get('boot')}")
    health_data = body.get("health") or {}
    ok &= check(bool(health_data.get("redis", {}).get("ok")), "redis ping")
    ok &= check(bool(health_data.get("clickhouse", {}).get("ok")), "clickhouse ping")

    snap = httpx.get(args.url + "/api/snapshot", timeout=20.0).json()
    ok &= check(snap.get("trading_mode") == "PAPER", "paper execution mode")
    ok &= check(len(snap.get("watchlist") or []) > 0, "watchlist populated")
    ok &= check(bool((snap.get("strategy") or {}).get("interval")), "strategy attached")
    ok &= check(bool((snap.get("metrics") or {}).get("latency")), "metrics present")
    ai = snap.get("ai") or {}
    if ai.get("configured"):
        ok &= check(ai.get("model") == "deepseek-flash", "DeepSeek Flash review model")
    else:
        print("[INFO] DeepSeek review disabled (DEEPSEEK_API_KEY not set)")

    bars = httpx.get(
        args.url + "/api/klines",
        params={"symbol": args.symbol, "interval": snap["strategy"]["interval"], "limit": 240},
        timeout=20.0,
    ).json().get("bars") or []
    ok &= check(len(bars) >= 50, "closed-bar read", f"{len(bars)} bars")
    if bars:
        ordered = all(bars[i]["open_time"] < bars[i + 1]["open_time"] for i in range(len(bars) - 1))
        ok &= check(ordered, "bar ordering")

    sessions = (snap.get("health") or {}).get("sessions") or {}
    gaps = sum(int(s.get("gaps", 0)) for s in sessions.values())
    print(f"[INFO] sessions={len(sessions)} gaps={gaps} "
          f"ch_rows={(snap.get('health') or {}).get('clickhouse', {}).get('rows')} "
          f"equity={snap.get('account', {}).get('equity')}")

    print("\nRESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
