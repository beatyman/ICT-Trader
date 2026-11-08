"""结构化数据契约 + 自洽性校验（Input/Output Schema + signal_id 规范）。

把 structured_plan 的输出从「dict」升级为「可测试、可审计」的契约：
  · validate_output：自洽性硬校验（多空价位顺序 / confidence 范围 / rr 实数计算）
  · assign_signal_ids：给特征条目分配稳定 signal_id（OB-0001 / FVG-0032 / SWP-0001 …）
  · SIGNAL_ID 规范：每个结构化条目 + 图元都能回溯到 signal_id
"""
from __future__ import annotations

import json

from typing import Any

__all__ = ["validate_output", "assign_signal_ids", "OUTPUT_SCHEMA", "INPUT_SCHEMA",
           "validate_input", "parse_ai_response", "validate_visual_consistency",
           "assign_result_signal_ids", "SCHEMA_VERSION"]

# 版本号：Schema / 提示词向后兼容的锚点
SCHEMA_VERSION = "1.0.0"

# ---------------------------------------------------------------------------
# Output Schema（文档化 + 供 AI/校验器参照）
# ---------------------------------------------------------------------------
OUTPUT_SCHEMA: dict[str, Any] = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "schema_version": SCHEMA_VERSION,
    "type": "object",
    "required": ["meta", "long_setups", "short_setups", "no_trade_conditions",
                 "data_gaps", "metadata"],
    "properties": {
        "meta": {"type": "object", "required": ["symbol", "timestamp", "current_price",
                 "analysis_bias", "htf_confluence_score"]},
        "long_setups": {"type": "array", "items": {"$ref": "#/definitions/setup"}},
        "short_setups": {"type": "array", "items": {"$ref": "#/definitions/setup"}},
        "no_trade_conditions": {"type": "array", "items": {"type": "string"}},
        "data_gaps": {"type": "array", "items": {"type": "string"}},
        "metadata": {"type": "object"},
    },
    "definitions": {
        "setup": {
            "type": "object",
            "required": ["id", "status", "setup_type", "entry_zone", "entry_trigger",
                         "sl", "tp", "confidence", "invalidation", "rationale",
                         "signal_refs", "risk_check"],
            "properties": {
                "id": {"type": "string", "pattern": "^(L|S)\\d+$"},
                "status": {"enum": ["valid", "conditional", "invalid"]},
                "entry_zone": {"type": "object", "required": ["low", "high"]},
                "sl": {"type": "number"},
                "tp": {"type": "array", "items": {"type": "object",
                       "required": ["label", "level", "rr"]}},
                "confidence": {"type": "number", "minimum": 0, "maximum": 100},
                "signal_refs": {"type": "array", "items": {"type": "string"}},
            },
        },
    },
}


def validate_output(out: dict) -> dict:
    """校验 structured_plan 输出自洽性。返回 {ok, errors[], warnings[]}。

    硬约束（违反 → errors，视为非法输出）：
      · long: sl < entry < tp；short: tp < entry < sl（不满足 → 该 setup 应被移除/置 neutral）
      · confidence ∈ [0, 100]
      · entry_zone.low <= entry_zone.high
      · tp[].rr 由 (tp-entry)/(entry-sl) 实数计算（容差 0.01）
    软约束（违反 → warnings）：
      · setup 缺 signal_refs（图文一致无法校验）
    """
    errors: list[str] = []
    warnings: list[str] = []
    meta = out.get("meta") or {}
    setups = list(out.get("long_setups", [])) + list(out.get("short_setups", []))

    for s in setups:
        sid = s.get("id", "?")
        direction = "long" if sid.startswith("L") else "short"
        zone = s.get("entry_zone") or {}
        lo, hi = zone.get("low"), zone.get("high")
        sl = s.get("sl")
        if lo is None or hi is None:
            errors.append(f"{sid}: entry_zone missing low/high")
            continue
        if lo > hi:
            errors.append(f"{sid}: entry_zone inverted ({lo} > {hi})")
            continue
        em = (lo + hi) / 2
        if sl is None:
            errors.append(f"{sid}: sl missing")
            continue
        # confidence 范围
        c = s.get("confidence")
        if c is None or not isinstance(c, (int, float)) or not (0 <= c <= 100):
            errors.append(f"{sid}: confidence out of range {c!r}")
        # 价位顺序
        for t in s.get("tp", []):
            lv = t.get("level")
            if lv is None:
                errors.append(f"{sid} {t.get('label')}: level missing")
                continue
            if direction == "long" and not (sl < em < lv):
                errors.append(f"{sid}: long order violation sl={sl} entry={em} tp={lv}")
            if direction == "short" and not (lv < em < sl):
                errors.append(f"{sid}: short order violation tp={lv} entry={em} sl={sl}")
            # rr 实数计算
            risk = abs(em - sl)
            if risk > 0 and t.get("rr") is not None:
                calc = round(abs(lv - em) / risk, 2)
                if abs(calc - float(t["rr"])) > 0.011:
                    warnings.append(f"{sid} {t.get('label')}: rr {t['rr']} != computed {calc}")
        # signal_refs（图文一致前提）
        if not s.get("signal_refs"):
            warnings.append(f"{sid}: missing signal_refs (visual-structural consistency unverifiable)")

    return {"ok": not errors, "errors": errors, "warnings": warnings,
            "schema_version": SCHEMA_VERSION}


def assign_result_signal_ids(result) -> None:
    """给 ICTResult 的对象就地打 signal_id（按列表顺序，稳定、幂等）。

    structured_plan 与 plot_ict_clean 共用此函数，保证「图↔信号」同 id。
    """
    for i, ob in enumerate(getattr(result, "order_blocks", None) or [], 1):
        ob.signal_id = f"OB-{i:04d}"
    for i, f in enumerate(getattr(result, "fvgs", None) or [], 1):
        f.signal_id = f"FVG-{i:04d}"
    for i, s in enumerate(getattr(result, "sweeps", None) or [], 1):
        s.signal_id = f"SWP-{i:04d}"
    for i, k in enumerate(getattr(result, "key_levels", None) or [], 1):
        k.signal_id = f"KL-{i:04d}"


def assign_signal_ids(inp: dict) -> dict:
    """给输入特征里的可引用条目分配稳定 signal_id，返回 {signal_id: {type, ...}}。

    signal_id 命名：<类型缩写>-<序号>，如 OB-0001 / FVG-0032 / SWP-0001 / ZS-1H-01。
    序号按出现顺序，稳定（同一输入 → 同一 id）。
    """
    refs: dict[str, dict] = {}
    def add(prefix, typ, payload):
        n = len([k for k in refs if k.startswith(prefix + "-")]) + 1
        sid = f"{prefix}-{n:04d}"
        refs[sid] = {"type": typ, **payload}
        return sid

    ict = inp.get("ict_trader_15m") or {}
    for ob in ict.get("order_blocks") or []:
        add("OB", "order_block", {"direction": ob.get("type"),
                                  "low": ob.get("low"), "high": ob.get("high")})
    for f in ict.get("fvg") or []:
        add("FVG", "fvg", {"direction": f.get("type"),
                           "low": f.get("low"), "high": f.get("high")})
    liq = ict.get("liquidity") or {}
    if liq.get("sweep"):
        add("SWP", "sweep", {"side": liq.get("sweep")})
    for tf in ("1d", "4h", "1h"):
        zs = inp.get("chanlun_htf", {}).get(tf, {}).get("zhongshu") or []
        for z in zs:
            add("ZS", "zhongshu", {"timeframe": tf, "low": z.get("low"),
                                   "high": z.get("high")})
    return refs


# ---------------------------------------------------------------------------
# Input Schema（形式化 + 校验规则）
# ---------------------------------------------------------------------------
INPUT_SCHEMA: dict[str, Any] = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "schema_version": SCHEMA_VERSION,
    "type": "object",
    "required": ["symbol", "current_price", "ict_trader_15m"],
    "properties": {
        "symbol": {"type": "string"},
        "current_price": {"type": "number"},
        "ict_trader_15m": {
            "type": "object",
            "properties": {
                "htf_bias": {"enum": ["bullish", "bearish", "long", "short", "neutral", None]},
                "market_structure": {"enum": ["BOS", "CHoCH", "MSS", None]},
                "liquidity": {"type": "object"},
                "order_blocks": {"type": "array"},
                "fvg": {"type": "array"},
            },
        },
        "wyckoff": {"type": "object"},
        "ichimoku": {"type": "object"},
        "chanlun_htf": {"type": "object"},
    },
}


def validate_input(inp: dict) -> dict:
    """校验输入特征 JSON。返回 {ok, errors[], warnings[]}。

    硬约束（errors）：current_price 缺失/非数值、liquidity 区间倒置。
    软约束（warnings）：htf_bias 取值异常、方向冲突（htf vs structure）。
    """
    errors: list[str] = []
    warnings: list[str] = []
    price = inp.get("current_price")
    if price is None:
        errors.append("current_price missing")
    elif not isinstance(price, (int, float)):
        errors.append(f"current_price not numeric: {price!r}")

    ict = inp.get("ict_trader_15m") or {}
    htf = ict.get("htf_bias")
    if htf not in (None, "bullish", "bearish", "long", "short", "neutral"):
        warnings.append(f"htf_bias unexpected: {htf!r}")

    liq = ict.get("liquidity") or {}
    bsl, ssl = liq.get("nearest_bsl"), liq.get("nearest_ssl")
    if bsl is not None and ssl is not None and float(bsl) <= float(ssl):
        errors.append(f"liquidity inverted: nearest_bsl {bsl} <= nearest_ssl {ssl}")

    # 方向冲突：htf_bias 与 structure_direction 相反 → warning（不是 error）
    sd = ict.get("structure_direction")
    htf_map = {"bullish": "bullish", "long": "bullish",
               "bearish": "bearish", "short": "bearish"}
    if htf in htf_map and sd in ("bullish", "bearish") and htf_map[htf] != sd:
        warnings.append(f"direction conflict: htf_bias {htf} vs structure {sd}")

    return {"ok": not errors, "errors": errors, "warnings": warnings}


# ---------------------------------------------------------------------------
# AI 响应解析 + 失败降级（幂等：同输入同输出）
# ---------------------------------------------------------------------------
def parse_ai_response(text: str, max_retries: int = 2) -> dict:
    """解析 AI 输出 JSON。失败重试（剥离围栏/提取子串），仍失败 → 降级 neutral。

    降级产物是确定性的 wait 计划，绝不编造价格。
    """
    import re

    candidate = text.strip()
    for _ in range(max_retries + 1):
        try:
            obj = json.loads(candidate)
            return obj if isinstance(obj, dict) else {}
        except Exception:
            m = re.search(r"\{.*\}", candidate, re.DOTALL)
            if m:
                try:
                    obj = json.loads(m.group(0))
                    return obj if isinstance(obj, dict) else {}
                except Exception:
                    pass
            # 剥离围栏再试
            candidate = candidate.replace("```json", "").replace("```", "").strip()

    return {"direction": "wait", "entry_zone": None, "sl": None, "tp": [],
            "confidence": 0, "reasoning": ["AI output unparseable after retries"],
            "data_gaps": ["AI JSON parse failed"],
            "prompt_version": SCHEMA_VERSION}


# ---------------------------------------------------------------------------
# 图信号匹配（Visual-Structural Consistency）双向校验
# ---------------------------------------------------------------------------
def validate_visual_consistency(out: dict, chart_elements: list[dict]) -> dict:
    """双向一致性校验。返回 {ok, mismatches[]}。

    chart_elements：渲染层的图元列表，每项必须含 {signal_id, type, level/zone, timeframe}。

    正向：output 每个 setup 的 signal_refs 都能在 chart_elements 找到（缺 → mismatch）。
    反向：chart_elements 每个 signal_id 前缀合法（OB/FVG/SWP/BSL/SSL/DOL/ZS），
          且价格区间一致（容差 0，即图元 level 必须等于结构化 level）。
    """
    mismatches: list[str] = []
    known = {e.get("signal_id") for e in chart_elements}
    for s in list(out.get("long_setups", [])) + list(out.get("short_setups", [])):
        for ref in s.get("signal_refs", []):
            if ref not in known:
                mismatches.append(f"{s['id']}: signal_ref {ref} missing in chart")

    valid_prefix = {"OB", "FVG", "SWP", "KL", "ZS", "BSL", "SSL", "DOL"}
    for e in chart_elements:
        sid = e.get("signal_id")
        if not sid or sid.split("-")[0] not in valid_prefix:
            mismatches.append(f"chart element invalid signal_id: {sid!r}")

    return {"ok": not mismatches, "mismatches": mismatches,
            "schema_version": SCHEMA_VERSION}
