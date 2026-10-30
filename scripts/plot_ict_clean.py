#!/usr/bin/env python
"""Clean PNG ICT chart with K-lines and a trade plan.

This is the approved *static* renderer.  It uses the same detectors as
``plot_ict_signal.py`` and draws:

  * real candles and volume
  * nearest key levels
  * active FVG / OB boxes
  * recent sweeps / CISD
  * Entry / SL / TP, risk/reward zones, gross and net RR

A plan is drawn by default.  It is either:

  * read from ``--plan-json`` (an ``ict-trader plan`` output), or
  * supplied explicitly (``--direction/--entry/--sl/--tp``), or
  * derived as ``AUTO CONDITIONAL`` from the last CISD / FVG / key level.

No candle, FVG, OB, sweep or price is invented.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd
from matplotlib.lines import Line2D
from matplotlib.patches import Rectangle
from matplotlib.ticker import FuncFormatter

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from ict_trader.concepts import ICTConfig, analyze, load_csv  # noqa: E402
from plot_ict_signal import _auto_plan, _net_rr  # noqa: E402


def _plan_from_json(path: Path) -> dict | None:
    obj = json.loads(path.read_text(encoding="utf-8"))
    if obj.get("decision") != "TRADE" or not obj.get("setup"):
        return None
    s = obj["setup"]
    d = s.get("direction")
    direction = "long" if d in {"bull", "long"} else "short"
    return {
        "direction": direction,
        "entry": float(s.get("entry_reference", s["level"])),
        "sl": float(s["stop"]),
        "tp": float(s["target"]),
        "trigger_time": s.get("signal_time") or obj.get("last_time"),
        "source": "ict-trader plan",
        "conditional": False,
    }


def _resolve_plan(args, view, result) -> dict | None:
    if args.plan_json:
        p = _plan_from_json(Path(args.plan_json))
        if p is not None:
            return p
    if args.direction:
        if args.entry is None or args.sl is None or args.tp is None:
            raise SystemExit("--direction requires --entry --sl --tp")
        if args.trigger_time is None:
            raise SystemExit("--direction requires --trigger-time")
        if args.direction == "long" and not (args.sl < args.entry < args.tp):
            raise SystemExit("long plan requires SL < Entry < TP")
        if args.direction == "short" and not (args.tp < args.entry < args.sl):
            raise SystemExit("short plan requires TP < Entry < SL")
        return {
            "direction": args.direction,
            "entry": float(args.entry),
            "sl": float(args.sl),
            "tp": float(args.tp),
            "trigger_time": args.trigger_time,
            "source": "explicit",
            "conditional": bool(args.conditional),
        }
    atr = float((view["high"] - view["low"]).rolling(14, min_periods=1).mean().iloc[-1])
    return _auto_plan(view, result, atr, min_gross_rr=2.5)


def render(args) -> Path:
    df = load_csv(args.csv)
    if len(df) < 30:
        raise SystemExit("need at least 30 bars")

    # A session window may contain only a few bars.  Analyse a longer history
    # ending at --end, then plot only the requested window.  This keeps the
    # right side of the chart free of pre-window candles without inventing any.
    end = pd.Timestamp(args.end) if args.end else df["date"].iloc[-1]
    hist = df[df["date"] <= end].reset_index(drop=True)
    if len(hist) < 30:
        raise SystemExit("need at least 30 bars up to --end")
    analysis = hist.tail(max(300, args.last_bars)).reset_index(drop=True)
    if args.start:
        start = pd.Timestamp(args.start)
        view = hist[(hist["date"] >= start) & (hist["date"] <= end)].reset_index(drop=True)
    else:
        view = analysis.tail(max(30, args.last_bars)).reset_index(drop=True)
    if view.empty:
        raise SystemExit("no bars in requested window")

    n = len(view)
    last = n - 1
    price = float(view["close"].iloc[-1])
    result = analyze(
        analysis,
        ICTConfig(mode="historical", show_sessions=False, show_htf_cisd=False,
                  cisd_all=True, cisd_min_grade=0, sweep_reclaim_bars=args.sweep_reclaim_bars),
    )
    plan = _resolve_plan(args, view, result)

    def view_index(dt) -> int | None:
        hits = view.index[view["date"] == pd.Timestamp(dt)].tolist()
        return int(hits[0]) if hits else None

    fig = plt.figure(figsize=(18, 10), dpi=180, facecolor="white")
    gs = fig.add_gridspec(2, 1, height_ratios=[4.2, 1.0], hspace=.05,
                          left=.06, right=.80, top=.84, bottom=.07)
    ax = fig.add_subplot(gs[0])
    axv = fig.add_subplot(gs[1], sharex=ax)

    for i, row in view.iterrows():
        up = row["close"] >= row["open"]
        color = "#c62828" if not up else "#1b8a4b"
        ax.vlines(i, row["low"], row["high"], color=color, linewidth=1.0, zorder=3)
        lo, hi = min(row["open"], row["close"]), max(row["open"], row["close"])
        ax.add_patch(Rectangle((i - .34, lo), .68, max(hi - lo, 1e-9),
                               facecolor=color, edgecolor=color, linewidth=.6, zorder=4))
        axv.bar(i, row["volume"], color=color, alpha=.55, width=.72, linewidth=0)

    # ---- key levels -------------------------------------------------
    visible = [
        k for k in result.key_levels
        if k.known_at <= last
        and (k.sweep_bar is None or k.sweep_bar > last)
        and (k.broken_at is None or k.broken_at > last)
    ]
    above = sorted([k for k in visible if k.price > price], key=lambda k: k.price)[:3]
    below = sorted([k for k in visible if k.price < price], key=lambda k: -k.price)[:3]
    handles = [
        Line2D([0], [0], color="#1b8a4b", lw=3, label="K-Line"),
        Rectangle((0, 0), 1, 1, facecolor="#f9a825", alpha=.3, label="FVG / OB"),
        Line2D([0], [0], marker="x", color="#455A64", lw=0, label="Sweep"),
        Line2D([0], [0], color="#1565c0", lw=2, label="SHORT Plan"),
    ]
    for k in above + below:
        color = "#1565c0" if k.kind == "H" else "#c62828"
        ax.axhline(float(k.price), color=color, linestyle=(0, (5, 3)),
                   linewidth=1.1, alpha=.8, zorder=2)
        handles.append(Line2D([0], [0], color=color, lw=1.2, ls=(0, (5, 3)),
                              label=f"{k.name} {k.price:.4g}"))

    # ---- active FVG / OB (mapped onto the plotted window) ----------
    view_end = view["date"].iloc[-1]
    view_start = view["date"].iloc[0]
    y_lo = float(view["low"].min()) - 1.0
    y_hi = float(view["high"].max()) + 1.0
    for f in [f for f in result.fvgs if f.active][-2:]:
        if f.bottom > y_hi or f.top < y_lo:
            continue
        if analysis["date"].iloc[f.created] < view_start:
            continue
        left_date = analysis["date"].iloc[max(0, f.left)]
        right_date = analysis["date"].iloc[min(len(analysis) - 1, f.right)]
        if right_date < view_start or left_date > view_end:
            continue
        x0 = int(view.index[view["date"] >= left_date][0])
        x1 = int(view.index[view["date"] <= right_date][-1])
        if x1 <= x0:
            continue
        ax.add_patch(Rectangle((x0, float(f.bottom)), x1 - x0, float(f.top - f.bottom),
                               facecolor="#f9a825", alpha=.14, edgecolor="#f9a825",
                               linewidth=1.0, linestyle="--", zorder=1))
        ax.text(x0, float(f.top), "FVG", fontsize=8, color="#8d6e00", clip_on=True)
    for ob in [o for o in result.order_blocks if o.active][-2:]:
        if ob.bottom > y_hi or ob.top < y_lo:
            continue
        if analysis["date"].iloc[ob.created] < view_start:
            continue
        left_date = analysis["date"].iloc[max(0, ob.left)]
        right_date = analysis["date"].iloc[min(len(analysis) - 1, ob.right)]
        if right_date < view_start or left_date > view_end:
            continue
        x0 = int(view.index[view["date"] >= left_date][0])
        x1 = int(view.index[view["date"] <= right_date][-1])
        if x1 <= x0:
            continue
        ax.add_patch(Rectangle((x0, float(ob.bottom)), x1 - x0, float(ob.top - ob.bottom),
                               facecolor="#1565c0", alpha=.10, edgecolor="#1565c0",
                               linewidth=1.0, linestyle="--", zorder=1))
        ax.text(x0, float(ob.top), "OB", fontsize=8, color="#0d47a1", clip_on=True)

    # ---- sweeps -----------------------------------------------------
    for s in result.sweeps[-8:]:
        if not (0 <= s.sweep_bar < len(analysis)):
            continue
        j = view_index(analysis["date"].iloc[s.sweep_bar])
        if j is not None:
            col = "#1b8a4b" if s.direction == "bull" else "#c62828"
            ax.scatter([j], [s.sweep_price], marker="x", s=65, color=col,
                       linewidth=1.5, zorder=9)
            ax.annotate(f"Sweep {s.name} {s.sweep_price:.2f}", (j, s.sweep_price),
                        xytext=(6, 10 if s.direction == "bear" else -14),
                        textcoords="offset points", fontsize=7.5, color=col, zorder=10)

    # ---- plan -------------------------------------------------------
    if plan:
        ti = view.index[view["date"] == pd.Timestamp(plan["trigger_time"])].tolist()
        if not ti:
            raise SystemExit(f"trigger_time not found: {plan['trigger_time']}")
        ti = int(ti[0])
        future = 10
        x_end = n - 1 + future
        ax.add_patch(Rectangle((ti, plan["sl"]),
                               x_end - ti, plan["entry"] - plan["sl"],
                               facecolor="#c62828", alpha=.08, linewidth=0, zorder=0))
        ax.add_patch(Rectangle((ti, plan["entry"]),
                               x_end - ti, plan["tp"] - plan["entry"],
                               facecolor="#1b8a4b", alpha=.07, linewidth=0, zorder=0))
        ax.text(ti + (x_end - ti) * .45, (plan["entry"] + plan["sl"]) / 2,
                f"Risk {abs(plan['entry']-plan['sl']):.2f} pts", fontsize=7.5,
                color="#b71c1c", va="center", zorder=11)
        ax.text(ti + (x_end - ti) * .45, (plan["entry"] + plan["tp"]) / 2,
                f"Reward {abs(plan['entry']-plan['tp']):.2f} pts", fontsize=7.5,
                color="#1b5e20", va="center", zorder=11)
        ax.plot([ti, x_end], [plan["entry"], plan["entry"]],
                color="#1565c0", lw=2.2, zorder=8)
        ax.plot([ti, x_end], [plan["sl"], plan["sl"]],
                color="#c62828", lw=1.2, ls=(0, (4, 3)), zorder=7)
        ax.plot([ti, x_end], [plan["tp"], plan["tp"]],
                color="#1b8a4b", lw=1.2, ls=(0, (4, 3)), zorder=7)
        gross, net = _net_rr(plan["direction"], plan["entry"], plan["sl"], plan["tp"],
                             args.commission_bps, args.slippage_bps, args.sell_tax_bps)
        plan_labels = [
            (plan["entry"], f"{plan['direction'].upper()} Entry {plan['entry']:.2f} | conditional", "#1565c0"),
            (plan["sl"], f"{plan['direction'].upper()} SL {plan['sl']:.2f}", "#c62828"),
            (plan["tp"], f"{plan['direction'].upper()} TP {plan['tp']:.2f} | net {net:.2f}R", "#1b8a4b"),
        ]
        label = "CONDITIONAL" if plan.get("conditional") else "PLAN"
        if args.alt_entry and args.alt_direction:
            a_entry, a_sl, a_tp = float(args.alt_entry), float(args.alt_sl), float(args.alt_tp)
            ac = "#8BC34A" if args.alt_direction == "long" else "#EF9A9A"
            ax.plot([ti, x_end], [a_entry, a_entry], color=ac, lw=1.1, ls=":", zorder=7)
            ax.plot([ti, x_end], [a_sl, a_sl], color="#ef9a9a", lw=1.0, ls=":", zorder=6)
            ax.plot([ti, x_end], [a_tp, a_tp], color=ac, lw=1.0, ls=":", zorder=6)
            plan_labels += [
                (a_entry, f"{args.alt_direction.upper()} Entry {a_entry:.2f} | scenario", ac),
                (a_sl, f"{args.alt_direction.upper()} SL {a_sl:.2f}", "#ef9a9a"),
                (a_tp, f"{args.alt_direction.upper()} TP {a_tp:.2f}", ac),
            ]
        # right-gutter labels with leader lines and vertical de-overlap
        ymin0, ymax0 = ax.get_ylim()
        sorted_labels = sorted(plan_labels, key=lambda z: z[0], reverse=True)
        placed, min_gap = [], (ymax0 - ymin0) * .042
        for p0, txt, col in sorted_labels:
            yy = p0
            while any(abs(yy - q) < min_gap for q in placed):
                yy -= min_gap
            placed.append(yy)
            ax.annotate(txt, xy=(x_end, p0), xytext=(x_end + 1.6, yy),
                        textcoords="data", fontsize=9, color=col, va="center", ha="left",
                        arrowprops=dict(arrowstyle="-", color=col, lw=.8, shrinkA=0, shrinkB=2),
                        annotation_clip=False, zorder=12)
        summary = (f"{label} — NOT TRIGGERED\n"
                   f"Source: {plan.get('source','')}\n"
                   f"Entry {plan['entry']:.4g}  SL {plan['sl']:.4g}  TP {plan['tp']:.4g}\n"
                   f"Gross RR {gross:.2f}  Net RR {net:.2f}")
        status = f"{plan['direction'].upper()} {label} | gross RR {gross:.2f} | net RR {net:.2f}"
    else:
        summary = "NO PLAN\nNo confirmed setup and no auto candidate"
        status = "NO TRADE"

    ax.text(.012, .965, summary, transform=ax.transAxes, va="top", ha="left",
            fontsize=8.6, color="#222222",
            bbox=dict(boxstyle="round,pad=.4", fc="white", ec="#9e9e9e", lw=1.0, alpha=.98),
            zorder=20)
    if args.alt_entry:
        handles.append(Line2D([0], [0], color="#8BC34A", lw=1.2, ls=":",
                              label=f"{args.alt_direction.upper()} SCENARIO"))
    if not plan:
        ax.text(.012, .80, "NO TRADE", transform=ax.transAxes, va="top", ha="left",
                fontsize=15, fontweight="bold", color="#b00020", zorder=20)

    if handles:
        ax.legend(handles=handles[:10], loc="lower left", bbox_to_anchor=(0, 1.01),
                  ncol=4, frameon=False, fontsize=7.5)
    # ---- layout -----------------------------------------------------
    extra = [plan["tp"], plan["sl"]] if plan else []
    if args.alt_entry:
        extra += [float(args.alt_tp), float(args.alt_sl)]
    low = min([float(view["low"].min())] + extra)
    high = max([float(view["high"].max())] + extra)
    pad = max((high - low) * .06, 1e-6)
    ax.set_xlim(-1, n + 22)
    ax.set_ylim(low - pad, high + pad)
    ax.set_ylabel("Price", fontsize=10, labelpad=8)
    ax.grid(True, color="#e6e6e6", linewidth=.7)
    ax.set_axisbelow(True)
    ax.tick_params(labelsize=9)
    axv.grid(True, axis="y", color="#e6e6e6", linewidth=.6)
    axv.set_axisbelow(True)
    axv.set_ylabel("Volume", fontsize=9, labelpad=8)
    axv.yaxis.set_major_formatter(FuncFormatter(lambda x, pos: f"{x/1e6:.1f}M" if abs(x) >= 1e6 else f"{x/1e3:.0f}K"))
    axv.tick_params(labelsize=8)
    ticks = list(range(0, n, max(1, n // 8)))
    axv.set_xticks(ticks)
    axv.set_xticklabels([view["date"].iloc[t].strftime("%m-%d %H:%M") for t in ticks],
                        rotation=20, ha="right", fontsize=8)
    fig.text(.06, .955, f"{args.symbol}  {args.timeframe}  ·  ICT structure + trade plan",
             fontsize=16, fontweight="bold", ha="left")
    fig.text(.06, .918,
             f"last bar {view['date'].iloc[-1]}  ·  close {price:.6g}  ·  {status}",
             fontsize=9.8, ha="left", color="#555555")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, facecolor="white", bbox_inches="tight", pad_inches=.25)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--csv", required=True)
    ap.add_argument("--symbol", default="")
    ap.add_argument("--timeframe", default="15m")
    ap.add_argument("--last-bars", type=int, default=96)
    ap.add_argument("--start", default=None,
                    help="plot window start (e.g. '2026-10-29 09:30')")
    ap.add_argument("--end", default=None,
                    help="plot window end / last closed bar")
    ap.add_argument("--sweep-reclaim-bars", type=int, default=3)
    ap.add_argument("--out", default="ict_plan.png")
    ap.add_argument("--plan-json", default=None,
                    help="ict-trader plan JSON; only decision=TRADE is used")
    ap.add_argument("--direction", choices=["long", "short"], default=None)
    ap.add_argument("--entry", type=float, default=None)
    ap.add_argument("--sl", type=float, default=None)
    ap.add_argument("--tp", type=float, default=None)
    ap.add_argument("--trigger-time", default=None)
    ap.add_argument("--alt-direction", choices=["long", "short"], default=None)
    ap.add_argument("--alt-entry", type=float, default=None)
    ap.add_argument("--alt-sl", type=float, default=None)
    ap.add_argument("--alt-tp", type=float, default=None)
    ap.add_argument("--conditional", action="store_true")
    ap.add_argument("--commission-bps", type=float, default=5.0)
    ap.add_argument("--slippage-bps", type=float, default=2.0)
    ap.add_argument("--sell-tax-bps", type=float, default=0.0)
    args = ap.parse_args()
    out = render(args)
    print(f"saved {out} ({out.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
