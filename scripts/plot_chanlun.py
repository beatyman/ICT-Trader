#!/usr/bin/env python3
"""缠论绘图：分型/笔/线段/中枢/买卖点 + MACD（基于 vendored chan.py 的 CPlotDriver）。

区间套（多周期）：传 --csv-htf 时画两级，高周期（日线）定方向、低周期（执行周期）看细节。

用法：
  # 单周期
  python scripts/plot_chanlun.py --csv data/eth_15m.csv --symbol ETHUSDT --freq 15m --out chanlun.png
  # 区间套（日线 + 15m）
  python scripts/plot_chanlun.py --csv data/eth_15m.csv --csv-htf data/eth_daily.csv \
      --symbol ETHUSDT --freq 15m --htf-freq 1d --out chanlun.png
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ict_trader.chanlun import _ensure_chanpy, df_to_klus, FREQ_MAP  # noqa: E402

_ensure_chanpy()
from Chan import CChan  # noqa: E402
from ChanConfig import CChanConfig  # noqa: E402
from Common.CEnum import KL_TYPE  # noqa: E402
from Plot.PlotDriver import CPlotDriver  # noqa: E402


def _build_chan(symbol: str, lv_specs: list) -> CChan:
    """lv_specs: [(KL_TYPE, df)]，从高周期到低周期。"""
    lv_list = [spec[0] for spec in lv_specs]
    config = CChanConfig({
        "bi_strict": True, "trigger_step": True, "skip_step": 0,
        "divergence_rate": float("inf"), "bsp2_follow_1": False, "bsp3_follow_1": False,
        "min_zs_cnt": 0, "bs1_peak": False, "macd_algo": "peak",
        "bs_type": "1,2,3a,1p,2s,3b", "print_warning": False, "zs_algo": "normal",
    })
    chan = CChan(code=symbol, data_src=None, lv_list=lv_list, config=config)
    day_like = (KL_TYPE.K_DAY, KL_TYPE.K_WEEK, KL_TYPE.K_MON)
    inp = {}
    for kl_type, df in lv_specs:
        # 日线/周线用 auto=True（00:00 当作当日收盘，区间套对齐），分钟级用 auto=False
        inp[kl_type] = df_to_klus(df, auto=(kl_type in day_like))
    chan.trigger_load(inp)
    for kl_type in lv_list:
        chan.kl_datas[kl_type].cal_seg_and_zs()
    return chan


def main() -> None:
    ap = argparse.ArgumentParser(description="chanlun plot (分型/笔/线段/中枢/买卖点)")
    ap.add_argument("--csv", required=True, help="execution timeframe OHLCV CSV")
    ap.add_argument("--csv-htf", default=None, help="higher timeframe CSV (区间套, optional)")
    ap.add_argument("--symbol", default="SYM")
    ap.add_argument("--freq", default="15m", help="execution timeframe freq (1m/5m/15m/1h/1d)")
    ap.add_argument("--htf-freq", default="1d", help="HTF freq (only with --csv-htf)")
    ap.add_argument("--bars", type=int, default=150, help="x_range (bars to show)")
    ap.add_argument("--rugd", action="store_true", default=False,
                    help="red up / green down (A股/港股)；default = green up (国际/加密)")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    df = pd.read_csv(args.csv, parse_dates=["date"]).sort_values("date").reset_index(drop=True)
    kl_type = getattr(KL_TYPE, FREQ_MAP.get(args.freq, "K_15M"))
    lv_specs = [(kl_type, df)]
    if args.csv_htf:
        df_htf = (pd.read_csv(args.csv_htf, parse_dates=["date"])
                  .sort_values("date").reset_index(drop=True))
        # 对齐时间：HTF 只保留与 LTF 重叠的区间（区间套要求每个 HTF bar 都有次级 bar）
        ltf_start = df["date"].iloc[0]
        ltf_end = df["date"].iloc[-1]
        df_htf = df_htf[(df_htf["date"] >= ltf_start) & (df_htf["date"] <= ltf_end)].reset_index(drop=True)
        htf_type = getattr(KL_TYPE, FREQ_MAP.get(args.htf_freq, "K_DAY"))
        lv_specs = [(htf_type, df_htf), (kl_type, df)]  # 高周期在前

    chan = _build_chan(args.symbol, lv_specs)
    plot_config = {"plot_kline": True, "plot_kline_combine": True, "plot_bi": True,
                   "plot_seg": True, "plot_zs": True, "plot_bsp": True, "plot_macd": True}
    plot_para = {"figure": {"x_range": args.bars},
                 "kl": {"rugd": args.rugd},          # rugd=False: 绿涨红跌
                 "bi": {"show_num": True}}           # 笔编号
    driver = CPlotDriver(chan, plot_config=plot_config, plot_para=plot_para)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    driver.figure.savefig(out, facecolor="white", bbox_inches="tight", dpi=110)
    print(f"saved {out}")


if __name__ == "__main__":
    main()
