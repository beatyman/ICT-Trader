#!/usr/bin/env python3
"""合并 ICT 主图（上）+ 日线缠论（中）+ 执行周期缠论（下）为一张三图。

每个时间维度各自用「长轴」展示（缠论需要长历史才能画出完整的笔/线段/中枢）：
  · ICT：短轴（近期结构 + 流动性 + 计划 + 筹码）
  · 日线缠论：长轴（约 1 年，看大级别笔/线段/中枢 + 买卖点）
  · 执行周期缠论：长轴（约 8 天 15m，看交易级笔/线段/中枢 + 买卖点）

用法：
  python scripts/plot_combined.py --csv data/eth_15m.csv --daily-csv data/eth_daily.csv \
      --symbol ETHUSDT --market crypto --freq 15m \
      --direction short --entry 2238.66 --sl 2251.3 --tp 2214.5 \
      --alt-direction long --alt-entry 2250.74 --alt-sl 2240 --alt-tp 2268 \
      --out eth_combined.png
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable


def main() -> None:
    ap = argparse.ArgumentParser(description="ICT (top) + daily chanlun (mid) + exec chanlun (bottom)")
    ap.add_argument("--csv", required=True, help="execution timeframe OHLCV CSV")
    ap.add_argument("--daily-csv", required=True, help="daily OHLCV CSV (daily chanlun)")
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--market", default="crypto")
    ap.add_argument("--freq", default="15m")
    ap.add_argument("--bars", type=int, default=40, help="ICT chart x_range (recent)")
    ap.add_argument("--bars-daily", type=int, default=300, help="daily chanlun x_range (long)")
    ap.add_argument("--bars-chan", type=int, default=800, help="exec chanlun x_range (long)")
    ap.add_argument("--direction", default=None)
    ap.add_argument("--entry", type=float, default=None)
    ap.add_argument("--sl", type=float, default=None)
    ap.add_argument("--tp", type=float, default=None)
    ap.add_argument("--alt-direction", default=None)
    ap.add_argument("--alt-entry", type=float, default=None)
    ap.add_argument("--alt-sl", type=float, default=None)
    ap.add_argument("--alt-tp", type=float, default=None)
    ap.add_argument("--risk-usd", type=float, default=2000)
    ap.add_argument("--trigger-time", default=None, help="default = last bar of --csv")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    ict_png = out.with_name(out.stem + ".ict.png")
    chan_d_png = out.with_name(out.stem + ".chan_d.png")
    chan_e_png = out.with_name(out.stem + ".chan_e.png")

    import pandas as pd
    _df = pd.read_csv(args.csv, parse_dates=["date"])
    trigger_time = args.trigger_time or str(_df["date"].iloc[-1])

    # 1. ICT 主图（上，短轴）
    ict_cmd = [PY, str(ROOT / "scripts" / "plot_ict_clean.py"),
               "--csv", args.csv, "--symbol", args.symbol,
               "--timeframe", args.freq, "--last-bars", str(args.bars),
               "--market", args.market, "--grade", "--risk-usd", str(args.risk_usd),
               "--show-po3", "--scenarios", "--show-fvg", "--show-ob",
               "--show-sweeps", "--show-structure", "--show-eq", "--show-cyq",
               "--po3-accumulate-bars", "20", "--out", str(ict_png)]
    if args.direction and args.entry:
        ict_cmd += ["--direction", args.direction, "--entry", str(args.entry),
                    "--sl", str(args.sl), "--tp", str(args.tp),
                    "--trigger-time", trigger_time, "--conditional"]
    if args.alt_direction and args.alt_entry:
        ict_cmd += ["--alt-direction", args.alt_direction, "--alt-entry", str(args.alt_entry),
                    "--alt-sl", str(args.alt_sl), "--alt-tp", str(args.alt_tp)]
    subprocess.run(ict_cmd, check=True)

    # 2. 日线缠论（中，长轴）
    chan_d_cmd = [PY, str(ROOT / "scripts" / "plot_chanlun.py"),
                  "--csv", args.daily_csv, "--symbol", args.symbol, "--freq", "1d",
                  "--bars", str(args.bars_daily), "--out", str(chan_d_png)]
    subprocess.run(chan_d_cmd, check=True)

    # 3. 执行周期缠论（下，长轴）
    chan_e_cmd = [PY, str(ROOT / "scripts" / "plot_chanlun.py"),
                  "--csv", args.csv, "--symbol", args.symbol, "--freq", args.freq,
                  "--bars", str(args.bars_chan), "--out", str(chan_e_png)]
    subprocess.run(chan_e_cmd, check=True)

    # 4. 三图上下堆叠
    from PIL import Image
    imgs = [Image.open(png) for png in (ict_png, chan_d_png, chan_e_png)]
    width = max(im.width for im in imgs)
    resized = [im.resize((width, int(round(im.height * width / im.width))), Image.LANCZOS)
               for im in imgs]
    total_h = sum(im.height for im in resized)
    combined = Image.new("RGB", (width, total_h), "white")
    y = 0
    for im in resized:
        combined.paste(im, (0, y))
        y += im.height
    combined.save(out)

    for png in (ict_png, chan_d_png, chan_e_png):
        png.unlink(missing_ok=True)
    print(f"saved {out} ({width}x{total_h})")


if __name__ == "__main__":
    main()
