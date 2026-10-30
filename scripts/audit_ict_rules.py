#!/usr/bin/env python
"""Audit a CSV against the layered ICT rule system.

Example:
  python scripts/audit_ict_rules.py \
    --csv output/ict/us_msft_15m_closed.csv \
    --daily-csv output/ict/us_msft_daily_closed.csv \
    --market us \
    --plan-json output/ict/msft_plan.json \
    --json-out output/ict/msft_audit.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ict_trader.concepts import load_csv  # noqa: E402
from ict_trader.rules import audit  # noqa: E402


def _load_plan(path: str | None) -> dict | None:
    if not path:
        return None
    obj = json.loads(Path(path).read_text(encoding="utf-8"))
    if obj.get("decision") != "TRADE" or not obj.get("setup"):
        return None
    s = obj["setup"]
    d = s.get("direction")
    return {
        "direction": "long" if d in {"bull", "long"} else "short",
        "entry": float(s.get("entry_reference", s["level"])),
        "sl": float(s["stop"]),
        "tp": float(s["target"]),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--csv", required=True)
    ap.add_argument("--daily-csv", default=None)
    ap.add_argument("--market", choices=["crypto", "us", "hk", "other"], default="crypto")
    ap.add_argument("--plan-json", default=None)
    ap.add_argument("--smt-csv", default=None)
    ap.add_argument("--rule-mode", choices=["strict", "shadow"], default="strict")
    ap.add_argument("--touch-atr-source", choices=["htf", "ltf"], default="htf")
    ap.add_argument("--json-out", default="output/ict/rule_audit.json")
    args = ap.parse_args()

    df = load_csv(args.csv)
    daily = load_csv(args.daily_csv) if args.daily_csv else None
    smt = load_csv(args.smt_csv) if args.smt_csv else None
    report = audit(df, daily=daily, market=args.market,
                   plan=_load_plan(args.plan_json), smt_df=smt, mode=args.rule_mode,
                   touch_atr_source=args.touch_atr_source)

    out = Path(args.json_out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str),
                   encoding="utf-8")

    print(f"decision: {report['decision']}")
    print(f"strict: {report.get('strict_decision')}  mode: {report.get('rule_mode')}")
    print(f"blocking failures: {', '.join(report['blocking_failures']) or 'none'}")
    for r in report["rules"]:
        print(f"{r['rule_id']:<7} {r['status']:<4} {r['name']}")
    print(f"saved {out}")


if __name__ == "__main__":
    main()
