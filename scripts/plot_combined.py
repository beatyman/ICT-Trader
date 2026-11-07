#!/usr/bin/env python3
"""合并 ICT 主图（上）+ 缠论辅图（下）为一张上下图。

ICT 在上（clean 结构 + 流动性 + 交易计划 + 筹码分布），
缠论在下（分型/笔/线段/中枢/买卖点 + MACD，可加区间套）。

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
    ap = argparse.ArgumentParser(description="ICT (top) + Chanlun (bottom) combined chart")
    ap.add_argument("--csv", required=True, help="execution timeframe OHLCV CSV")
    ap.add_argument("--daily-csv", required=True, help="daily OHLCV CSV (chanlun HTF)")
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--market", default="crypto")
    ap.add_argument("--freq", default="15m")
    ap.add_argument("--bars", type=int, default=40)
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
    chan_png = out.with_name(out.stem + ".chan.png")

    import pandas as pd
    _df = pd.read_csv(args.csv, parse_dates=["date"])
    trigger_time = args.trigger_time or str(_df["date"].iloc[-1])

    # 1. ICT 主图（上）
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

    # 2. 缠论辅图（下）
    chan_cmd = [PY, str(ROOT / "scripts" / "plot_chanlun.py"),
                "--csv", args.csv, "--symbol", args.symbol, "--freq", args.freq,
                "--bars", str(args.bars), "--csv-htf", args.daily_csv,
                "--htf-freq", "1d", "--out", str(chan_png)]
    subprocess.run(chan_cmd, check=True)

    # 3. 上下堆叠
    from PIL import Image
    top = Image.open(ict_png)
    bottom = Image.open(chan_png)
    width = max(top.width, bottom.width)
    top = top.resize((width, int(round(top.height * width / top.width))), Image.LANCZOS)
    bottom = bottom.resize((width, int(round(bottom.height * width / bottom.width))), Image.LANCZOS)
    combined = Image.new("RGB", (width, top.height + bottom.height), "white")
    combined.paste(top, (0, 0))
    combined.paste(bottom, (0, top.height))
    combined.save(out)

    ict_png.unlink(missing_ok=True)
    chan_png.unlink(missing_ok=True)
    print(f"saved {out} ({width}x{combined.height})")


if __name__ == "__main__":
    main()
