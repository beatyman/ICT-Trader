#!/usr/bin/env python
"""Schematic ICT + PA teaching map.

This is an explicitly labelled *illustrative* infographic, not market data and
not investment advice.  It does not read a CSV and must not be used as a signal
chart; use scripts/plot_ict_plotly.py for real data.
"""
from __future__ import annotations

from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.patches import FancyArrowPatch, Rectangle
from matplotlib.lines import Line2D

plt.rcParams.update({
    "font.family": "Microsoft YaHei",
    "axes.edgecolor": "#cfd8dc",
    "axes.linewidth": .8,
})

UP, DOWN = "#0f7b4f", "#b3261e"
BLUE, PURPLE, ORANGE, CYAN = "#245b8a", "#6a3d9a", "#c77700", "#00838f"
INK, MUTED = "#17212b", "#60707d"


def _candles(ax, sl, width=.55, alpha=1.0):
    for i, r in sl.iterrows():
        color = UP if r.close >= r.open else DOWN
        ax.vlines(i, r.low, r.high, color=color, linewidth=.8, alpha=alpha, zorder=3)
        ax.add_patch(Rectangle((i - width / 2, min(r.open, r.close)), width,
                               max(abs(r.close - r.open), 8), facecolor=color,
                               edgecolor=color, linewidth=.4, alpha=alpha, zorder=4))


def _label(ax, x, y, text, color=INK, xytext=None, fs=7.2, ha="left"):
    ax.annotate(text, xy=(x, y), xytext=xytext or (x, y + 250), textcoords="data",
                fontsize=fs, color=color, ha=ha, va="center",
                bbox=dict(boxstyle="round,pad=.28", fc="white", ec=color, lw=.7, alpha=.96),
                arrowprops=dict(arrowstyle="-", color=color, lw=.8,
                                shrinkA=2, shrinkB=2), zorder=30)


def main():
    rng = np.random.default_rng(7)
    n = 300
    anchors = [
        (0, 79200), (30, 80000), (60, 80700), (85, 81100), (110, 81950),
        (128, 82450), (145, 82680), (152, 82200), (160, 81350),
        (172, 81700), (188, 82080), (200, 81450), (215, 80700),
        (238, 79700), (262, 78650), (285, 78100), (299, 77900),
    ]
    base = np.interp(np.arange(n), [a[0] for a in anchors], [a[1] for a in anchors])
    close = base + rng.normal(0, 55, n)
    open_ = np.r_[close[0], close[:-1]] + rng.normal(0, 25, n)
    high = np.maximum(open_, close) + np.abs(rng.normal(0, 75, n))
    low = np.minimum(open_, close) - np.abs(rng.normal(0, 75, n))
    # force the narrative candles
    for i, (o, h, l, c) in {
        145: (82580, 82750, 82480, 82540),
        150: (82530, 82560, 81900, 82000),
        155: (81990, 82020, 81300, 81400),
        190: (82060, 82120, 81850, 81900),
    }.items():
        open_[i], high[i], low[i], close[i] = o, h, l, c
    df = __import__("pandas").DataFrame({"open": open_, "high": high, "low": low, "close": close})

    fig = plt.figure(figsize=(20, 11.25), dpi=200, facecolor="#f7f8fa")
    # header
    fig.text(.035, .965, "ICT + PA 交易逻辑图", fontsize=24, weight="bold", color=INK)
    fig.text(.035, .936, "HTF 信号 → 流动性 → 结构 → 入场模型 → Entry / SL / TP → 出场",
             fontsize=10.5, color=MUTED)
    fig.text(.975, .965, "SCHEMATIC · 示意，非投资建议", ha="right", fontsize=9.5,
             color=DOWN, weight="bold")

    ax = fig.add_axes([.035, .30, .625, .60])
    ax.set_facecolor("white")
    _candles(ax, df)
    ax.set_xlim(-3, 332)
    ax.set_ylim(77500, 84000)
    ax.set_ylabel("Price (illustrative)", fontsize=9, color=MUTED)
    ax.tick_params(labelsize=8, colors=MUTED)
    ax.grid(color="#e7ebef", linewidth=.6, zorder=0)
    ax.set_axisbelow(True)
    ax.set_xticks([0, 30, 60, 90, 120, 150, 180, 210, 240, 270, 299])
    ax.set_xticklabels(["D1", "D4", "D7", "D10", "D13", "D16", "D19", "D22", "D25", "D28", "D30"])
    ax.spines[["top", "right"]].set_visible(False)

    # HTF zones
    ax.add_patch(Rectangle((108, 82300), 45, 500, facecolor=PURPLE, alpha=.10,
                           edgecolor=PURPLE, lw=1.0, ls="--", zorder=1))
    ax.add_patch(Rectangle((155, 81850), 50, 300, facecolor="#f2b705", alpha=.13,
                           edgecolor="#b28704", lw=1.0, ls="--", zorder=1))
    ax.text(96, 83010, "4H OB / HTF POI", fontsize=8, color=PURPLE, weight="bold")
    ax.text(156, 81620, "4H FVG", fontsize=8, color="#8d6e00", weight="bold")

    # HTF / liquidity levels
    levels = [
        (83200, "PWH 83,200", "#90a4ae", ":"),
        (82800, "PDH 82,800", "#546e7a", "--"),
        (82300, "Asian High 82,300", CYAN, ":"),
        (81800, "Asian Low 81,800", CYAN, ":"),
        (79500, "PDL 79,500", "#90a4ae", ":"),
    ]
    for y, txt, c, ls in levels:
        ax.axhline(y, color=c, lw=1.0, ls=ls, alpha=.85, zorder=2)
        ax.text(302, y, txt, fontsize=7, color=c, va="center", ha="left", clip_on=False)

    # equal highs / sweep
    ax.plot([120, 145], [82650, 82650], color=CYAN, lw=1.4, ls="--", zorder=5)
    ax.text(122, 82720, "Equal Highs (EQH)", fontsize=7.3, color=CYAN)
    ax.annotate("Liquidity Sweep\n82,750", xy=(145, 82750), xytext=(52, 83520),
                fontsize=7.5, color=DOWN, ha="center",
                bbox=dict(boxstyle="round,pad=.28", fc="white", ec=DOWN, lw=.8),
                arrowprops=dict(arrowstyle="->", color=DOWN, lw=1.0))
    # liquidity void
    ax.add_patch(Rectangle((150, 81400), 10, 500, facecolor="#b0bec5", alpha=.18,
                           edgecolor="none", zorder=1))
    ax.text(151, 81520, "Liquidity Void", fontsize=7, color="#455a64", rotation=90, va="bottom")

    # MSS / BOS
    ax.plot([152, 160], [82520, 81420], color=DOWN, lw=1.2, ls="--", zorder=6)
    _label(ax, 160, 81420, "MSS | 15m | 11:15 | 81,400", DOWN, xytext=(166, 80980), fs=7.2)
    _label(ax, 200, 81450, "BOS | 15m | 13:30 | 81,350", DOWN, xytext=(198, 80620), fs=7.2)
    # HH/HL/LH/LL
    for x, y, txt, c in [(60, 80950, "HH", UP), (85, 80750, "HL", UP),
                         (128, 82580, "HH", UP), (160, 81420, "LH", DOWN),
                         (215, 80700, "LL", DOWN), (262, 78650, "LL", DOWN)]:
        ax.text(x, y, txt, fontsize=8, color=c, weight="bold",
                bbox=dict(boxstyle="circle,pad=.18", fc="white", ec=c, lw=.7))

    # FVG / OB / CE
    ax.add_patch(Rectangle((163, 81700), 30, 300, facecolor="#f2b705", alpha=.18,
                           edgecolor="#b28704", lw=.9, ls="--", zorder=5))
    ax.plot([163,193],[81850,81850], color="#8d6e00", lw=1.0, zorder=6)
    ax.text(164, 81870, "Bearish FVG | CE 50% 81,850", fontsize=7, color="#8d6e00")
    ax.add_patch(Rectangle((150, 82150), 12, 320, facecolor=PURPLE, alpha=.16,
                           edgecolor=PURPLE, lw=.8, ls="--", zorder=5))
    ax.text(138, 82040, "Bearish OB", fontsize=7, color=PURPLE)
    ax.add_patch(Rectangle((145, 82450), 12, 220, facecolor=ORANGE, alpha=.14,
                           edgecolor=ORANGE, lw=.8, ls="--", zorder=5))
    ax.text(133, 82940, "Breaker", fontsize=7, color=ORANGE)

    # killzones / news
    ax.axvspan(112, 140, color="#42a5f5", alpha=.06, zorder=0)
    ax.text(113, 77780, "London Killzone", fontsize=7, color=BLUE)
    ax.axvspan(164, 198, color="#26a69a", alpha=.06, zorder=0)
    ax.text(165, 77780, "NY AM Killzone", fontsize=7, color=UP)
    ax.axvspan(148, 158, color=DOWN, alpha=.07, zorder=0)
    ax.text(149, 82600, "News Filter", fontsize=7, color=DOWN, rotation=90)

    # trade plan
    entry, sl, tp1, tp2, tp3 = 81900, 82700, 80300, 79100, 77900
    x0, x1 = 190, 265
    ax.add_patch(Rectangle((x0, entry), x1 - x0, sl - entry, facecolor=DOWN, alpha=.07, zorder=0))
    ax.add_patch(Rectangle((x0, tp3), x1 - x0, entry - tp3, facecolor=UP, alpha=.06, zorder=0))
    for y, c, ls, txt in [(entry, UP, "-", "Entry 81,900"), (sl, DOWN, "-", "SL 82,700"),
                          (tp1, BLUE, "--", "TP1 80,300"), (tp2, BLUE, "--", "TP2 79,100"),
                          (tp3, BLUE, "--", "TP3 77,900")]:
        ax.plot([x0, x1], [y, y], color=c, lw=1.8 if y == entry else 1.2, ls=ls, zorder=8)
        ax.text(x1 + 2, y, txt, fontsize=7.2, color=c, va="center", weight="bold" if y == entry else "normal")
    ax.text(x1 + 2, 80900, "RR 1:2 / 1:3.5 / 1:5", fontsize=7.2, color=INK)
    ax.text(x1 + 2, 82380, "Invalidation 82,700", fontsize=7, color=DOWN)
    ax.annotate("Entry trigger\nFVG/OB retest + CISD", xy=(190, entry), xytext=(205, 83040),
                fontsize=7.2, color=UP, ha="left",
                bbox=dict(boxstyle="round,pad=.28", fc="white", ec=UP, lw=.8),
                arrowprops=dict(arrowstyle="->", color=UP, lw=1.0))
    # trailing stop path
    ax.plot([190, 210, 235, 260], [82700, 82200, 80900, 79500], color=ORANGE, lw=1.3,
            ls=(0, (4, 3)), marker="o", ms=3, zorder=9)
    ax.text(236, 81220, "Trailing SL path", fontsize=7, color=ORANGE)

    # HTF -> LTF resonance
    ax.annotate("HTF → LTF 共振", xy=(190, 81900), xytext=(66, 83880),
                fontsize=7.4, color=PURPLE, ha="center",
                bbox=dict(boxstyle="round,pad=.3", fc="white", ec=PURPLE, lw=.8),
                arrowprops=dict(arrowstyle="->", color=PURPLE, lw=1.1,
                                connectionstyle="arc3,rad=-.18"))

    # volume strip
    axv = fig.add_axes([.035, .235, .625, .055], sharex=ax)
    vol = 3500 + 2500 * np.abs(np.diff(np.r_[close[0], close])) / 100
    vol[145] = 9000
    vol[150:160] = 7000
    axv.bar(range(n), vol, color=[UP if c >= o else DOWN for o, c in zip(open_, close)],
            width=.7, alpha=.55, linewidth=0)
    axv.set_ylim(0, 11000)
    axv.set_ylabel("Volume", fontsize=7, color=MUTED)
    axv.tick_params(labelsize=7, colors=MUTED)
    axv.set_yticks([])
    axv.spines[["top", "right", "left"]].set_visible(False)
    axv.grid(False)

    # ---------- sidebar ----------
    sx = .68
    fig.text(sx, .905, "HTF BIAS", fontsize=9, weight="bold", color=INK)
    fig.text(sx, .883, "D1  BEARISH  ·  4H  BEARISH", fontsize=10.5, weight="bold", color=DOWN)
    fig.text(sx, .858,
             "D1 close below cloud · 4H OB rejection\n"
             "PWH swept · DOL = PDL 79,500",
             fontsize=7.7, color=MUTED, linespacing=1.35)

    # insets
    insets = [
        (.68, .655, .285, .145, "① Liquidity Sweep", (128, 155), "Equal Highs / PDH swept"),
        (.68, .500, .285, .145, "② MSS + Displacement", (148, 175), "Bearish MSS · 81,400"),
        (.68, .345, .285, .145, "③ FVG / OB Entry", (180, 205), "CE 81,850 · Entry 81,900"),
    ]
    for i, (x, y, w, h, title, (a, b), sub) in enumerate(insets):
        ia = fig.add_axes([x, y, w, h])
        sl = df.iloc[a:b].reset_index(drop=True)
        _candles(ia, sl, width=.5)
        ia.set_xlim(-2, len(sl) + 2)
        lo, hi = sl.low.min(), sl.high.max()
        pad = (hi - lo) * .18 if hi > lo else 100
        ia.set_ylim(lo - pad, hi + pad)
        ia.set_xticks([]); ia.set_yticks([])
        ia.set_facecolor("#fbfcfd")
        ia.set_title(title, fontsize=8, loc="left", color=INK, pad=2)
        ia.text(.02, .88, sub, transform=ia.transAxes, fontsize=6.5, color=MUTED)
        ia.spines[["top", "right"]].set_visible(False)
        ia.grid(color="#eceff1", linewidth=.4)
        if i == 0:
            ia.scatter([len(sl) - 8], [sl.high.max()], marker="x", s=30, color=DOWN, zorder=10)
        if i == 1:
            ia.axhline(sl.low.min() + 40, color=DOWN, lw=.8, ls="--")
        if i == 2:
            ia.axhspan(sl.low.min() + (hi - lo) * .35, sl.low.min() + (hi - lo) * .55,
                       color="#f2b705", alpha=.18)

    # legend / playbook
    fig.text(sx, .305, "SIGNAL LEGEND / PLAYBOOK", fontsize=8.5, weight="bold", color=INK)
    legend = [
        ("FVG / CE 50%", "#f2b705"), ("Order Block", PURPLE),
        ("Breaker", ORANGE), ("Liquidity pool", CYAN),
        ("Liquidity void", "#b0bec5"), ("Entry / SL / TP", UP),
    ]
    yy = .282
    for txt, c in legend:
        fig.add_artist(Rectangle((sx, yy - .006), .012, .012, transform=fig.transFigure,
                                 facecolor=c, alpha=.55, edgecolor="none"))
        fig.text(sx + .018, yy, txt, fontsize=7.2, color=MUTED)
        yy -= .026
    fig.text(sx, .095,
             "Labels: HTF Bias · D1 OB · 4H FVG · PDH/PDL · PWH/PWL\n"
             "Asian Range · EQH/EQL · Sweep · Void · BOS/MSS · HH/HL/LH/LL\n"
             "Bullish/Bearish OB · Breaker · FVG · CE · Displacement · OTE\n"
             "Judas Swing · Turtle Soup · Silver Bullet · Killzones · News\n"
             "Entry · SL · TP1/2/3 · RR · Invalidation · Trailing SL",
             fontsize=6.1, color=MUTED, linespacing=1.3)

    fig.text(.035, .045,
             "SCHEMATIC TEACHING MAP. All prices, times and RR are illustrative. "
             "Not market data, not a signal, not investment advice.",
             fontsize=8, color=DOWN)
    fig.text(.035, .022,
             "Use scripts/plot_ict_plotly.py for real-data ICT charts; this file is only a logic diagram.",
             fontsize=7.2, color=MUTED)

    out = Path("output/ict/ict_pa_logic_map.png")
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, facecolor=fig.get_facecolor(), dpi=200)
    print(f"saved {out} ({out.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
