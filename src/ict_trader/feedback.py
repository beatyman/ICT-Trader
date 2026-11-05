"""模块 6+7：结果跟踪、反馈评分与归因分析。

反馈评分（事后按 1h/4h/24h 评估）：
  方向正确：达 TP2 +2；达 TP1 +1；未达 TP1 +0.5。
  方向错误：正常止损 -1；亏损大于止损距离按比例加重（最高 -3）；保本/小亏 -0.5。
  附加扣分：逆背景 -1；云内/区间内 -1；辅助 veto 仍入场 -2；无失效条件 -1。

归因：按 ICT setup / 辅助状态 / 时段 / 品种分组统计。
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict

import numpy as np
import pandas as pd


@dataclass
class TradeRecord:
    time: str = ""
    symbol: str = ""
    direction: str = ""
    entry: float = 0.0
    stop: float = 0.0
    tp1: float = 0.0
    tp2: float = 0.0
    invalidation: float = 0.0
    confidence: float = 0.0
    score: float = 0.0
    size_factor: float = 0.0
    setup_type: str = ""
    wyckoff_bias: str = ""
    ichimoku_cloud: str = ""
    session: str = ""
    # 结果
    exit_price: float = 0.0
    exit_reason: str = ""          # tp1/tp2/stop/breakeven/eod
    pnl: float = 0.0
    ret_pct: float = 0.0
    direction_correct: bool = False
    feedback: float = 0.0
    penalty_flags: list = field(default_factory=list)


def feedback_score(rec: TradeRecord, loss_ratio: float = 1.0) -> float:
    """按方向正误 + 附加扣分计算反馈分。"""
    s = 0.0
    if rec.direction_correct:
        if rec.exit_reason == "tp2":
            s += 2.0
        elif rec.exit_reason == "tp1":
            s += 1.0
        else:
            s += 0.5
    else:
        if loss_ratio > 1.5:
            s -= 3.0
        elif loss_ratio > 1.0:
            s -= 2.0
        elif loss_ratio > 0.3:
            s -= 1.0
        else:
            s -= 0.5
    # 附加扣分
    for flag in rec.penalty_flags:
        if flag == "counter_background":
            s -= 1.0
        elif flag == "inside_cloud_or_range":
            s -= 1.0
        elif flag == "veto_overridden":
            s -= 2.0
        elif flag == "no_invalidation":
            s -= 1.0
    return round(s, 2)


def attribution(records: list[TradeRecord]) -> dict:
    """归因：分组统计 + 汇总绩效。"""
    if not records:
        return {"n": 0, "groups": {}}
    df = pd.DataFrame([asdict(r) for r in records])

    def _stats(sub: pd.DataFrame) -> dict:
        n = len(sub)
        if n == 0:
            return {"n": 0, "win_rate": 0.0, "pf": 0.0, "expectancy": 0.0,
                    "avg_feedback": 0.0, "net_pnl": 0.0}
        wins = sub[sub["pnl"] > 0]
        losses = sub[sub["pnl"] < 0]
        gp = wins["pnl"].sum()
        gl = -losses["pnl"].sum()
        return {
            "n": n,
            "win_rate": round(len(wins) / n, 3),
            "pf": round(gp / gl, 3) if gl > 0 else 99.0,
            "expectancy": round(float(sub["pnl"].mean()), 2),
            "avg_feedback": round(float(sub["feedback"].mean()), 3),
            "net_pnl": round(float(sub["pnl"].sum()), 2),
        }

    groups = {}
    for key, col in [("setup_type", "setup_type"),
                     ("wyckoff_bias", "wyckoff_bias"),
                     ("ichimoku_cloud", "ichimoku_cloud"),
                     ("session", "session")]:
        groups[key] = {str(k): _stats(g) for k, g in df.groupby(col)}

    # 加重扣分样本单独统计
    heavy = df[df["feedback"] <= -3.0]
    groups["heavy_loss"] = _stats(heavy)

    total = _stats(df)
    # 权益曲线回撤
    eq = df["pnl"].cumsum()
    cummax = eq.cummax()
    max_dd = float(((eq - cummax) / (cummax + 1e-9)).min()) if len(eq) else 0.0
    return {"n": len(records), "total": total, "max_drawdown": round(max_dd, 4),
            "groups": groups}


def discover_problems(records: list[TradeRecord]) -> list[dict]:
    """归因后发现问题（至少 3 个），给出假设 + 验证 + 调整。"""
    if len(records) < 10:
        return [{"problem": "sample too small (<10)", "action": "collect more trades"}]
    a = attribution(records)
    problems = []
    g = a["groups"]
    # 1. 扫单未确认 / 结构不明（ICT 单独绩效差）
    if "setup_type" in g:
        for k, v in g["setup_type"].items():
            if v["n"] >= 3 and v["win_rate"] < 0.35 and v["expectancy"] < 0:
                problems.append({
                    "problem": f"setup '{k}' has win_rate {v['win_rate']}, expectancy {v['expectancy']}",
                    "hypothesis": "unconfirmed sweep / weak structure passing through",
                    "verify": "raise sweep-confirmation and displacement gates",
                    "adjust": "require L1-04 (MSS) and L3-10 (displacement) both PASS",
                })
    # 2. 辅助 veto 被绕过
    if "veto_overridden" in str(records):
        problems.append({"problem": "auxiliary veto overridden",
                         "action": "make veto hard (no override)", "adjust": "enforce fusion veto"})
    # 3. 云内/区间内交易
    for k, v in g.get("ichimoku_cloud", {}).items():
        if k == "inside" and v["n"] >= 2 and v["expectancy"] < 0:
            problems.append({"problem": f"inside-cloud trades losing ({k})",
                             "action": "keep cloud veto", "adjust": "never trade inside cloud"})
    # 4. 加重亏损样本
    hv = g.get("heavy_loss", {})
    if hv.get("n", 0) >= 1:
        problems.append({"problem": f"{hv['n']} heavy-loss trades (feedback<=-3)",
                         "action": "review stop width and counter-background entries",
                         "adjust": "widen/shrink stop per ATR, tighten background veto"})
    # 5. 时段不适
    for k, v in g.get("session", {}).items():
        if v["n"] >= 3 and v["expectancy"] < 0:
            problems.append({"problem": f"session '{k}' losing",
                             "action": "restrict to high-quality killzones",
                             "adjust": "add session filter"})
    return problems
