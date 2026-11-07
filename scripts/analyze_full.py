#!/usr/bin/env python3
"""多周期结构化分析完整流水线（一键）。

流程：取数据 → 特征提取(ICT主+缠论/Ichimoku/Wyckoff辅) → 结构化计划 → 4张图 → AI提示词。

输出到 --out 目录：
  input.json        输入特征 JSON（给 AI 的数据）
  plan.json         我们引擎的结构化计划 JSON
  ai_prompt.txt     给 AI 的提示词（含 input.json）
  <stem>.1_ict.png / .2_chan_1d.png / .3_chan_4h.png / .4_chan_15m.png

用法（加密自动拉 Binance）：
  python scripts/analyze_full.py --symbol ETHUSDT --market crypto --out results/eth

用法（读 CSV，港股/A股）：
  python scripts/analyze_full.py --symbol 03690.HK --market hk \
      --csv-15m meituan_15m.csv --csv-4h meituan_4h.csv --csv-1h meituan_1h.csv --csv-daily meituan_daily.csv --out results/meituan
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable
sys.path.insert(0, str(ROOT / "src"))

from ict_trader.structured_plan import structured_plan, build_input_from_data  # noqa: E402
from ict_trader.ai_prompt import build_ai_prompt  # noqa: E402


def _fetch(symbol: str, interval: str, limit: int) -> pd.DataFrame:
    from ict_trader.ict_extras import fetch_binance_klines
    df = fetch_binance_klines(symbol, interval, limit)
    if df is None:
        raise RuntimeError(f"failed to fetch {symbol} {interval}")
    return df


def main() -> None:
    ap = argparse.ArgumentParser(description="multi-timeframe structured analysis pipeline")
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--market", default="crypto")
    ap.add_argument("--csv-15m", default=None)
    ap.add_argument("--csv-4h", default=None)
    ap.add_argument("--csv-1h", default=None)
    ap.add_argument("--csv-daily", default=None)
    ap.add_argument("--out", required=True, help="output directory (created if missing)")
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    # 1. 取数据
    if args.csv_15m:
        df_15m = pd.read_csv(args.csv_15m, parse_dates=["date"])
        df_4h = pd.read_csv(args.csv_4h, parse_dates=["date"])
        df_1h = pd.read_csv(args.csv_1h, parse_dates=["date"]) if args.csv_1h else None
        df_daily = pd.read_csv(args.csv_daily, parse_dates=["date"])
    else:
        df_15m = _fetch(args.symbol, "15m", 1000)
        df_4h = _fetch(args.symbol, "4h", 1000)
        df_1h = _fetch(args.symbol, "1h", 1000)
        df_daily = _fetch(args.symbol, "1d", 2000)

    # 2. 特征提取 + 结构化计划 + AI 提示词
    inp = build_input_from_data(df_15m, df_4h, df_daily, args.symbol, args.market, df_1h)
    plan = structured_plan(inp)
    prompt = build_ai_prompt(inp)

    (out / "input.json").write_text(json.dumps(inp, ensure_ascii=False, indent=2), encoding="utf-8")
    (out / "plan.json").write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    (out / "ai_prompt.txt").write_text(prompt, encoding="utf-8")

    # 3. 4 张图（ICT + 1d/4h/15m 缠论）
    ict_cmd = [PY, str(ROOT / "scripts" / "plot_ict_clean.py"),
               "--csv", str(args.csv_15m) if args.csv_15m else "-",
               "--symbol", args.symbol, "--timeframe", "15m",
               "--market", args.market, "--grade", "--show-po3", "--scenarios",
               "--show-fvg", "--show-ob", "--show-sweeps", "--show-structure",
               "--show-eq", "--show-cyq", "--po3-accumulate-bars", "20",
               "--out", str(out / f"{out.stem}.1_ict.png")]
    if args.csv_15m:
        ict_cmd[ict_cmd.index("--csv") + 1] = str(args.csv_15m)
    else:
        # 加密已 fetch，写临时 CSV 再画
        tmp = out / "_tmp_15m.csv"
        df_15m.to_csv(tmp, index=False)
        ict_cmd[ict_cmd.index("--csv") + 1] = str(tmp)

    _run = lambda cmd: subprocess.run(cmd, check=True)
    _run(ict_cmd)
    if not args.csv_15m:
        (out / "_tmp_15m.csv").unlink(missing_ok=True)

    for tf, freq, csv, name in (("1d", "1d", args.csv_daily, "2_chan_1d"),
                                ("4h", "4h", args.csv_4h, "3_chan_4h"),
                                ("15m", "15m", args.csv_15m, "4_chan_15m")):
        if csv is None:
            continue
        _run([PY, str(ROOT / "scripts" / "plot_chanlun.py"),
              "--csv", str(csv), "--symbol", args.symbol, "--freq", freq,
              "--bars", "300" if tf == "1d" else "250" if tf == "4h" else "800",
              "--out", str(out / f"{out.stem}.{name}.png")])

    print(f"done -> {out}")
    print(f"  input.json ({len(json.dumps(inp))} bytes), plan.json, ai_prompt.txt, 4 charts")


if __name__ == "__main__":
    main()
