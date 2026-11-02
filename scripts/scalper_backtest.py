"""Walk-forward backtest of the scalper FVG-nesting signal (optimised).

Pre-computes the FVG detection once, then walks forward cheaply.  Compares
baseline (no filter) vs filtered (15M/1H/4H alignment).
"""
from __future__ import annotations

import argparse
import time
from pathlib import Path

import pandas as pd

from ict_trader.concepts import ICTConfig, detect_fvgs
from ict_trader.rules import _htf_scores
from ict_trader.scalper import _dir_of, _net_rr

OUT = Path(__file__).resolve().parents[3] / "output" / "ict"


def _visible(fvgs, idx):
    return [f for f in fvgs if f.created <= idx < f.right]


def _nesting(h_vis, l_vis, price):
    """Return (direction, entry, sl, tp) or None from the FVG nesting."""
    if not h_vis:
        return None
    h = max(h_vis, key=lambda f: f.created)
    if not (float(h.bottom) <= price <= float(h.top)):
        return None
    candidates = [l for l in l_vis
                  if l.direction == h.direction
                  and l.created >= h.created
                  and not (float(l.bottom) > float(h.top) or float(l.top) < float(h.bottom))]
    if not candidates:
        return None
    l = max(candidates, key=lambda f: f.created)
    direction = "long" if l.direction == "bull" else "short"
    entry = float(l.ce)
    if direction == "long":
        sl = float(l.bottom)
        tp = entry + 2 * (entry - sl)
    else:
        sl = float(l.top)
        tp = entry - 2 * (sl - entry)
    return direction, entry, sl, tp


def forward(ltf_df, sig_bar, entry, sl, tp, direction, max_bars=48):
    hi = ltf_df["high"].to_numpy(float)
    lo = ltf_df["low"].to_numpy(float)
    for k in range(sig_bar + 1, min(len(ltf_df), sig_bar + 1 + max_bars)):
        if direction == "long":
            if hi[k] >= tp:
                return "tp"
            if lo[k] <= sl:
                return "sl"
        else:
            if lo[k] <= tp:
                return "tp"
            if hi[k] >= sl:
                return "sl"
    return "open"


def run(symbol: str, step_min: int = 60) -> dict:
    ltf = pd.read_csv(OUT / f"{symbol.lower()}_5m_live.csv", parse_dates=["date"])
    htf = pd.read_csv(OUT / f"{symbol.lower()}_1h_live.csv", parse_dates=["date"])
    cfg = ICTConfig(mode="historical", show_sessions=False, show_htf_cisd=False,
                    cisd_all=False, show_sweeps=False, show_fvg=True, displacement_filter=True)
    h_fvgs = detect_fvgs(htf, cfg)
    l_fvgs = detect_fvgs(ltf, cfg)
    h_times = htf["date"].to_numpy()
    l_times = ltf["date"].to_numpy()
    step = max(1, step_min // 5)
    n = len(ltf)
    res = {"baseline": {"tp": 0, "sl": 0, "open": 0, "signals": []},
           "filtered": {"tp": 0, "sl": 0, "open": 0, "signals": []}}

    for i in range(240, n - 48, step):
        price = float(ltf["close"].iloc[i])
        # 1H bar index at this 5m time
        hj = int(pd.Series(h_times).searchsorted(l_times[i], side="right")) - 1
        if hj < 30:
            continue
        h_vis = _visible(h_fvgs, hj)
        l_vis = _visible(l_fvgs, i)
        nest = _nesting(h_vis, l_vis, price)
        if nest is None:
            continue
        direction, entry, sl, tp = nest
        # filtered: 15M/1H/4H alignment must agree
        aligned = None
        try:
            d15 = _dir_of(_htf_scores(ltf.iloc[: i + 1], "15min"))
            d1 = _dir_of(_htf_scores(htf.iloc[: hj + 1], "1h"))
            d4 = _dir_of(_htf_scores(htf.iloc[: hj + 1], "4h"))
            aligned = d4 if (d4 is not None and d1 == d4 and d15 == d4) else None
        except Exception:
            aligned = None

        base_out = forward(ltf, i, entry, sl, tp, direction)
        res["baseline"][base_out] += 1
        res["baseline"]["signals"].append((str(l_times[i]), direction, base_out))

        if aligned is not None and aligned == direction:
            filt_out = forward(ltf, i, entry, sl, tp, direction)
            res["filtered"][filt_out] += 1
            res["filtered"]["signals"].append((str(l_times[i]), direction, filt_out))
    return res


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--step-min", type=int, default=60)
    args = ap.parse_args()
    t0 = time.time()
    res = run(args.symbol, args.step_min)
    for key in ("baseline", "filtered"):
        r = res[key]
        total = r["tp"] + r["sl"]
        wr = r["tp"] / total if total else 0.0
        print(f"{key:9s}  signals={len(r['signals'])}  tp={r['tp']}  sl={r['sl']}  "
              f"open={r['open']}  win_rate={wr:.2%}")
        for sig in r["signals"][-8:]:
            print(f"    {sig[0]}  {sig[1]:5s}  {sig[2]}")
    print(f"elapsed {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
