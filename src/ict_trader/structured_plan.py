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
        "ict_trader_15m": {
            "htf_bias": None,               # L5-03 方向（1D/4H 对齐）
            "htf_aligned": False,           # 1D/4H 是否同向
            "market_structure": None,       # BOS / CHoCH / MSS
            "structure_direction": None,    # bullish / bearish
            "liquidity": {"bsl": [], "ssl": [], "sweep": None,
                          "nearest_bsl": None, "nearest_ssl": None, "dol": None},
            "order_blocks": [],             # 仅 active（未 mitigated）
            "fvg": [],                      # 仅 active（未 filled）
            "premium_discount": None,       # premium / discount / equilibrium
            "killzone": None,
            "po3_phase": None,
            "po3_range": {"high": None, "low": None},
            "grade": {"grade": None, "hits": 0, "enabled": 0},
        },
        "wyckoff": {"phase": None, "events": [], "volume_note": None,
                    "range": {"low": None, "high": None}},
        "ichimoku": {"tenkan": None, "kijun": None, "senkou_a": None, "senkou_b": None,
                     "chikou": None, "cloud_bias": None, "price_vs_cloud": None,
                     "tk_cross": None},
        "chanlun_htf": {"1d": {"trend": None, "zhongshu": [], "buy_sell_point": None, "beichi": None},
                        "4h": {"trend": None, "zhongshu": [], "buy_sell_point": None, "beichi": None},
                        "1h": {"trend": None, "zhongshu": [], "buy_sell_point": None, "beichi": None}},
        "metadata": {
            "data_availability": {},
            "price_context": {},
            "structure_context": {},
            "volatility_context": {},
        },
        "notes": None,
    }

    # ---- metadata：数据可得性 + 上下文（数据不充分时供 AI 决策） ----
    md = inp["metadata"]
    for tf, df in (("15m", df_15m), ("4h", df_4h), ("1h", df_1h), ("1d", df_daily)):
        if df is not None and len(df):
            md["data_availability"][tf] = {
                "bars": int(len(df)),
                "from": str(df["date"].iloc[0]),
                "to": str(df["date"].iloc[-1]),
            }
        else:
            md["data_availability"][tf] = {"bars": 0, "from": None, "to": None}

    if df_15m is not None and len(df_15m) >= 2:
        c = df_15m["close"].astype(float)
        h = df_15m["high"].astype(float)
        l = df_15m["low"].astype(float)
        tr = pd.DataFrame({
            "hl": h - l,
            "hc": (h - c.shift(1)).abs(),
            "lc": (l - c.shift(1)).abs(),
        }).max(axis=1)
        atr = float(tr.rolling(14, min_periods=1).mean().iloc[-1])
        win20 = 20
        rhi = float(h.tail(win20).max())
        rlo = float(l.tail(win20).min())
        pc = md["price_context"]
        pc["current"] = float(c.iloc[-1])
        pc["recent_20_high"] = round(rhi, 4)
        pc["recent_20_low"] = round(rlo, 4)
        pc["recent_20_range_pct"] = round((rhi - rlo) / rlo * 100, 3) if rlo else None
        vc = md["volatility_context"]
        vc["atr_14"] = round(atr, 4)
        vc["atr_pct"] = round(atr / c.iloc[-1] * 100, 3) if c.iloc[-1] else None
        # 摆动模式（近 20 根 vs 更早 20 根）
        hi_now, lo_now = h.tail(win20).max(), l.tail(win20).min()
        hi_prev, lo_prev = (h.iloc[-2 * win20:-win20].max() if len(h) >= 2 * win20 else h.max(),
                            l.iloc[-2 * win20:-win20].min() if len(l) >= 2 * win20 else l.min())
        if hi_now > hi_prev and lo_now > lo_prev:
            pattern = "higher_highs_higher_lows"
        elif hi_now < hi_prev and lo_now < lo_prev:
            pattern = "lower_highs_lower_lows"
        else:
            pattern = "range"
        md["structure_context"]["swing_pattern"] = pattern

    # ---- ICT 15m ----
    if df_15m is not None and len(df_15m) >= 30:
        try:
            from .concepts import analyze, ICTConfig
            from .po3 import detect_po3
            res = analyze(df_15m, ICTConfig(mode="historical", show_sessions=False,
                                            show_htf_cisd=False, cisd_all=True, cisd_min_grade=0))
            ict = inp["ict_trader_15m"]
            # ---- 用 audit() 取完整 ICT 决策数据（L5-03 方向 / L5-06 溢价折价 / Grade）----
            aud = {}
            try:
                from .rules import audit
                aud = audit(df_15m, daily=df_daily, market=market, res=res)
            except Exception:
                pass
            rules = aud.get("rules", []) if isinstance(aud, dict) else []

            def _rule(rid):
                for r in rules:
                    if r.get("rule_id") == rid:
                        return r
                return None

            # HTF 偏向（L5-03 方向 + 是否 PASS）
            l5_03 = _rule("L5-03")
            if l5_03:
                v = l5_03.get("value")
                if isinstance(v, dict):
                    d = v.get("direction")
                    # 归一化 short/long -> bearish/bullish
                    ict["htf_bias"] = {"short": "bearish", "long": "bullish"}.get(d, d)
                ict["htf_aligned"] = l5_03.get("status") == "PASS"

            # 结构：最近一个 BOS/CHoCH/MSS + 方向（ICTResult.events）
            evts = getattr(res, "events", [])
            if evts:
                last = evts[-1]
                ict["market_structure"] = last.kind if hasattr(last, "kind") else None
                if hasattr(last, "direction"):
                    ict["structure_direction"] = last.direction

            # 流动性：BSL/SSL + sweep + 最近位 + DOL
            liq = ict["liquidity"]
            bsl = sorted(round(k.price, 4) for k in res.key_levels
                         if getattr(k, "name", "") in ("BSL", "EQH"))
            ssl = sorted(round(k.price, 4) for k in res.key_levels
                         if getattr(k, "name", "") in ("SSL", "EQL"))
            liq["bsl"] = bsl
            liq["ssl"] = ssl
            liq["sweep"] = "ssl" if any(getattr(s, "direction", "") == "bull" for s in res.sweeps) else \
                           "bsl" if any(getattr(s, "direction", "") == "bear" for s in res.sweeps) else None
            price = float(df_15m["close"].iloc[-1])
            above = [p for p in bsl if p > price]
            below = [p for p in ssl if p < price]
            liq["nearest_bsl"] = min(above) if above else None
            liq["nearest_ssl"] = max(below) if below else None
            # DOL：最近未扫流动性目标（空→下方 ssl，多→上方 bsl）
            from .sweeps import nearest_key_target
            unswept = [k for k in res.key_levels if not getattr(k, "swept", False)]
            liq["dol"] = round(nearest_key_target(unswept, price, False), 4) if unswept else None

            # OB + FVG：只留 active（未 mitigated/filled）
            ict["order_blocks"] = [{"type": "bullish" if ob.direction == "bull" else "bearish",
                                    "low": round(ob.bottom, 4), "high": round(ob.top, 4),
                                    "mitigated": bool(ob.mitigated)}
                                   for ob in getattr(res, "order_blocks", [])
                                   if not getattr(ob, "mitigated", True)][:5]
            ict["fvg"] = [{"type": "bullish" if f.direction == "bull" else "bearish",
                           "low": round(f.bottom, 4), "high": round(f.top, 4),
                           "filled": bool(getattr(f, "mitigated", False))}
                          for f in getattr(res, "fvgs", [])
                          if not getattr(f, "mitigated", True)][:5]

            # 溢价/折价（L5-06 zone）
            l5_06 = _rule("L5-06")
            if l5_06:
                v = l5_06.get("value")
                if isinstance(v, dict):
                    ict["premium_discount"] = v.get("zone")

            # Grade
            g = aud.get("grade", {}) if isinstance(aud, dict) else {}
            ict["grade"] = {"grade": g.get("grade"), "hits": g.get("hits", 0),
                            "enabled": g.get("enabled", 0)}

            # 结构上下文（事件计数）
            sc = md["structure_context"]
            kinds = [getattr(e, "kind", "") for e in evts]
            sc["bos_count"] = kinds.count("BOS")
            sc["mss_count"] = kinds.count("MSS")
            sc["choch_count"] = sum(1 for k in kinds if k not in ("BOS", "MSS"))
            sc["ob_count"] = len(getattr(res, "order_blocks", []))
            sc["fvg_count"] = len(getattr(res, "fvgs", []))
            sc["sweep_count"] = len(getattr(res, "sweeps", []))
            sc["key_level_count"] = len(getattr(res, "key_levels", []))

            # PO3（阶段 + 区间）
            win = df_15m[df_15m["date"] >= str(df_15m["date"].iloc[-1])[:10]].reset_index(drop=True)
            if len(win) >= 10:
                po3, _ = detect_po3(win, accumulate_bars=20)
                ict["po3_phase"] = {"ACCUMULATING": "accumulation", "MANIPULATED": "manipulation",
                                    "DISTRIBUTING": "distribution", "INVALID": None}.get(
                                        getattr(po3, "state", ""), None)
                ict["po3_range"] = {"high": round(float(po3.range_high), 4),
                                    "low": round(float(po3.range_low), 4)}
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
    """返回 (analysis_bias, htf_confluence_score, detail)。

    ICT 主方向（权重 3），缠论/Ichimoku/Wyckoff 辅助投票。
    ICT 有方向时以 ICT 为准；ICT 无方向时退化为辅助票决定。
    """
    # ICT 主方向
    ict_bias = _get(inp, "ict_trader_15m", "htf_bias")
    ict_sign = {"bullish": 1, "long": 1, "bearish": -1, "short": -1}.get(ict_bias or "", 0)
    detail: dict[str, int] = {"ict_htf": ict_sign * 3}

    score = ict_sign * 3  # ICT 权重 3（主）
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

    max_score = 3 + sum(weights.values()) + 2 + 1  # ICT(3) + 缠论(4) + ichimoku(2) + wyckoff(1) = 10
    confluence = int(round(max(0.0, min(1.0, abs(score) / max_score if max_score else 0.0)) * 100))

    # 方向：ICT 主，ICT 无方向时用辅助票
    if ict_sign != 0:
        bias = "bullish" if ict_sign > 0 else "bearish"
    else:
        aux = score  # ict_sign=0，score 只剩辅助票
        bias = "bullish" if aux > 0 else "bearish" if aux < 0 else "neutral"
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
    liq = _get(inp, "ict_trader_15m", "liquidity") or {}
    nearest_bsl = _num(liq.get("nearest_bsl"))
    nearest_ssl = _num(liq.get("nearest_ssl"))
    dol = _num(liq.get("dol"))
    atr = _num(_get(inp, "metadata", "volatility_context", "atr_14")) or (price * 0.005)
    tp_pool = _liquidity_levels(inp, direction)

    # bias 用 bullish/bearish，direction 用 long/short，须先映射再比较
    bias_dir = {"long": "bullish", "short": "bearish"}[direction]
    status = "valid" if bias == bias_dir else "conditional"
    setup_type = "trend_pullback" if kind == "A" else "sweep_reversal"

    # 入场：A=顺势回踩（sell the rally / buy the dip 到最近流动性），B=扫单反转（追）
    if kind == "A":
        if direction == "short":
            # 反弹到最近 BSL 阻力（sell the rally）
            entry = nearest_bsl if nearest_bsl and nearest_bsl > price else price * 1.005
            entry_low, entry_high = price, entry
            sl = entry + 0.5 * atr
        else:
            # 回踩到最近 SSL 支撑（buy the dip）
            entry = nearest_ssl if nearest_ssl and nearest_ssl < price else price * 0.995
            entry_low, entry_high = entry, price
            sl = entry - 0.5 * atr
    else:
        # 区间半宽不得超过 SL 距离的一半，否则 SL 会落在入场区内（触发即止损）
        buf = min(price * 0.002, 0.25 * atr)
        entry_low, entry_high = price - buf, price + buf
        sl = price - 0.5 * atr if direction == "long" else price + 0.5 * atr

    # TP：DOL / 最近流动性池 / 中枢 / 云层（跳过 RR<1 的过近目标、方向错误的目标）
    entry_mid = (entry_low + entry_high) / 2
    cands = [v for v in tp_pool if _rr(entry_mid, sl, v, direction) >= 1.0]
    if dol is not None:
        dol_on_profit_side = dol > entry_mid if direction == "long" else dol < entry_mid
        if dol_on_profit_side and _rr(entry_mid, sl, dol, direction) >= 1.0:
            cands.insert(0, dol)
    tp_vals = list(dict.fromkeys(cands))[:3]
    tps = []
    for i, lv in enumerate(tp_vals, 1):
        tps.append({"label": f"TP{i}", "level": round(lv, 4),
                    "rr": _rr(entry_mid, sl, lv, direction)})
    if not tps:
        tps = [{"label": "TP1",
                "level": round(sl + (entry_mid - sl) * 2 if direction == "long" else sl - (sl - entry_mid) * 2, 4),
                "rr": 2.0}]

    # 置信度：confluence + setup + 共振
    conf = confluence
    if kind == "A" and bias == bias_dir:
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

    g = _get(inp, "ict_trader_15m", "grade") or {}
    rationale = {
        "ict_15m": [
            f"HTF bias: {_get(inp, 'ict_trader_15m', 'htf_bias') or 'n/a'} "
            f"(aligned={_get(inp, 'ict_trader_15m', 'htf_aligned')})",
            f"structure: {_get(inp, 'ict_trader_15m', 'market_structure') or 'n/a'} "
            f"{_get(inp, 'ict_trader_15m', 'structure_direction') or ''}",
            f"liquidity: sweep={_get(inp, 'ict_trader_15m', 'liquidity', 'sweep') or 'none'}, "
            f"nearest_bsl={nearest_bsl}, nearest_ssl={nearest_ssl}, DOL={dol}",
            f"PD: {_get(inp, 'ict_trader_15m', 'premium_discount') or 'n/a'}, "
            f"PO3: {_get(inp, 'ict_trader_15m', 'po3_phase') or 'n/a'}, "
            f"grade: {g.get('grade') or 'n/a'} ({g.get('hits', 0)}/{g.get('enabled', 0)})",
        ],
        "wyckoff": [f"phase: {_get(inp, 'wyckoff', 'phase') or 'n/a'}"],
        "ichimoku": [f"cloud: {_get(inp, 'ichimoku', 'cloud_bias') or 'n/a'} / "
                     f"price {_get(inp, 'ichimoku', 'price_vs_cloud') or 'n/a'} cloud"],
        "chanlun_htf": [f"1d: {_get(inp, 'chanlun_htf', '1d', 'trend') or 'n/a'}, "
                        f"4h: {_get(inp, 'chanlun_htf', '4h', 'trend') or 'n/a'}, "
                        f"1h: {_get(inp, 'chanlun_htf', '1h', 'trend') or 'n/a'}"],
    }
    if kind == "A":
        trigger = (f"price rallies into {round(entry_low, 4)}-{round(entry_high, 4)} (sell the rally)"
                   if direction == "short"
                   else f"price dips into {round(entry_low, 4)}-{round(entry_high, 4)} (buy the dip)")
    else:
        trigger = f"close {'above' if direction == 'long' else 'below'} {round(entry_high if direction == 'long' else entry_low, 4)}"
    return {
        "id": f"{'L' if direction == 'long' else 'S'}{idx}",
        "status": status,
        "setup_type": setup_type,
        "timeframe_confluence": [tf for tf in ("1d", "4h", "1h", "15m")],
        "entry_zone": {"low": round(entry_low, 4), "high": round(entry_high, 4)},
        "entry_trigger": trigger,
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

    # 风险门：每个 setup 过一遍风控（止损/盈亏比/单笔亏损/当日亏损/次数）
    try:
        from .trade_risk import risk_check
        for s in long_setups + short_setups:
            s["risk_check"] = risk_check(s)
    except Exception:
        pass

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
            "htf_confluence_detail": detail,   # 逐项得分（供 AI 决策）
            "summary": "; ".join(summary_parts),
        },
        "long_setups": long_setups,
        "short_setups": short_setups,
        "no_trade_conditions": no_trade,
        "data_gaps": data_gaps,
        "metadata": inp.get("metadata", {}),   # 数据可得性 + 价格/结构/波动上下文
        "risk_notes": [
            "confidence reflects structural consistency only, not win rate",
            "no profit guarantee; not investment advice",
        ],
    }
