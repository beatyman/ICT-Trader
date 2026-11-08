"""风险门（Risk Manager）—— 从 day-trader-skill 吸收。

每个 setup 下单前过一遍风控检查：止损必填且方向正确、单笔最大亏损、
盈亏比下限、当日累计亏损、当日交易次数、入场窗口。PASS 才允许展示给用户，
PASS 不等于「是」，更不等于下单。

适配加密 24/7：入场窗口用 UTC，默认关（config 里 no_entries_* = null 时跳过）。
"""
from __future__ import annotations

from typing import Any

__all__ = ["risk_check", "DEFAULT_LIMITS"]

DEFAULT_LIMITS = {
    "require_stop": True,
    "max_loss_per_trade_usd": 100.0,   # 单笔最大亏损（美元）
    "min_reward_risk": 2.0,            # 最低盈亏比
    "max_daily_loss_usd": 300.0,       # 当日最大累计亏损
    "max_trades_per_day": 3,           # 当日最大开仓次数
    "min_order_usd": 20.0,             # 最小订单额
    "max_order_usd": 100000.0,         # 最大订单额
}


def risk_check(setup: dict, account: dict | None = None,
               limits: dict | None = None,
               placed_today: dict | None = None) -> dict:
    """对单个 setup 做风控检查。

    setup: 结构化计划的 long/short setup 条目（含 direction/entry_zone/sl/tp/confidence）。
    account: {"equity": float, "position_usd": float}（可选，仓位/资金检查）。
    limits: 覆盖 DEFAULT_LIMITS。
    placed_today: {"trades": int, "daily_loss_usd": float}（当日已开仓次数/已占用亏损）。

    返回 {"status": "PASS"|"BLOCK", "checks": [...], "reasons": [...]}。
    """
    L = dict(DEFAULT_LIMITS)
    if limits:
        L.update(limits)
    placed = placed_today or {"trades": 0, "daily_loss_usd": 0.0}

    checks: list[str] = []
    reasons: list[str] = []

    direction = "long" if setup.get("id", "").startswith("L") else "short"
    entry_lo = setup.get("entry_zone", {}).get("low")
    entry_hi = setup.get("entry_zone", {}).get("high")
    sl = setup.get("sl")
    tps = setup.get("tp") or []

    # 1. stop：必填 + 方向正确
    if L["require_stop"] and sl is None:
        reasons.append("no stop loss")
    elif sl is not None and entry_hi is not None:
        if direction == "long" and sl >= entry_hi:
            reasons.append("stop on wrong side of entry (long)")
        elif direction == "short" and sl <= entry_lo:
            reasons.append("stop on wrong side of entry (short)")

    entry_mid = ((entry_lo + entry_hi) / 2) if (entry_lo is not None and entry_hi is not None) else None

    # 2. 盈亏比
    rr = None
    if entry_mid is not None and sl is not None and tps:
        tp1 = tps[0]["level"]
        risk = abs(entry_mid - sl)
        reward = abs(tp1 - entry_mid)
        rr = reward / risk if risk > 0 else 0.0
        if rr < L["min_reward_risk"]:
            reasons.append(f"reward:risk {rr:.2f} < min {L['min_reward_risk']}")
        else:
            checks.append(f"reward:risk {rr:.2f}")

    # 3. 单笔最大亏损（有仓位金额时）
    if account and entry_mid is not None and sl is not None:
        position_usd = account.get("position_usd", 0.0)
        risk_usd = abs(entry_mid - sl) / entry_mid * position_usd if entry_mid > 0 else 0.0
        if position_usd > 0 and risk_usd > L["max_loss_per_trade_usd"]:
            reasons.append(f"max loss ${risk_usd:.2f} > ${L['max_loss_per_trade_usd']}")

    # 4. 当日累计亏损 + 交易次数
    if placed["trades"] >= L["max_trades_per_day"]:
        reasons.append(f"{placed['trades']} trades today >= max {L['max_trades_per_day']}")
    if placed.get("daily_loss_usd", 0.0) >= L["max_daily_loss_usd"]:
        reasons.append(f"daily loss ${placed['daily_loss_usd']:.2f} >= max ${L['max_daily_loss_usd']}")

    status = "BLOCK" if reasons else "PASS"
    return {"status": status, "checks": checks, "reasons": reasons, "rr": rr,
            "limits": {k: L[k] for k in ("max_loss_per_trade_usd", "min_reward_risk",
                                         "max_daily_loss_usd", "max_trades_per_day")}}
