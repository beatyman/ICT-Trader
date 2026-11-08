#!/usr/bin/env python3
"""定时交易决策：有仓位 → 止盈/止损/持有；无仓位 → 是否开仓。

配合定时任务每 15m 触发一次。仓位状态持久化在 --state-file（JSON），跨次运行记忆。

决策逻辑：
  有仓位：
    long:  price <= sl → stop_loss；price >= tp1 → take_profit；否则 hold
    short: price >= sl → stop_loss；price <= tp1 → take_profit；否则 hold
  无仓位：
    从结构化计划里找 status=valid 且 confidence>=阈值 的 setup，
    若当前价已进入 entry_zone → open（记入仓位）；否则 wait。

用法（加密，自动拉 Binance）：
  python scripts/trade_decision.py --symbol ETHUSDT --market crypto \
      --state-file results/eth_position.json --min-confidence 50

用法（读 CSV）：
  python scripts/trade_decision.py --symbol 03690.HK --market hk \
      --csv-15m meituan_15m.csv --csv-4h meituan_4h.csv --csv-1h meituan_1h.csv \
      --csv-daily meituan_daily.csv --state-file results/meituan_position.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ict_trader.structured_plan import structured_plan, build_input_from_data  # noqa: E402


def load_state(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"symbol": None, "position": None, "history": []}


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def find_entry_setup(plan: dict, price: float, min_conf: int) -> dict | None:
    """返回当前价已进入 entry_zone 的、status=valid 且置信度达标的首个 setup。"""
    for s in plan.get("short_setups", []) + plan.get("long_setups", []):
        if s.get("status") != "valid":
            continue
        if s.get("confidence", 0) < min_conf:
            continue
        zone = s.get("entry_zone") or {}
        if zone.get("low") is None or zone.get("high") is None:
            continue
        lo = float(zone["low"])
        hi = float(zone["high"])
        if lo <= price <= hi:
            return s
    return None


def decide(state: dict, plan: dict, price: float, timestamp: str,
           min_conf: int) -> tuple[str, str]:
    """返回 (action, reason)。action ∈ open/hold/take_profit/stop_loss/wait。"""
    pos = state.get("position")
    if pos:
        direction = pos["direction"]
        sl = float(pos["sl"])
        tp1 = float(pos.get("tp1") or 0)
        if direction == "long":
            if price <= sl:
                return "stop_loss", f"price {price} <= SL {sl}"
            if tp1 and price >= tp1:
                return "take_profit", f"price {price} >= TP1 {tp1}"
        else:  # short
            if price >= sl:
                return "stop_loss", f"price {price} >= SL {sl}"
            if tp1 and price <= tp1:
                return "take_profit", f"price {price} <= TP1 {tp1}"
        return "hold", f"price {price} within SL {sl} / TP1 {tp1}, hold"

    # 无仓位 → 找 valid 且置信度足够的 setup，且当前价已进入入场区
    setup = find_entry_setup(plan, price, min_conf)
    if setup is not None:
        lo = float(setup["entry_zone"]["low"])
        hi = float(setup["entry_zone"]["high"])
        return "open", f"{setup['id']} valid setup in entry zone [{lo}, {hi}]"

    # 未开仓：把"为什么不开"说清楚（无 valid 形态 / 置信度不足 / 价格不在区间）
    valid = [s for s in plan.get("short_setups", []) + plan.get("long_setups", [])
             if s.get("status") == "valid"]
    if not valid:
        return "wait", "no valid setup (none aligned with HTF bias)"
    best = max(valid, key=lambda s: s.get("confidence", 0))
    conf = best.get("confidence", 0)
    if conf < min_conf:
        return "wait", f"valid setup {best['id']} confidence {conf} < min {min_conf}"
    lo = float(best["entry_zone"]["low"])
    hi = float(best["entry_zone"]["high"])
    return "wait", f"price {price} outside {best['id']} entry zone [{lo}, {hi}]"


def main() -> None:
    ap = argparse.ArgumentParser(description="trade decision (position-aware)")
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--market", default="crypto")
    ap.add_argument("--csv-15m", default=None)
    ap.add_argument("--csv-4h", default=None)
    ap.add_argument("--csv-1h", default=None)
    ap.add_argument("--csv-daily", default=None)
    ap.add_argument("--state-file", required=True)
    ap.add_argument("--min-confidence", type=int, default=50)
    args = ap.parse_args()

    state_path = Path(args.state_file)
    state = load_state(state_path)

    # 取数据
    if args.csv_15m:
        df_15m = pd.read_csv(args.csv_15m, parse_dates=["date"])
        df_4h = pd.read_csv(args.csv_4h, parse_dates=["date"])
        df_1h = pd.read_csv(args.csv_1h, parse_dates=["date"]) if args.csv_1h else None
        df_daily = pd.read_csv(args.csv_daily, parse_dates=["date"])
    else:
        from ict_trader.ict_extras import fetch_binance_klines
        df_15m = fetch_binance_klines(args.symbol, "15m", 1000)
        df_4h = fetch_binance_klines(args.symbol, "4h", 1000)
        df_1h = fetch_binance_klines(args.symbol, "1h", 1000)
        df_daily = fetch_binance_klines(args.symbol, "1d", 2000)
        if df_15m is None or df_4h is None or df_daily is None:
            print(json.dumps({"error": f"fetch failed for {args.symbol}"}, ensure_ascii=False))
            return

    price = float(df_15m["close"].iloc[-1])
    timestamp = str(df_15m["date"].iloc[-1])
    inp = build_input_from_data(df_15m, df_4h, df_daily, args.symbol, args.market, df_1h)
    plan = structured_plan(inp)
    action, reason = decide(state, plan, price, timestamp, args.min_confidence)

    # 更新仓位状态
    if action in ("stop_loss", "take_profit"):
        pos = state.get("position") or {}
        closed = {
            "symbol": args.symbol,
            "direction": pos.get("direction"),
            "entry": pos.get("entry"),
            "exit": round(price, 4),
            "result": "tp" if action == "take_profit" else "sl",
            "closed_at": timestamp,
        }
        state.setdefault("history", []).append(closed)
        state["position"] = None
    elif action == "open":
        # 与 decide() 用同一套筛选，保证开的就是被判定的那个 setup
        setup = find_entry_setup(plan, price, args.min_confidence)
        if setup:
            direction = "short" if setup["id"].startswith("S") else "long"
            tps = setup.get("tp") or []
            state["position"] = {
                "direction": direction,
                "entry": round(price, 4),
                "sl": setup["sl"],
                "tp1": tps[0]["level"] if tps else None,
                "opened_at": timestamp,
            }
    state["symbol"] = args.symbol

    save_state(state_path, state)

    # Time Machine：开仓前回放诚实胜率（先到止盈/止损的概率 + 历史真实胜负）
    tm_out = None
    if action == "open":
        setup = find_entry_setup(plan, price, args.min_confidence)
        if setup and setup.get("tp"):
            from ict_trader.timemachine import time_machine
            direction = "short" if setup["id"].startswith("S") else "long"
            tm_out = time_machine(df_15m, entry=price, sl=setup["sl"],
                                  tp=setup["tp"][0]["level"], direction=direction,
                                  n_bars=16, replays=2000, seed=19)

    out = {
        "symbol": args.symbol,
        "timestamp": timestamp,
        "price": round(price, 4),
        "action": action,
        "reason": reason,
        "position": state["position"],
        "plan_bias": plan["meta"]["analysis_bias"],
        "plan_confluence": plan["meta"]["htf_confluence_score"],
        "time_machine": tm_out,
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
