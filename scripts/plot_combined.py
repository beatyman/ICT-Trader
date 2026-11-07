#!/usr/bin/env python3
"""生成 ICT 主图 + 多周期缠论图（单独 4 张图，不合并）。

每个时间维度各自用长轴（缠论需要长历史才能画出完整笔/线段/中枢/买卖点）：
  1. ICT 主图（15m，短轴）   —— 当前结构 + 流动性 + 交易计划 + 筹码
  2. 日线缠论（长轴）         —— 约 1 年，大级别笔/线段/中枢 + 买卖点
  3. 4h 缠论（长轴）          —— 中级别结构
  4. 15m 缠论（长轴）         —— 交易级结构

输出 4 个独立 PNG（按 <stem>.1_ict / .2_chan_1d / .3_chan_4h / .4_chan_15m 命名）。

用法：
  python scripts/plot_combined.py --csv data/eth_15m.csv --daily-csv data/eth_daily.csv \
      --csv-4h data/eth_4h.csv --symbol ETHUSDT --market crypto --freq 15m \
      --direction short --entry 2238.66 --sl 2251.3 --tp 2214.5 \
      --alt-direction long --alt-entry 2250.74 --alt-sl 2240 --alt-tp 2268 \
      --out eth
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable


def _run(cmd: list) -> None:
    subprocess.run(cmd, check=True)


def main() -> None:
    ap = argparse.ArgumentParser(description="ICT + multi-timeframe chanlun (separate charts)")
    ap.add_argument("--csv", required=True, help="execution timeframe OHLCV CSV (15m)")
    ap.add_argument("--daily-csv", required=True, help="daily OHLCV CSV")
    ap.add_argument("--csv-4h", required=True, help="4h OHLCV CSV")
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--market", default="crypto")
    ap.add_argument("--freq", default="15m")
    ap.add_argument("--bars", type=int, default=40, help="ICT x_range (recent)")
    ap.add_argument("--bars-daily", type=int, default=300, help="daily chanlun x_range")
    ap.add_argument("--bars-4h", type=int, default=250, help="4h chanlun x_range")
    ap.add_argument("--bars-chan", type=int, default=800, help="exec chanlun x_range")
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
    ap.add_argument("--out", required=True, help="output base name (no extension)")
    args = ap.parse_args()

    out_base = Path(args.out)
    out_base.parent.mkdir(parents=True, exist_ok=True)
    stem = out_base.stem
    parent = out_base.parent

    import pandas as pd
    _df = pd.read_csv(args.csv, parse_dates=["date"])
    trigger_time = args.trigger_time or str(_df["date"].iloc[-1])

    outputs = []

    # 1. ICT 主图
    ict_png = parent / f"{stem}.1_ict.png"
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
    _run(ict_cmd)
    outputs.append(ict_png)

    # 2. 日线缠论
    chan_d_png = parent / f"{stem}.2_chan_1d.png"
    _run([PY, str(ROOT / "scripts" / "plot_chanlun.py"),
          "--csv", args.daily_csv, "--symbol", args.symbol, "--freq", "1d",
          "--bars", str(args.bars_daily), "--out", str(chan_d_png)])
    outputs.append(chan_d_png)

    # 3. 4h 缠论
    chan_4h_png = parent / f"{stem}.3_chan_4h.png"
    _run([PY, str(ROOT / "scripts" / "plot_chanlun.py"),
          "--csv", args.csv_4h, "--symbol", args.symbol, "--freq", "4h",
          "--bars", str(args.bars_4h), "--out", str(chan_4h_png)])
    outputs.append(chan_4h_png)

    # 4. 15m 缠论
    chan_e_png = parent / f"{stem}.4_chan_15m.png"
    _run([PY, str(ROOT / "scripts" / "plot_chanlun.py"),
          "--csv", args.csv, "--symbol", args.symbol, "--freq", args.freq,
          "--bars", str(args.bars_chan), "--out", str(chan_e_png)])
    outputs.append(chan_e_png)

    for o in outputs:
        print(f"saved {o}")


if __name__ == "__main__":
    main()
