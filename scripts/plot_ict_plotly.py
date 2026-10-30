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
            "conditional": False,
        }
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
    ap.add_argument("--direction", choices=["long", "short"], default=None)
    ap.add_argument("--entry", type=float, default=None)
    ap.add_argument("--sl", type=float, default=None)
    ap.add_argument("--tp", type=float, default=None)
    ap.add_argument("--trigger-time", default=None)
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
        fig.add_hline(y=plan["entry"], line=dict(color="#00bfff", width=2),
                      annotation_text=f"Entry: {plan['entry']:.4g}",
                      annotation_position="top left", row=1, col=1)
        fig.add_hline(y=plan["sl"], line=dict(color="#ff4d4d", width=1.5, dash="dash"),
                      annotation_text=f"SL: {plan['sl']:.4g}",
                      annotation_position="bottom left", row=1, col=1)
        fig.add_hline(y=plan["tp"], line=dict(color="#00e676", width=1.5, dash="dash"),
                      annotation_text=f"TP: {plan['tp']:.4g}",
                      annotation_position="top left", row=1, col=1)
        fig.add_annotation(
            x=0.98, y=0.98, xref="paper", yref="paper",
            text=f"<b>RR: {gross:.2f} | Net RR: {net:.2f}</b>",
            showarrow=False, align="right",
            bgcolor=theme["rr_bg"], font=dict(size=13, color=theme["rr_font"]),
        )
        title_state = (
            f"{plan['direction'].upper()} PLAN | gross RR {gross:.2f} | net RR {net:.2f}"
            if not plan.get("conditional") else
            f"AUTO CONDITIONAL | gross RR {gross:.2f} | net RR {net:.2f}"
        )
    else:
        title_state = "NO TRADE"
        fig.add_annotation(x=0.5, y=0.55, xref="paper", yref="paper",
                           text="NO TRADE", showarrow=False,
                           font=dict(size=40, color=theme["watermark"]))

    fig.update_layout(
        template=theme["template"],
        title=f"{args.symbol}  {args.timeframe}  |  30d  |  last {df['date'].iloc[-1]}  |  {title_state}",
        xaxis_rangeslider_visible=False,
        hovermode="x unified",
        margin=dict(l=40, r=90, t=60, b=30),
        showlegend=False,
        plot_bgcolor=theme["paper"],
        paper_bgcolor=theme["paper"],
        font=dict(color=theme["font"]),
        height=900,
    )
    fig.update_xaxes(showgrid=False, zeroline=False, rangeslider_visible=False,
                     rangebreaks=_rangebreaks(args.market))
    fig.update_yaxes(showgrid=True, gridcolor=theme["grid"],
                     zeroline=False, row=1, col=1)
    fig.update_yaxes(title_text="Volume", showgrid=False, zeroline=False, row=2, col=1)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.write_html(out, include_plotlyjs=True)
    print(f"saved {out} ({out.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
