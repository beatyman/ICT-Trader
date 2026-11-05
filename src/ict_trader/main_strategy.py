"""模块 3：ICT 主策略层（5m/15m）——唯一入场信号源。

信号链（必须按顺序，缺一不发信号）：
  1. 1H/15m 结构方向明确（BOS/CHoCH，L5-03）
  2. 5m 流动性扫单（前高/前低被扫后收回，L2-04）
  3. 5m 结构转变 / 位移（MSS / 强动能，L1-04 或 L3-10）
  4. 回踩 OB/FVG，且位于折价区（做多）/ 溢价区（做空）
  5. 触发（回踩确认后突破触发 K 线极值，或收盘确认）

约束：未确认扫单不赌；结构不明不发信号；5m 信号不得绕过背景层。
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .concepts import analyze, ICTConfig
from .rules import audit


@dataclass
class ICTSignal:
    direction: str = "neutral"          # long / short / neutral
    confidence: float = 0.0             # 0-100
    entry: float = 0.0
    stop: float = 0.0
    tp1: float = 0.0
    tp2: float = 0.0
    invalidation: float = 0.0
    setup_type: str = ""
    htf_alignment: bool = False
    premium_discount: str = ""          # premium / discount / mid
    reasons: list = field(default_factory=list)
    rules: dict = field(default_factory=dict)


def ict_signal(df_5m: pd.DataFrame, df_daily: pd.DataFrame, market: str = "crypto",
               min_rr: float = 2.0) -> ICTSignal:
    """ICT 主策略：跑分析 + 审计，输出入场信号。"""
    sig = ICTSignal()
    price = float(df_5m["close"].iloc[-1])

    # 分析（拿扫单/结构/OB/FVG）
    res = analyze(df_5m, ICTConfig(mode="historical", show_sessions=False,
                                   show_htf_cisd=False, cisd_all=True, cisd_min_grade=0))
    # 审计（拿规则 PASS/FAIL）
    a = audit(df_5m, daily=df_daily, market=market)
    by_id = {r["rule_id"]: r for r in a["rules"]}
    sig.rules = {rid: by_id[rid]["status"] for rid in by_id}

    atr = float(a["atr"]) if a.get("atr") else 1.0

    # ---- 1. HTF 方向 ----
    htf_dir = None
    r = by_id.get("L5-03")
    if r and isinstance(r.get("value"), dict):
        htf_dir = r["value"].get("direction")  # long/short
    htf_ok = by_id.get("L5-03", {}).get("status") == "PASS"
    sig.htf_alignment = htf_ok

    # ---- 2. 扫单 ----
    has_sweep = by_id.get("L2-04", {}).get("status") == "PASS"
    sweeps = res.sweeps
    recent_sweep = sweeps[-1] if sweeps else None

    # ---- 3. MSS / 位移 ----
    has_mss = by_id.get("L1-04", {}).get("status") == "PASS"
    has_displacement = by_id.get("L3-10", {}).get("status") == "PASS"

    # ---- 4. 溢价/折价 ----
    zone = None
    r506 = by_id.get("L5-06")
    if r506 and isinstance(r506.get("value"), dict):
        zone = r506["value"].get("zone")
    sig.premium_discount = zone or "mid"

    # ---- 方向判定（扫单方向 + MSS + HTF） ----
    direction = "neutral"
    if has_sweep and recent_sweep is not None and (has_mss or has_displacement):
        sweep_dir = recent_sweep.direction  # bull / bear
        # 扫单方向 → 反转方向（bull sweep → long；bear sweep → short）
        dir_from_sweep = "long" if sweep_dir == "bull" else "short"
        # 折价做多 / 溢价做空校验
        if dir_from_sweep == "long" and zone == "discount":
            direction = "long"
        elif dir_from_sweep == "short" and zone == "premium":
            direction = "short"
        elif (has_mss and has_displacement):
            # 结构+位移强，即使位置不是完美折价/溢价也允许
            direction = dir_from_sweep
    sig.direction = direction

    if direction == "neutral":
        if not has_sweep:
            sig.reasons.append("no confirmed liquidity sweep")
        else:
            sig.reasons.append("no MSS/displacement, or position not in discount/premium")
        return sig

    # ---- 入场/止损/目标 ----
    if direction == "long":
        stop = float(recent_sweep.sweep_price) - 0.1 * atr
        entry = price
        risk = entry - stop
        tp1 = entry + risk * min_rr
        tp2 = entry + risk * min_rr * 1.5
        sig.invalidation = stop
    else:
        stop = float(recent_sweep.sweep_price) + 0.1 * atr
        entry = price
        risk = stop - entry
        tp1 = entry - risk * min_rr
        tp2 = entry - risk * min_rr * 1.5
        sig.invalidation = stop

    sig.entry = round(entry, 6)
    sig.stop = round(stop, 6)
    sig.tp1 = round(tp1, 6)
    sig.tp2 = round(tp2, 6)
    sig.setup_type = "liquidity_sweep + MSS + OB/FVG retest"

    # ---- 置信度（0-100） ----
    conf = 0
    if htf_ok:
        conf += 25
    if has_sweep:
        conf += 25
    if has_mss:
        conf += 20
    if has_displacement:
        conf += 15
    if direction == "long" and zone == "discount":
        conf += 15
    elif direction == "short" and zone == "premium":
        conf += 15
    sig.confidence = float(min(100, conf))
    if htf_ok:
        sig.reasons.append("1H/15m structure aligned")
    return sig
