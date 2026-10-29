#!/usr/bin/env python
"""Plot a real-bar ICT chart with optional trade plan.

The script reads a CSV, runs the same ICT detection used by the repository, and
draws only structures that exist in that CSV.  It never invents an FVG, OB,
sweep, CISD or trade level.

Examples
--------
Structure-only chart:

    python scripts/plot_ict_signal.py \
      --csv output/ict/hk03690_15m_2025_2026.csv \
      --symbol HK.03690 --last-bars 300 \
      --out output/ict/hk03690_ict.html

With a confirmed plan:

    python scripts/plot_ict_signal.py \
      --csv data/15m.csv --symbol BTCUSDT \
      --direction long --entry 79933.7 --sl 79666.9 --tp 81344 \
      --trigger-time "2026-10-29 03:00:00" \
      --commission-bps 5 --slippage-bps 2 --sell-tax-bps 0 \
      --out output/ict/btc_plan.html

Requirements:

    pip install pandas plotly

If the repository is not installed, run from the repository root; the script
adds ``src/`` to sys.path automatically.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ict_trader.concepts import ICTConfig, analyze, load_csv  # noqa: E402


def _nearest_levels(levels, price: float, n: int = 3):
    above = sorted([x for x in levels if x.price > price], key=lambda x: x.price)[:n]
    below = sorted([x for x in levels if x.price < price], key=lambda x: -x.price)[:n]
    return above, below


def _net_rr(direction: str, entry: float, sl: float, tp: float, commission_bps: float,
            slippage_bps: float, sell_tax_bps: float) -> tuple[float, float]:
    if direction == "long":
        gross_risk = entry - sl
        gross_reward = tp - entry
        entry_cost = entry * (commission_bps + slippage_bps) / 10_000.0
        exit_cost = tp * (commission_bps + slippage_bps + sell_tax_bps) / 10_000.0
    else:
        gross_risk = sl - entry
        gross_reward = entry - tp
        entry_cost = entry * (commission_bps + slippage_bps) / 10_000.0
        exit_cost = tp * (commission_bps + slippage_bps + sell_tax_bps) / 10_000.0
    if gross_risk <= 0:
        raise ValueError("invalid plan: stop is on the wrong side of entry")
    gross_rr = gross_reward / gross_risk
    net_reward = gross_reward - entry_cost - exit_cost
    net_risk = gross_risk + entry_cost + exit_cost
    net_rr = net_reward / net_risk if net_risk > 0 else float("nan")
    return gross_rr, net_rr


def build_chart(df: pd.DataFrame, args) -> go.Figure:
    if len(df) < 30:
        raise SystemExit("not enough bars: need at least 30")

    view = df.tail(args.last_bars).reset_index(drop=True)
    # Detection uses only bars up to the last row; no future data is injected.
    cfg = ICTConfig(
        mode="historical",
        swing_len=args.swing_len,
        show_sessions=False,
        show_htf_cisd=False,
        cisd_all=True,
        cisd_min_grade=0,
        sweep_reclaim_bars=args.sweep_reclaim_bars,
    )
    result = analyze(view, cfg)
    n = len(view)
    last = n - 1
    price = float(view["close"].iloc[-1])

    fig = go.Figure()
    fig.add_trace(
        go.Candlestick(
            x=view["date"], open=view["open"], high=view["high"],
            low=view["low"], close=view["close"], name="OHLC",
            increasing_line_color="#26a69a", decreasing_line_color="#ef5350",
        )
    )

    # ---- key levels -------------------------------------------------
    visible = [
        k for k in result.key_levels
        if k.known_at <= last
        and (k.sweep_bar is None or k.sweep_bar > last)
        and (k.broken_at is None or k.broken_at > last)
    ]
    above, below = _nearest_levels(visible, price, 3)
    for k in above + below:
        color = "rgba(160,160,160,0.8)"
        fig.add_hline(
            y=float(k.price), line_width=1, line_dash="dot", line_color=color,
            annotation_text=f"{k.name} {k.price:.4g}",
            annotation_position="right", annotation_font_size=10,
        )

    # ---- FVG --------------------------------------------------------
    fvgs = [f for f in result.fvgs if f.active and f.created <= last]
    for f in fvgs[-4:]:
        x0 = view["date"].iloc[max(0, f.left)]
        x1 = view["date"].iloc[min(n - 1, max(f.right, f.left + 1))]
        fig.add_shape(
            type="rect", x0=x0, x1=x1, y0=float(f.bottom), y1=float(f.top),
            fillcolor="rgba(255,235,59,0.16)",
            line=dict(color="rgba(255,235,59,0.7)", width=1),
        )
        fig.add_hline(
            y=float(f.ce), line_width=1, line_dash="solid",
            line_color="rgba(255,235,59,0.8)",
        )
        fig.add_annotation(
            x=x0, y=float(f.top), text=f"FVG CE {f.ce:.4g}",
            showarrow=False, font=dict(size=9, color="#ffeb3b"),
            xanchor="left", yanchor="bottom",
        )

    # ---- order blocks ----------------------------------------------
    obs = [o for o in result.order_blocks if o.active and o.created <= last]
    for o in obs[-3:]:
        x0 = view["date"].iloc[max(0, o.left)]
        x1 = view["date"].iloc[min(n - 1, max(o.right, o.left + 1))]
        fig.add_shape(
            type="rect", x0=x0, x1=x1, y0=float(o.bottom), y1=float(o.top),
            fillcolor="rgba(100,181,246,0.16)",
            line=dict(color="rgba(100,181,246,0.8)", width=1),
        )
        fig.add_hline(
            y=float(o.mean), line_width=1, line_dash="dot",
            line_color="rgba(100,181,246,0.8)",
        )

    # ---- sweeps -----------------------------------------------------
    for s in result.sweeps[-3:]:
        i = min(n - 1, max(0, s.sweep_bar))
        color = "#ef5350" if s.direction == "bear" else "#26a69a"
        fig.add_trace(
            go.Scatter(
                x=[view["date"].iloc[i]], y=[float(s.sweep_price)],
                mode="markers", marker=dict(size=11, color=color, symbol="circle-open"),
                name=f"Sweep {s.name}", hovertext=f"{s.name} sweep {s.level:.4g}",
                showlegend=False,
            )
        )
        fig.add_annotation(
            x=view["date"].iloc[i], y=float(s.sweep_price),
            text=f"Sweep {s.name}", showarrow=True, arrowhead=2,
            ax=0, ay=-28 if s.direction == "bull" else 28,
            font=dict(size=9, color=color),
        )

    # ---- CISD -------------------------------------------------------
    for c in result.cisds[-2:]:
        i = min(n - 1, max(0, c.end))
        fig.add_vline(
            x=view["date"].iloc[i], line_width=1, line_dash="dash",
            line_color="rgba(255,255,255,0.7)",
            annotation_text=f"CISD {c.direction} {c.end}",
            annotation_position="top", annotation_font_size=9,
        )

    # ---- structure events ------------------------------------------
    for e in result.events[-4:]:
        i = min(n - 1, max(0, e.index))
        x0 = view["date"].iloc[max(0, i - 2)]
        x1 = view["date"].iloc[min(n - 1, i + 2)]
        color = "#ef5350" if e.kind == "CHoCH" else "#26a69a"
        if e.kind == "MSS":
            color = "#ff9800"
        fig.add_shape(
            type="line", x0=x0, x1=x1, y0=float(e.price), y1=float(e.price),
            line=dict(color=color, width=1.5, dash="dot"),
        )
        fig.add_annotation(
            x=x1, y=float(e.price), text=e.kind, showarrow=False,
            font=dict(size=9, color=color), xanchor="left",
        )

    # ---- optional trade plan --------------------------------------
    if args.direction:
        if args.entry is None or args.sl is None or args.tp is None:
            raise SystemExit("--direction requires --entry --sl --tp")
        if args.trigger_time is None:
            raise SystemExit("--direction requires --trigger-time")
        if args.direction == "long" and not (args.sl < args.entry < args.tp):
            raise SystemExit("long plan requires SL < Entry < TP")
        if args.direction == "short" and not (args.tp < args.entry < args.sl):
            raise SystemExit("short plan requires TP < Entry < SL")

        trigger = pd.Timestamp(args.trigger_time)
        matches = view.index[view["date"] == trigger].tolist()
        if not matches:
            raise SystemExit(f"trigger_time not found in data: {trigger}")
        ti = int(matches[0])
        x0 = view["date"].iloc[ti]
        x1 = view["date"].iloc[-1]

        fig.add_shape(
            type="rect", x0=x0, x1=x1, y0=args.entry, y1=args.sl,
            fillcolor="rgba(255,0,0,0.12)", line=dict(width=0), layer="below",
        )
        fig.add_shape(
            type="rect", x0=x0, x1=x1, y0=args.entry, y1=args.tp,
            fillcolor="rgba(0,255,0,0.10)", line=dict(width=0), layer="below",
        )
        fig.add_hline(y=args.entry, line_color="#42a5f5", line_width=2,
                      annotation_text=f"Entry {args.entry:g}", annotation_position="right")
        fig.add_hline(y=args.sl, line_color="#ef5350", line_dash="dash",
                      annotation_text=f"SL {args.sl:g}", annotation_position="right")
        fig.add_hline(y=args.tp, line_color="#66bb6a", line_dash="dash",
                      annotation_text=f"TP {args.tp:g}", annotation_position="right")

        gross_rr, net_rr = _net_rr(
            args.direction, args.entry, args.sl, args.tp,
            args.commission_bps, args.slippage_bps, args.sell_tax_bps,
        )
        fig.add_annotation(
            x=x1, y=args.entry,
            text=f"RR: {gross_rr:.2f} | Net RR: {net_rr:.2f}",
            showarrow=True, arrowhead=0, ax=0, ay=-45,
            font=dict(size=12, color="#ffffff"),
            bgcolor="rgba(0,0,0,0.6)",
        )
        status = f"{args.direction.upper()} plan | gross RR {gross_rr:.2f} | net RR {net_rr:.2f}"
    else:
        status = "NO_TRADE — no confirmed setup supplied"
        fig.add_annotation(
            x=view["date"].iloc[len(view) // 2], y=price,
            text="NO TRADE", showarrow=False,
            font=dict(size=34, color="rgba(255,255,255,0.18)"),
        )

    # ---- layout -----------------------------------------------------
    y_min = float(view["low"].min())
    y_max = float(view["high"].max())
    pad = (y_max - y_min) * 0.08 if y_max > y_min else 1.0
    fig.update_layout(
        title=(
            f"{args.symbol}  {args.timeframe}  |  last bar "
            f"{view['date'].iloc[-1]}  |  close {price:g}  |  {status}"
        ),
        template="plotly_dark",
        height=900,
        xaxis_rangeslider_visible=False,
        hovermode="x unified",
        margin=dict(l=40, r=150, t=70, b=40),
        legend=dict(orientation="h", y=1.02, x=0),
    )
    fig.update_yaxes(range=[y_min - pad, y_max + pad])
    fig.update_xaxes(rangebreaks=[dict(bounds=["sat", "mon"])] if args.hide_weekends else [])
    return fig


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--csv", required=True)
    ap.add_argument("--symbol", default="")
    ap.add_argument("--timeframe", default="15m")
    ap.add_argument("--last-bars", type=int, default=300)
    ap.add_argument("--swing-len", type=int, default=3)
    ap.add_argument("--sweep-reclaim-bars", type=int, default=3)
    ap.add_argument("--out", default="ict_chart.html")
    ap.add_argument("--show", action="store_true")
    ap.add_argument("--hide-weekends", action="store_true")
    ap.add_argument("--direction", choices=["long", "short"], default=None)
    ap.add_argument("--entry", type=float, default=None)
    ap.add_argument("--sl", type=float, default=None)
    ap.add_argument("--tp", type=float, default=None)
    ap.add_argument("--trigger-time", default=None)
    ap.add_argument("--commission-bps", type=float, default=5.0)
    ap.add_argument("--slippage-bps", type=float, default=2.0)
    ap.add_argument("--sell-tax-bps", type=float, default=0.0)
    args = ap.parse_args()

    data = load_csv(args.csv)
    if "date" not in data.columns:
        raise SystemExit("CSV must contain a 'date' column")
    if len(data) < 30:
        raise SystemExit("CSV needs at least 30 bars")
    for col in ("open", "high", "low", "close"):
        if col not in data.columns:
            raise SystemExit(f"CSV missing column: {col}")

    fig = build_chart(data, args)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.write_html(out, include_plotlyjs=True)
    print(f"saved {out} ({out.stat().st_size} bytes)")
    if args.show:
        fig.show()


if __name__ == "__main__":
    main()
