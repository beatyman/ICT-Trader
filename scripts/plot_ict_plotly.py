#!/usr/bin/env python
"""Plotly ICT signal chart: 30-day 15m window, price + volume panels.

Contract:
  * real CSV only; no synthetic bars
  * structures come from ict_trader detectors
  * plan comes from --plan-json, explicit args, or AUTO CONDITIONAL
  * no future data is used to place Entry/SL/TP
  * if a structure is absent, it is not drawn
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import timedelta
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from ict_trader.concepts import ICTConfig, analyze, load_csv  # noqa: E402
from plot_ict_signal import _auto_plan, _net_rr  # noqa: E402

UP, DOWN = "#26a69a", "#ef5350"


def _resolve_plan(args, df, result):
    if args.plan_json:
        obj = json.loads(Path(args.plan_json).read_text(encoding="utf-8"))
        if obj.get("decision") == "TRADE" and obj.get("setup"):
            s = obj["setup"]
            d = s.get("direction")
            return {
                "direction": "long" if d in {"bull", "long"} else "short",
                "entry": float(s.get("entry_reference", s["level"])),
                "sl": float(s["stop"]),
                "tp": float(s["target"]),
                "trigger_time": pd.Timestamp(s.get("signal_time") or obj["last_time"]),
                "conditional": False,
            }
    if args.direction:
        if args.entry is None or args.sl is None or args.tp is None or args.trigger_time is None:
            raise SystemExit("--direction requires --entry --sl --tp --trigger-time")
        if args.direction == "long" and not (args.sl < args.entry < args.tp):
            raise SystemExit("long requires SL < Entry < TP")
        if args.direction == "short" and not (args.tp < args.entry < args.sl):
            raise SystemExit("short requires TP < Entry < SL")
        return {
            "direction": args.direction,
            "entry": float(args.entry),
            "sl": float(args.sl),
            "tp": float(args.tp),
            "trigger_time": pd.Timestamp(args.trigger_time),
            "conditional": bool(args.conditional),
            "source": "explicit",
        }
    if getattr(args, "no_auto_plan", False):
        return None
    atr = float((df["high"] - df["low"]).rolling(14, min_periods=1).mean().iloc[-1])
    return _auto_plan(df, result, atr, min_gross_rr=2.5)


def _rangebreaks(market: str):
    if market == "crypto":
        return []
    breaks = [dict(bounds=["sat", "mon"])]
    # US/HK cash sessions are intraday; hide the long overnight block.
    breaks.append(dict(bounds=[16, 9.5], pattern="hour"))
    return breaks


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--csv", required=True)
    ap.add_argument("--symbol", default="")
    ap.add_argument("--timeframe", default="15m")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--market", choices=["crypto", "us", "hk", "other"], default="crypto")
    ap.add_argument("--theme", choices=["light", "dark"], default="light")
    ap.add_argument("--plan-json", default=None)
    ap.add_argument("--audit-json", default=None)
    ap.add_argument("--direction", choices=["long", "short"], default=None)
    ap.add_argument("--entry", type=float, default=None)
    ap.add_argument("--sl", type=float, default=None)
    ap.add_argument("--tp", type=float, default=None)
    ap.add_argument("--no-auto-plan", action="store_true",
                    help="do not synthesize a plan; only draw explicit/scenario levels")
    ap.add_argument("--scenario-tp", action="append", default=[], metavar="LABEL:PRICE:DIR",
                    help="draw a scenario target, e.g. '情景A:2174.08:short'")
    ap.add_argument("--dol", action="append", default=[], metavar="PRICE:LABEL",
                    help="draw a DOL line, e.g. '2174.08:SSL'")
    ap.add_argument("--trigger-time", default=None)
    ap.add_argument("--conditional", action="store_true", help="label an explicit plan as conditional")
    ap.add_argument("--commission-bps", type=float, default=5.0)
    ap.add_argument("--slippage-bps", type=float, default=2.0)
    ap.add_argument("--sell-tax-bps", type=float, default=0.0)
    ap.add_argument("--out", default="ict_signal.html")
    args = ap.parse_args()
    light = args.theme == "light"
    theme = {
        "template": "plotly_white" if light else "plotly_dark",
        "paper": "#ffffff" if light else "#1e1e1e",
        "font": "#222222" if light else "#e0e0e0",
        "muted": "#555555" if light else "#bdbdbd",
        "grid": "rgba(0,0,0,0.06)" if light else "rgba(255,255,255,0.05)",
        "fvg_label": "#8d6e00" if light else "#fdd835",
        "ob_label": "#4a148c" if light else "#ce93d8",
        "sweep": "#c62828" if light else "#ffab91",
        "sweep_bg": "rgba(255,255,255,0.85)" if light else "rgba(0,0,0,0.35)",
        "cisd": "rgba(0,0,0,0.45)" if light else "rgba(255,255,255,0.45)",
        "cisd_label": "#444444" if light else "#e0e0e0",
        "rr_bg": "rgba(255,255,255,0.88)" if light else "rgba(0,0,0,0.55)",
        "rr_font": "#111111" if light else "white",
        "watermark": "rgba(0,0,0,0.10)" if light else "rgba(255,255,255,0.18)",
        "panel_bg": "rgba(255,255,255,0.94)" if light else "rgba(0,0,0,0.78)",
        "panel_border": "rgba(0,0,0,0.18)" if light else "rgba(255,255,255,0.25)",
    }

    df = load_csv(args.csv)
    if "volume" not in df.columns:
        raise SystemExit("CSV must contain volume")
    latest = df["date"].max()
    df = df[df["date"] >= latest - timedelta(days=args.days)].reset_index(drop=True)
    if len(df) < 30:
        raise SystemExit("not enough bars in the requested window")

    result = analyze(
        df,
        ICTConfig(mode="historical", show_sessions=False, show_htf_cisd=False,
                  cisd_all=True, cisd_min_grade=0),
    )
    plan = _resolve_plan(args, df, result)
    n = len(df)
    price = float(df["close"].iloc[-1])

    fig = make_subplots(
        rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.03,
        row_heights=[0.75, 0.25],
    )
    fig.add_trace(
        go.Candlestick(
            x=df["date"], open=df["open"], high=df["high"], low=df["low"], close=df["close"],
            name="K-Line", increasing_line_color=UP, decreasing_line_color=DOWN,
            increasing_fillcolor=UP, decreasing_fillcolor=DOWN,
        ),
        row=1, col=1,
    )
    colors = [UP if c >= o else DOWN for o, c in zip(df["open"], df["close"])]
    fig.add_trace(
        go.Bar(x=df["date"], y=df["volume"], marker_color=colors,
               marker_line_width=0, opacity=0.6, name="Volume"),
        row=2, col=1,
    )

    # ---- key levels -------------------------------------------------
    visible = [
        k for k in result.key_levels
        if k.known_at <= n - 1
        and (k.sweep_bar is None or k.sweep_bar > n - 1)
        and (k.broken_at is None or k.broken_at > n - 1)
    ]
    above = sorted([k for k in visible if k.price > price], key=lambda k: k.price)[:3]
    below = sorted([k for k in visible if k.price < price], key=lambda k: -k.price)[:3]
    for k in above + below:
        fig.add_hline(
            y=float(k.price), line=dict(color="#9e9e9e", width=1, dash="dot"),
            annotation_text=f"{k.name} {k.price:.4g}", annotation_position="right",
            annotation_font=dict(size=10, color=theme["muted"]), row=1, col=1,
        )

    # ---- FVG / OB ---------------------------------------------------
    y_lo, y_hi = float(df["low"].min()), float(df["high"].max())
    for f in [f for f in result.fvgs if f.active][-2:]:
        if f.top < y_lo or f.bottom > y_hi:
            continue
        x0 = df["date"].iloc[max(0, f.left)]
        x1 = df["date"].iloc[min(n - 1, max(f.right, f.left + 1))]
        fig.add_shape(
            type="rect", x0=x0, x1=x1, y0=float(f.bottom), y1=float(f.top),
            fillcolor="rgba(255,235,59,0.15)", line_width=0, row=1, col=1,
        )
        fig.add_shape(
            type="line", x0=x0, x1=x1, y0=float(f.ce), y1=float(f.ce),
            line=dict(color="rgba(255,235,59,0.8)", width=1), row=1, col=1,
        )
        fig.add_annotation(x=x0, y=float(f.top), text="FVG", showarrow=False,
                           font=dict(size=9, color=theme["fvg_label"]), row=1, col=1)
    for ob in [o for o in result.order_blocks if o.active][-2:]:
        if ob.top < y_lo or ob.bottom > y_hi:
            continue
        x0 = df["date"].iloc[max(0, ob.left)]
        x1 = df["date"].iloc[min(n - 1, max(ob.right, ob.left + 1))]
        fig.add_shape(
            type="rect", x0=x0, x1=x1, y0=float(ob.bottom), y1=float(ob.top),
            fillcolor="rgba(138,43,226,0.15)", line_width=0, row=1, col=1,
        )
        fig.add_annotation(x=x0, y=float(ob.top), text="OB", showarrow=False,
                           font=dict(size=9, color=theme["ob_label"]), row=1, col=1)

    # ---- sweeps -----------------------------------------------------
    for s in result.sweeps[-4:]:
        if not (0 <= s.sweep_bar < n):
            continue
        fig.add_annotation(
            x=df["date"].iloc[s.sweep_bar], y=float(s.sweep_price),
            text="Sweep", showarrow=True, arrowhead=2, ax=0, ay=-28,
            font=dict(size=9, color=theme["sweep"]), arrowcolor=theme["sweep"],
            bgcolor=theme["sweep_bg"], row=1, col=1,
        )

    # ---- market structure ------------------------------------------
    for e in result.events[-3:]:
        i = min(n - 1, max(0, e.index))
        x0 = df["date"].iloc[max(0, i - 2)]
        x1 = df["date"].iloc[min(n - 1, i + 2)]
        color = "#ff9800" if e.kind == "MSS" else ("#ef5350" if e.kind == "CHoCH" else "#26a69a")
        fig.add_trace(
            go.Scatter(x=[x0, x1], y=[float(e.price), float(e.price)],
                       mode="lines", line=dict(color=color, width=1.5),
                       hoverinfo="skip", showlegend=False),
            row=1, col=1,
        )
        fig.add_annotation(x=x1, y=float(e.price), text=e.kind, showarrow=False,
                           font=dict(size=9, color=color), row=1, col=1)

    # ---- CISD -------------------------------------------------------
    for c in result.cisds[-1:]:
        if 0 <= c.end < n:
            fig.add_vline(x=df["date"].iloc[c.end], line=dict(color=theme["cisd"],
                          width=1, dash="dash"), row=1, col=1)
            fig.add_annotation(x=df["date"].iloc[c.end], y=float(c.level),
                               text=f"CISD {c.direction}", showarrow=False,
                               font=dict(size=9, color=theme["cisd_label"]), row=1, col=1)

    # ---- plan -------------------------------------------------------
    if plan:
        trigger = pd.Timestamp(plan["trigger_time"])
        if trigger < df["date"].iloc[0] or trigger > df["date"].iloc[-1]:
            raise SystemExit("trigger_time outside plotted window")
        gross, net = _net_rr(plan["direction"], plan["entry"], plan["sl"], plan["tp"],
                             args.commission_bps, args.slippage_bps, args.sell_tax_bps)
        x1 = df["date"].iloc[-1]
        fig.add_shape(type="rect", x0=trigger, x1=x1, y0=plan["sl"], y1=plan["entry"],
                      fillcolor="rgba(255,0,0,0.10)", line_width=0, row=1, col=1)
        fig.add_shape(type="rect", x0=trigger, x1=x1, y0=plan["entry"], y1=plan["tp"],
                      fillcolor="rgba(0,255,0,0.10)", line_width=0, row=1, col=1)
        _tr = pd.concat([df["high"] - df["low"],
                         (df["high"] - df["close"].shift()).abs(),
                         (df["low"] - df["close"].shift()).abs()], axis=1).max(axis=1)
        patr = float(_tr.rolling(14, min_periods=1).mean().iloc[-1]) or 1.0
        xs = [0.40, 0.52, 0.64]
        cur0 = float(df["close"].iloc[-1])
        status = "conditional" if plan.get("conditional") else "triggered"
        fig.add_hline(y=plan["entry"], line=dict(color="#00bfff", width=2), row=1, col=1)
        fig.add_hline(y=plan["sl"], line=dict(color="#ff4d4d", width=1.5, dash="dash"), row=1, col=1)
        fig.add_hline(y=plan["tp"], line=dict(color="#00e676", width=1.5, dash="dash"), row=1, col=1)
        for _i, (txt, yy, col) in enumerate([
            (f"Entry {plan['entry']:,.2f} | {status} | {(plan['entry']-cur0)/patr:+.2f} ATR",
             plan["entry"] - 0.45 * patr if plan["direction"] == "short" else plan["entry"] + 0.45 * patr, "#0077b6"),
            (f"SL {plan['sl']:,.2f} | {(plan['sl']-cur0)/patr:+.2f} ATR",
             plan["sl"] + 0.45 * patr, "#d32f2f"),
            (f"TP {plan['tp']:,.2f} | net {net:.2f}R | {(plan['tp']-cur0)/patr:+.2f} ATR",
             plan["tp"] + 0.45 * patr if plan["direction"] == "short" else plan["tp"] - 0.45 * patr, "#008a5e"),
        ]):
            fig.add_trace(go.Scatter(x=[df["date"].iloc[int(len(df) * xs[_i])]], y=[yy], mode="text", text=[txt],
                                     textposition="middle center", textfont=dict(size=10, color=col),
                                     showlegend=False), row=1, col=1)
        fig.add_annotation(
            x=0.98, y=0.98, xref="paper", yref="paper",
            text=f"<b>RR: {gross:.2f} | Net RR: {net:.2f}</b>",
            showarrow=False, align="right",
            bgcolor=theme["rr_bg"], font=dict(size=13, color=theme["rr_font"]),
        )
        label = "PLAN"
        if plan.get("conditional"):
            label = "CONDITIONAL" if plan.get("source") == "explicit" else "AUTO CONDITIONAL"
        title_state = (
            f"{plan['direction'].upper()} {label} | gross RR {gross:.2f} | net RR {net:.2f}"
        )
    else:
        title_state = "NO TRADE"
        fig.add_annotation(x=0.5, y=0.55, xref="paper", yref="paper",
                           text="NO TRADE", showarrow=False,
                           font=dict(size=40, color=theme["watermark"]))

    # ---- scenario TP / DOL (independent of plan) ---------------------
    def _atr14(frame: pd.DataFrame) -> float:
        tr = pd.concat([frame["high"] - frame["low"],
                        (frame["high"] - frame["close"].shift()).abs(),
                        (frame["low"] - frame["close"].shift()).abs()], axis=1).max(axis=1)
        v = float(tr.rolling(14, min_periods=1).mean().iloc[-1])
        return v if v > 0 else 1.0

    atr = _atr14(df)
    cur = float(df["close"].iloc[-1])
    extra_prices: list[float] = []
    scenario_prices: list[float] = []
    yshift_i = 0
    for spec in args.scenario_tp:
        try:
            label, price_s, direction = spec.split(":", 2)
            price = float(price_s)
        except ValueError:
            raise SystemExit(f"bad --scenario-tp: {spec}")
        color = "#00a86b" if direction == "long" else "#d32f2f"
        dist = (price - cur) / atr
        extra_prices.append(price)
        scenario_prices.append(price)
        fig.add_trace(go.Scatter(x=[df["date"].iloc[0], df["date"].iloc[-1]],
                                 y=[price, price], mode="lines",
                                 line=dict(color=color, width=1.2, dash="dash"),
                                 name=f"TP({label}) {price:,.2f}"), row=1, col=1)
        fig.add_trace(go.Scatter(x=[df["date"].iloc[int(len(df) * 0.62)]], y=[price - .18 * atr], mode="text",
                                 text=[f"TP({label}) {price:,.2f} | {dist:+.2f} ATR"],
                                 textposition="bottom center",
                                 textfont=dict(size=10, color=color), showlegend=False), row=1, col=1)
        yshift_i += 12
    for spec in args.dol:
        try:
            price_s, label = spec.split(":", 1)
            price = float(price_s)
        except ValueError:
            raise SystemExit(f"bad --dol: {spec}")
        dist = (price - cur) / atr
        if any(abs(price - sp) < .01 for sp in scenario_prices):
            continue
        extra_prices.append(price)
        fig.add_trace(go.Scatter(x=[df["date"].iloc[0], df["date"].iloc[-1]],
                                 y=[price, price], mode="lines",
                                 line=dict(color="#0097a7", width=1, dash="dot"),
                                 name=f"DOL({label}) {price:,.2f}"), row=1, col=1)
        fig.add_trace(go.Scatter(x=[df["date"].iloc[int(len(df) * 0.62)]], y=[price - .18 * atr], mode="text",
                                 text=[f"DOL({label}) {price:,.2f} | {dist:+.2f} ATR"],
                                 textposition="bottom center",
                                 textfont=dict(size=10, color="#0097a7"), showlegend=False), row=1, col=1)
        yshift_i += 12
    if extra_prices:
        lo = min([float(df["low"].min())] + extra_prices) - .5 * atr
        hi = max([float(df["high"].max())] + extra_prices) + .5 * atr
        pass  # y-range test

    audit = None
    if args.audit_json:
        audit = json.loads(Path(args.audit_json).read_text(encoding="utf-8"))
    elif args.plan_json:
        _po = json.loads(Path(args.plan_json).read_text(encoding="utf-8"))
        audit = _po.get("rule_audit")

    if audit:
        rs = {r.get("rule_id"): r.get("status") for r in audit.get("rules", [])}
        def st(i):
            return rs.get(i, "N/A")
        lines = [
            f"CONFLUENCE  |  AUDIT: {audit.get('decision','?')}",
            f"Sweep {st('L2-04')}   HTF PDA {st('L2-08')}   Volume {st('L2-09')}   Session {st('L5-01')}",
            f"DOL {st('L2-07')}   SMT {st('L2-10')}   Trigger {st('L3-05')}   RR {st('L4-03')}",
            f"GS: {audit.get('good_setup', {}).get('decision', 'N/A')}   " + "   ".join(audit.get('good_setup', {}).get('failed', []) or []) ,
            f"Blockers: {', '.join(audit.get('blocking_failures', [])) or 'none'}",
        ]
        fig.add_annotation(
            x=0.012, y=0.985, xref="paper", yref="paper",
            text="<br>".join(lines), align="left", showarrow=False,
            bgcolor=theme["panel_bg"], bordercolor=theme["panel_border"],
            borderwidth=1, borderpad=5, font=dict(size=8, color=theme["font"]),
        )

    fig.update_layout(
        template=theme["template"],
        title=f"{args.symbol}  {args.timeframe}  |  30d  |  last {df['date'].iloc[-1]}  |  {title_state}",
        xaxis_rangeslider_visible=False,
        hovermode="x unified",
        margin=dict(l=40, r=240, t=60, b=30),
        showlegend=True, legend=dict(orientation="h", y=-0.08, x=0, font=dict(size=9)),
        plot_bgcolor=theme["paper"],
        paper_bgcolor=theme["paper"],
        font=dict(color=theme["font"]),
        height=900,
    )
    fig.update_xaxes(type="date", showgrid=False, zeroline=False, rangeslider_visible=False,
                     rangebreaks=_rangebreaks(args.market))
    fig.update_xaxes(range=[df["date"].min(), df["date"].max()], row=1, col=1)
    fig.update_xaxes(range=[df["date"].min(), df["date"].max()], row=2, col=1)
    fig.update_yaxes(showgrid=True, gridcolor=theme["grid"],
                     zeroline=False, row=1, col=1)
    fig.update_yaxes(title_text="Volume", showgrid=False, zeroline=False, row=2, col=1)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.write_html(out, include_plotlyjs=True)
    print(f"saved {out} ({out.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
