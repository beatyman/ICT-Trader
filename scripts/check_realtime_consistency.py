"""Read-only consistency audit for the running realtime paper terminal."""
from __future__ import annotations

import json
import sys
from collections import Counter

import requests

BASE = "http://127.0.0.1:8080"


def main() -> int:
    snap = requests.get(f"{BASE}/api/snapshot", timeout=15).json()
    positions = {p["symbol"]: p for p in snap.get("positions", [])}
    setups = snap.get("setups", [])
    open_setups = {}
    for s in setups:
        if s.get("status") == "OPEN":
            open_setups.setdefault(s["symbol"], []).append(s)
    orphan = sorted(sym for sym in positions if sym not in open_setups)
    duplicate = sorted(sym for sym, rows in open_setups.items() if len(rows) > 1)
    no_position = sorted(sym for sym in open_setups if sym not in positions)
    engines = Counter(s.get("engine") or "?" for s in setups)
    signals = Counter(s.get("engine") or "?" for s in snap.get("engine_signals", []))
    report = {
        "phase": snap.get("phase"),
        "positions": sorted(positions),
        "open_setups": {k: len(v) for k, v in open_setups.items()},
        "orphan_positions": orphan,
        "duplicate_open_setups": duplicate,
        "open_setup_without_position": no_position,
        "setup_engines": dict(engines),
        "signal_engines": dict(signals),
        "ok": not orphan and not duplicate and not no_position,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
