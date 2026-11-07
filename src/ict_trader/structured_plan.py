"""多周期结构化交易分析器（ICT 主 + Wyckoff/Ichimoku/缠论 辅）。

把四个方法论的「特征 JSON」融合成结构化多空点位（Entry/TP/SL/失效/置信度）。
决策规则：
  1. HTF 偏向 = 1d/4h/1h 缠论趋势 + Ichimoku 云层 + Wyckoff 阶段（1d 权重最高）。
  2. 多级别共振优先：1d/4h/1h 同向且 15m 出现扫单/OB/FVG 回踩 → 置信度最高。
  3. HTF 与 LTF 冲突 → 降置信度，输出条件单，不强行给高置信度。
  4. Entry 可执行区间/触发条件；SL 在结构失效点之外；TP 指向流动性池/中枢/云层。
  5. 每方向最多 2 个方案：A=顺势共振，B=回调/反转条件单。无则 null。
  6. 不确定用 null，不用「约/附近」当唯一价格。
  7. 输出含 no_trade_conditions + data_gaps。
  8. 置信度只表示结构一致性，不代表胜率。
  9. 输出纯 JSON（可 JSON.parse）。
"""
from __future__ import annotations

from typing import Any

__all__ = ["structured_plan", "build_input_from_data"]


# ---------- helpers ----------

def _get(d: dict, *path: str, default=None) -> Any:
    for p in path:
        if not isinstance(d, dict) or p not in d:
            return default
        d = d[p]
    return d if d is not None else default


def _num(v: Any) -> float | None:
    try:
        if v is None:
            return None
        f = float(v)
        return f if f == f else None  # NaN -> None
    except (TypeError, ValueError):
        return None


def _trend_sign(trend: str | None) -> int:
    return {"up": 1, "down": -1}.get(trend or "", 0)


def _phase_sign(phase: str | None) -> int:
    return {"accumulation": 1, "markup": 1,
            "distribution": -1, "markdown": -1}.get(phase or "", 0)


def _cloud_sign(cloud_bias: str | None) -> int:
    return {"bullish": 1, "bearish": -1}.get(cloud_bias or "", 0)


def _price_vs_cloud_sign(state: str | None) -> int:
    return {"above": 1, "below": -1, "inside": 0}.get(state or "", 0)


def _rr(entry: float | None, sl: float | None, tp: float | None, direction: str) -> float:
    if entry is None or sl is None or tp is None:
        return 0.0
    risk = abs(entry - sl)
    reward = abs(tp - entry)
    if risk <= 0:
        return 0.0
    return round(reward / risk, 2)


# ---------- feature extraction (from engine) ----------

def build_input_from_data(df_15m, df_4h, df_daily, symbol: str, market: str = "crypto",
                          df_1h=None) -> dict:
    """从 OHLCV DataFrame + 引擎算出「输入 JSON」（ICT/Wyckoff/Ichimoku/缠论特征）。"""
    import pandas as pd

    def _df(df):
        return df.sort_values("date").reset_index(drop=True) if df is not None else None

    df_15m, df_4h, df_1h, df_daily = _df(df_15m), _df(df_4h), _df(df_1h), _df(df_daily)
    inp: dict = {
        "symbol": symbol,
        "timestamp": str(df_15m["date"].iloc[-1]) if df_15m is not None and len(df_15m) else None,
        "current_price": float(df_15m["close"].iloc[-1]) if df_15m is not None and len(df_15m) else None,
        "account": {"risk_per_trade_pct": 1, "max_leverage": None},
        "ict_trader_15m": {"htf_bias": None, "market_structure": None,
                           "liquidity": {"bsl": [], "ssl": [], "sweep": None},
                           "order_blocks": [], "fvg": [], "killzone": None,
                           "po3_phase": None},
        "wyckoff": {"phase": None, "events": [], "volume_note": None,
                    "range": {"low": None, "high": None}},
        "ichimoku": {"tenkan": None, "kijun": None, "senkou_a": None, "senkou_b": None,
                     "chikou": None, "cloud_bias": None, "price_vs_cloud": None,
                     "tk_cross": None},
        "chanlun_htf": {"1d": {"trend": None, "zhongshu": [], "buy_sell_point": None, "beichi": None},
                        "4h": {"trend": None, "zhongshu": [], "buy_sell_point": None, "beichi": None},
                        "1h": {"trend": None, "zhongshu": [], "buy_sell_point": None, "beichi": None}},
        "notes": None,
    }

    # ---- ICT 15m ----
    if df_15m is not None and len(df_15m) >= 30:
        try:
            from .concepts import analyze, ICTConfig
            from .po3 import detect_po3
            res = analyze(df_15m, ICTConfig(mode="historical", show_sessions=False,
                                            show_htf_cisd=False, cisd_all=True, cisd_min_grade=0))
            ict = inp["ict_trader_15m"]
            # 结构：最近一个 BOS/CHoCH/MSS（ICTResult.events，非 structure）
            evts = getattr(res, "events", [])
            if evts:
                last = evts[-1]
                ict["market_structure"] = last.kind if hasattr(last, "kind") else None
            # 流动性：BSL/SSL + sweep
            liq = ict["liquidity"]
            liq["bsl"] = [round(k.price, 4) for k in res.key_levels if getattr(k, "name", "") in ("BSL", "EQH")]
            liq["ssl"] = [round(k.price, 4) for k in res.key_levels if getattr(k, "name", "") in ("SSL", "EQL")]
            liq["sweep"] = "ssl" if any(getattr(s, "direction", "") == "bull" for s in res.sweeps) else \
                           "bsl" if any(getattr(s, "direction", "") == "bear" for s in res.sweeps) else None
            # OB + FVG（OrderBlock/FVG 用 bottom/top，非 low/high）
            ict["order_blocks"] = [{"type": "bullish" if ob.direction == "bull" else "bearish",
                                    "low": round(ob.bottom, 4), "high": round(ob.top, 4),
                                    "mitigated": bool(ob.mitigated)}
                                   for ob in getattr(res, "order_blocks", [])[:3]]
            ict["fvg"] = [{"type": "bullish" if f.direction == "bull" else "bearish",
                           "low": round(f.bottom, 4), "high": round(f.top, 4),
                           "filled": bool(getattr(f, "mitigated", False))}
                          for f in getattr(res, "fvgs", [])[:3]]
            # PO3
            win = df_15m[df_15m["date"] >= str(df_15m["date"].iloc[-1])[:10]].reset_index(drop=True)
            if len(win) >= 10:
                po3, _ = detect_po3(win, accumulate_bars=20)
                ict["po3_phase"] = {"ACCUMULATING": "accumulation", "MANIPULATED": "manipulation",
                                    "DISTRIBUTING": "distribution", "INVALID": None}.get(
                                        getattr(po3, "state", ""), None)
        except Exception:
            pass

    # ---- Wyckoff ----
    if df_15m is not None and len(df_15m) >= 30:
        try:
            from .wyckoff import detect_wyckoff
            st, _ = detect_wyckoff(df_15m)
            if st.direction != 0:
                inp["wyckoff"]["phase"] = "accumulation" if st.direction == 1 else "distribution"
                inp["wyckoff"]["range"] = {"low": round(float(st.range_bottom), 4),
                                           "high": round(float(st.range_top), 4)}
        except Exception:
            pass

    # ---- Ichimoku（日线） ----
    if df_daily is not None and len(df_daily) >= 52:
        try:
            from ichimoku.indicators import ichimoku as ichi_ind
            from ichimoku.signals import signals as ichi_sig
            d = df_daily.copy()
            d.index = pd.to_datetime(d["date"])
            ind = ichi_ind(d, 9, 26, 52, "PLATFORM")
            sig = ichi_sig(d, ind, 26)
            ichi = inp["ichimoku"]
            ichi["tenkan"] = round(float(ind["tenkan"].iloc[-1]), 4)
            ichi["kijun"] = round(float(ind["kijun"].iloc[-1]), 4)
            ichi["senkou_a"] = round(float(ind["leading_a"].iloc[-1]), 4)
            ichi["senkou_b"] = round(float(ind["leading_b"].iloc[-1]), 4)
            close = float(d["close"].iloc[-1])
            top = max(ichi["senkou_a"], ichi["senkou_b"])
            bottom = min(ichi["senkou_a"], ichi["senkou_b"])
            ichi["cloud_bias"] = "bullish" if ichi["senkou_a"] > ichi["senkou_b"] else "bearish"
            ichi["price_vs_cloud"] = "above" if close > top else "below" if close < bottom else "inside"
            ichi["tk_cross"] = "bullish" if ichi["tenkan"] > ichi["kijun"] else "bearish"
        except Exception:
            pass

    # ---- 缠论 1d/4h/1h ----
    try:
        from .chanlun import analyze_chanlun
        for tf, df, freq in (("1d", df_daily, "1d"), ("4h", df_4h, "4h"), ("1h", df_1h, "1h")):
            if df is None or len(df) < 30:
                continue
            r = analyze_chanlun(df, symbol, freq)
            cl = inp["chanlun_htf"][tf]
            cl["trend"] = r.last_bi_direction if r.last_bi_direction in ("up", "down") else "range"
            if r.last_zs_zd is not None and r.last_zs_zg is not None:
                cl["zhongshu"] = [{"low": round(r.last_zs_zd, 4), "high": round(r.last_zs_zg, 4)}]
            cl["buy_sell_point"] = r.bsp_type or None
            # 背驰 = 一买/一卖（chan.py 无 divergence 字段，从买卖点派生）
            cl["beichi"] = bool(r.bsp_type and any(t in r.bsp_type for t in ("1买", "1卖")))
    except Exception as e:
        inp["notes"] = f"chanlun error: {type(e).__name__}: {e}"

    return inp


# ---------- decision ----------

def _collect_gaps(inp: dict) -> list[str]:
    gaps: list[str] = []
    if inp.get("current_price") is None:
        gaps.append("current_price missing")
    if _get(inp, "ict_trader_15m", "htf_bias") is None:
        gaps.append("ict_trader_15m.htf_bias missing")
    if _get(inp, "ichimoku", "cloud_bias") is None:
        gaps.append("ichimoku.cloud_bias missing")
    if _get(inp, "wyckoff", "phase") is None:
        gaps.append("wyckoff.phase missing")
    for tf in ("1d", "4h", "1h"):
        if _get(inp, "chanlun_htf", tf, "trend") is None:
            gaps.append(f"chanlun_htf.{tf}.trend missing")
    return gaps


def _htf_bias(inp: dict) -> tuple[str, int, dict]:
    """返回 (analysis_bias, htf_confluence_score, detail)。"""
    score = 0
    detail: dict[str, int] = {}
    # 缠论 1d/4h/1h（1d 权重 2）
    weights = {"1d": 2, "4h": 1, "1h": 1}
    for tf, w in weights.items():
        s = _trend_sign(_get(inp, "chanlun_htf", tf, "trend"))
        detail[f"chanlun_{tf}"] = s * w
        score += s * w
    # Ichimoku
    ic = _cloud_sign(_get(inp, "ichimoku", "cloud_bias")) + _price_vs_cloud_sign(_get(inp, "ichimoku", "price_vs_cloud"))
    detail["ichimoku"] = ic
    score += ic
    # Wyckoff
    wp = _phase_sign(_get(inp, "wyckoff", "phase"))
    detail["wyckoff"] = wp
    score += wp

    # 归一化到 0-100 的 confluence score（用 |score| 表示共振强度，不分多空）
    max_score = sum(weights.values()) + 2 + 1  # 缠论(4) + ichimoku(2) + wyckoff(1) = 7
    confluence = int(round(max(0.0, min(1.0, abs(score) / max_score if max_score else 0.0)) * 100))
    bias = "bullish" if score > 0 else "bearish" if score < 0 else "neutral"
    return bias, confluence, detail


def _liquidity_levels(inp: dict, direction: str) -> list[float]:
    """TP 候选：流动性池 + 中枢边界 + 云层。"""
    liq = _get(inp, "ict_trader_15m", "liquidity") or {}
    if direction == "long":
        lv = [_num(x) for x in (liq.get("bsl") or [])]
        lv += [_get(inp, "chanlun_htf", "1h", "zhongshu", 0, "high")]
        lv += [_get(inp, "ichimoku", "senkou_a"), _get(inp, "ichimoku", "senkou_b")]
    else:
        lv = [_num(x) for x in (liq.get("ssl") or [])]
        lv += [_get(inp, "chanlun_htf", "1h", "zhongshu", 0, "low")]
        lv += [_get(inp, "ichimoku", "senkou_a"), _get(inp, "ichimoku", "senkou_b")]
    price = _num(inp.get("current_price"))
    out = [v for v in lv if v is not None]
    if direction == "long" and price is not None:
        out = sorted((v for v in out if v > price))
    elif direction == "short" and price is not None:
        out = sorted((v for v in out if v < price), reverse=True)
    return out


def _ob_fvg_zone(inp: dict, direction: str) -> tuple[float, float] | None:
    """从 OB/FVG 取入场区间（低-高）。"""
    obs = _get(inp, "ict_trader_15m", "order_blocks") or []
    fvgs = _get(inp, "ict_trader_15m", "fvg") or []
    typ = "bullish" if direction == "long" else "bearish"
    for src in (obs, fvgs):
        for it in src:
            if it.get("type") == typ and not it.get("mitigated", it.get("filled", False)):
                lo, hi = _num(it.get("low")), _num(it.get("high"))
                if lo is not None and hi is not None and lo < hi:
                    return (lo, hi)
    return None


def _build_setup(inp: dict, direction: str, bias: str, kind: str, idx: int,
                 confluence: int) -> dict | None:
    price = _num(inp.get("current_price"))
    if price is None:
        return None
    zone = _ob_fvg_zone(inp, direction)
    liq = _liquidity_levels(inp, direction)

    status = "valid" if bias == direction else "conditional"
    setup_type = "trend_pullback" if kind == "A" else "sweep_reversal"

    if zone:
        entry_low, entry_high = zone
    else:
        # 无 OB/FVG → 用当前价 ± 小缓冲做触发条件
        buf = price * 0.002
        entry_low, entry_high = (price - buf, price + buf) if direction == "long" else (price - buf, price + buf)

    # SL：结构失效点之外（OB/FVG 之外 / sweep 极值之外）
    if zone:
        sl = zone[0] - (zone[1] - zone[0]) * 0.5 if direction == "long" else zone[1] + (zone[1] - zone[0]) * 0.5
    else:
        sl = price * (0.985 if direction == "long" else 1.015)

    # TP：流动性池/中枢/云层（跳过 RR<1 的过近目标）
    tp_vals = [v for v in liq if _rr(price, sl, v, direction) >= 1.0][:3]
    tps = []
    if tp_vals:
        for i, lv in enumerate(tp_vals, 1):
            tps.append({"label": f"TP{i}", "level": round(lv, 4),
                        "rr": _rr(price, sl, lv, direction)})
    if not tps:
        tps = [{"label": "TP1", "level": round(sl + (price - sl) * 2 if direction == "long" else sl - (sl - price) * 2, 4),
                "rr": 2.0}]

    # 置信度：confluence + setup + 共振
    conf = confluence
    if kind == "A" and bias == direction:
        conf += 10  # 顺势共振
    elif kind == "B" and _get(inp, "ict_trader_15m", "liquidity", "sweep") is not None:
        conf += 5   # 扫单反转
    if _get(inp, "ict_trader_15m", "killzone") not in (None, "none"):
        conf += 5
    conf = max(0, min(100, conf))

    inv = (f"close below {round(sl, 4)} invalidates long" if direction == "long"
           else f"close above {round(sl, 4)} invalidates short")
    if kind == "B":
        sweep = _get(inp, "ict_trader_15m", "liquidity", "sweep")
        inv = (f"no MSS after {sweep} sweep, or {inv}" if sweep else f"no confirmed sweep, or {inv}")

    rationale = {
        "ict_15m": [f"market structure: {_get(inp, 'ict_trader_15m', 'market_structure') or 'n/a'}",
                    f"PO3: {_get(inp, 'ict_trader_15m', 'po3_phase') or 'n/a'}",
                    f"killzone: {_get(inp, 'ict_trader_15m', 'killzone') or 'n/a'}"],
        "wyckoff": [f"phase: {_get(inp, 'wyckoff', 'phase') or 'n/a'}"],
        "ichimoku": [f"cloud: {_get(inp, 'ichimoku', 'cloud_bias') or 'n/a'} / "
                     f"price {_get(inp, 'ichimoku', 'price_vs_cloud') or 'n/a'} cloud"],
        "chanlun_htf": [f"1d: {_get(inp, 'chanlun_htf', '1d', 'trend') or 'n/a'}, "
                        f"4h: {_get(inp, 'chanlun_htf', '4h', 'trend') or 'n/a'}, "
                        f"1h: {_get(inp, 'chanlun_htf', '1h', 'trend') or 'n/a'}"],
    }
    return {
        "id": f"{'L' if direction == 'long' else 'S'}{idx}",
        "status": status,
        "setup_type": setup_type,
        "timeframe_confluence": [tf for tf in ("1d", "4h", "1h", "15m")],
        "entry_zone": {"low": round(entry_low, 4), "high": round(entry_high, 4)},
        "entry_trigger": f"close {'above' if direction == 'long' else 'below'} {round(entry_high if direction == 'long' else entry_low, 4)}",
        "sl": round(sl, 4),
        "tp": tps,
        "confidence": conf,
        "invalidation": inv,
        "rationale": rationale,
        "risk_note": "conflict: HTF vs LTF, reduced confidence" if status == "conditional"
                     else "aligned HTF confluence",
    }


def structured_plan(inp: dict) -> dict:
    """输入特征 JSON → 输出结构化多空点位 JSON。"""
    bias, confluence, detail = _htf_bias(inp)
    data_gaps = _collect_gaps(inp)
    price = _num(inp.get("current_price"))

    long_setups, short_setups = [], []
    no_trade: list[str] = []

    if price is None:
        no_trade.append("current_price missing, cannot place entries")
    if bias == "neutral":
        no_trade.append("HTF bias neutral (no dominant direction)")

    # 15m 入场确认：扫单/OB/FVG 是否存在
    sweep = _get(inp, "ict_trader_15m", "liquidity", "sweep")
    has_ob = bool(_get(inp, "ict_trader_15m", "order_blocks"))
    has_fvg = bool(_get(inp, "ict_trader_15m", "fvg"))
    if sweep is None and not has_ob and not has_fvg:
        no_trade.append("15m has no sweep / OB / FVG entry confirmation")

    # 多方案
    if bias in ("bullish", "neutral") and price is not None:
        long_setups.append(_build_setup(inp, "long", bias, "A", 1, confluence))
        if sweep in ("ssl",):
            long_setups.append(_build_setup(inp, "long", bias, "B", 2, confluence))
    if bias in ("bearish", "neutral") and price is not None:
        short_setups.append(_build_setup(inp, "short", bias, "A", 1, confluence))
        if sweep in ("bsl",):
            short_setups.append(_build_setup(inp, "short", bias, "B", 2, confluence))

    long_setups = [s for s in long_setups if s is not None]
    short_setups = [s for s in short_setups if s is not None]

    summary_parts = [f"HTF {bias} (confluence {confluence}/100)"]
    if long_setups:
        summary_parts.append(f"{len(long_setups)} long setup(s)")
    if short_setups:
        summary_parts.append(f"{len(short_setups)} short setup(s)")
    if no_trade:
        summary_parts.append(f"{len(no_trade)} no-trade condition(s)")

    return {
        "meta": {
            "symbol": inp.get("symbol"),
            "timestamp": inp.get("timestamp"),
            "current_price": price,
            "analysis_bias": bias,
            "htf_confluence_score": confluence,
            "summary": "; ".join(summary_parts),
        },
        "long_setups": long_setups,
        "short_setups": short_setups,
        "no_trade_conditions": no_trade,
        "data_gaps": data_gaps,
        "risk_notes": [
            "confidence reflects structural consistency only, not win rate",
            "no profit guarantee; not investment advice",
        ],
    }
