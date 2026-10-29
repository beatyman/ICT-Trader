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
            "conditional": False,
        }
    atr = float((view["high"] - view["low"]).rolling(14, min_periods=1).mean().iloc[-1])
    return _auto_plan(view, result, atr, min_gross_rr=2.5)


def render(args) -> Path:
    df = load_csv(args.csv)
    if len(df) < 30:
        raise SystemExit("need at least 30 bars")
    view = df.tail(max(30, args.last_bars)).reset_index(drop=True)
    n = len(view)
    last = n - 1
    price = float(view["close"].iloc[-1])
    result = analyze(
        view,
        ICTConfig(mode="historical", show_sessions=False, show_htf_cisd=False,
                  cisd_all=True, cisd_min_grade=0, sweep_reclaim_bars=args.sweep_reclaim_bars),
    )
    plan = _resolve_plan(args, view, result)

    fig = plt.figure(figsize=(18, 10), dpi=180, facecolor="white")
    gs = fig.add_gridspec(2, 1, height_ratios=[4.2, 1.0], hspace=.05,
                          left=.06, right=.965, top=.84, bottom=.07)
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
    handles = []
    for k in above + below:
        color = "#1565c0" if k.kind == "H" else "#c62828"
        ax.axhline(float(k.price), color=color, linestyle=(0, (5, 3)),
                   linewidth=1.1, alpha=.8, zorder=2)
        handles.append(Line2D([0], [0], color=color, lw=1.2, ls=(0, (5, 3)),
                              label=f"{k.name} {k.price:.4g}"))

    # ---- active FVG / OB -------------------------------------------
    fvgs = [f for f in result.fvgs if f.active and f.created <= last]
    for f in fvgs[-2:]:
        x0 = view["date"].iloc[max(0, f.left)]
        x1 = view["date"].iloc[min(n - 1, max(f.right, f.left + 1))]
        ax.add_patch(Rectangle((max(0, f.left), float(f.bottom)),
                               max(1, min(n - 1, f.right) - max(0, f.left)),
                               float(f.top - f.bottom),
                               facecolor="#f9a825", alpha=.14, edgecolor="#f9a825",
                               linewidth=1.0, linestyle="--", zorder=1))
        ax.text(max(0, f.left), float(f.top), "FVG", fontsize=8, color="#8d6e00")
    for ob in [o for o in result.order_blocks if o.active and o.created <= last][-2:]:
        ax.add_patch(Rectangle((max(0, ob.left), float(ob.bottom)),
                               max(1, min(n - 1, ob.right) - max(0, ob.left)),
                               float(ob.top - ob.bottom),
                               facecolor="#1565c0", alpha=.10, edgecolor="#1565c0",
                               linewidth=1.0, linestyle="--", zorder=1))
        ax.text(max(0, ob.left), float(ob.top), "OB", fontsize=8, color="#0d47a1")

    # ---- sweeps -----------------------------------------------------
    for s in result.sweeps[-8:]:
        j = s.sweep_bar
        if 0 <= j < n:
            col = "#1b8a4b" if s.direction == "bull" else "#c62828"
            ax.scatter([j], [s.sweep_price], marker="x", s=65, color=col,
                       linewidth=1.5, zorder=9)

    # ---- plan -------------------------------------------------------
    if plan:
        ti = view.index[view["date"] == pd.Timestamp(plan["trigger_time"])].tolist()
        if not ti:
            raise SystemExit(f"trigger_time not found: {plan['trigger_time']}")
        x0 = view["date"].iloc[int(ti[0])]
        x1 = view["date"].iloc[-1]
        ax.add_patch(Rectangle((int(ti[0]), plan["sl"]),
                               n - int(ti[0]), plan["entry"] - plan["sl"],
                               facecolor="#c62828", alpha=.08, linewidth=0, zorder=0))
        ax.add_patch(Rectangle((int(ti[0]), plan["entry"]),
                               n - int(ti[0]), plan["tp"] - plan["entry"],
                               facecolor="#1b8a4b", alpha=.07, linewidth=0, zorder=0))
        ax.plot([int(ti[0]), n - 1], [plan["entry"], plan["entry"]],
                color="#1565c0", lw=2.2, zorder=8)
        ax.plot([int(ti[0]), n - 1], [plan["sl"], plan["sl"]],
                color="#c62828", lw=1.2, ls=(0, (4, 3)), zorder=7)
        ax.plot([int(ti[0]), n - 1], [plan["tp"], plan["tp"]],
                color="#1b8a4b", lw=1.2, ls=(0, (4, 3)), zorder=7)
        ax.text(n - 1 + .4, plan["entry"], f"{plan['direction'].upper()} {plan['entry']:.4g}",
                fontsize=9, color="#1565c0", va="center", ha="left", fontweight="bold")
        ax.text(n - 1 + .4, plan["sl"], f"SL {plan['sl']:.4g}", fontsize=9,
                color="#c62828", va="center", ha="left")
        ax.text(n - 1 + .4, plan["tp"], f"TP {plan['tp']:.4g}", fontsize=9,
                color="#1b8a4b", va="center", ha="left")
        gross, net = _net_rr(plan["direction"], plan["entry"], plan["sl"], plan["tp"],
                             args.commission_bps, args.slippage_bps, args.sell_tax_bps)
        label = "AUTO CONDITIONAL" if plan.get("conditional") else "PLAN"
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
    if not plan:
        ax.text(.012, .80, "NO TRADE", transform=ax.transAxes, va="top", ha="left",
                fontsize=15, fontweight="bold", color="#b00020", zorder=20)

    # ---- layout -----------------------------------------------------
    low = min(float(view["low"].min()), plan["tp"] if plan else 1e18, plan["sl"] if plan else 1e18)
    high = max(float(view["high"].max()), plan["tp"] if plan else -1e18, plan["sl"] if plan else -1e18)
    pad = max((high - low) * .06, 1e-6)
    ax.set_xlim(-1, n + 13)
    ax.set_ylim(low - pad, high + pad)
    ax.set_ylabel("Price", fontsize=10, labelpad=8)
    ax.grid(True, color="#e6e6e6", linewidth=.7)
    ax.set_axisbelow(True)
    ax.tick_params(labelsize=9)
    axv.grid(True, axis="y", color="#e6e6e6", linewidth=.6)
    axv.set_axisbelow(True)
    axv.set_ylabel("Volume", fontsize=9, labelpad=8)
    axv.yaxis.set_major_formatter(FuncFormatter(lambda x, pos: f"{x/1e6:.1f}M"))
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
    ap.add_argument("--sweep-reclaim-bars", type=int, default=3)
    ap.add_argument("--out", default="ict_plan.png")
    ap.add_argument("--plan-json", default=None,
                    help="ict-trader plan JSON; only decision=TRADE is used")
    ap.add_argument("--direction", choices=["long", "short"], default=None)
    ap.add_argument("--entry", type=float, default=None)
    ap.add_argument("--sl", type=float, default=None)
    ap.add_argument("--tp", type=float, default=None)
    ap.add_argument("--trigger-time", default=None)
    ap.add_argument("--commission-bps", type=float, default=5.0)
    ap.add_argument("--slippage-bps", type=float, default=2.0)
    ap.add_argument("--sell-tax-bps", type=float, default=0.0)
    args = ap.parse_args()
    out = render(args)
    print(f"saved {out} ({out.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
