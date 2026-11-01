"""Trailing Reversal Trading System (MarkitTick) — Python port + backtest.

Faithful port of the Pine Script v6 indicator "Trailing Reversal Trading
System [MarkitTick]".  It is a swing-reversal strategy:

    swing flips when price moves `rev_pct`% beyond the last pivot extreme
    entry at the flip bar close
    stop = ATR * sl_atr, capped at the pivot the swing reversed from
    TP1/TP2/TP3 = risk * R1/R2/R3
    exit on TP3, stop, or an opposite-swing reversal

Standalone: imports only pandas/numpy, does not touch the ICT engine.

Usage:

    python scripts/trailing_reversal_backtest.py --csv data/15m.csv
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field

import numpy as np
import pandas as pd


# --------------------------------------------------------------------- #
# indicators (Wilder's RMA semantics used by Pine ta.*)
# --------------------------------------------------------------------- #
def rma(s: pd.Series, length: int) -> pd.Series:
    """Wilder's RMA == EMA with alpha = 1/length (Pine ta.rma)."""
    return s.ewm(alpha=1.0 / length, adjust=False).mean()


def true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [df["high"] - df["low"],
         (df["high"] - prev_close).abs(),
         (df["low"] - prev_close).abs()], axis=1
    ).max(axis=1)
    return tr


def atr(df: pd.DataFrame, length: int = 14) -> pd.Series:
    return rma(true_range(df), length)


def dmi_adx(df: pd.DataFrame, length: int = 14):
    """Pine ta.dmi(diLength, adxLength): returns (DI+, DI-, ADX)."""
    up = df["high"].diff()
    dn = -df["low"].diff()
    plus_dm = pd.Series(np.where((up > dn) & (up > 0), up, 0.0), index=df.index)
    minus_dm = pd.Series(np.where((dn > up) & (dn > 0), dn, 0.0), index=df.index)
    atr_ = rma(true_range(df), length)
    plus_di = 100.0 * rma(plus_dm, length) / atr_
    minus_di = 100.0 * rma(minus_dm, length) / atr_
    dx = 100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    adx_ = rma(dx, length)
    return plus_di, minus_di, adx_


def smooth(s: pd.Series, length: int, method: str) -> pd.Series:
    if method == "SMA":
        return s.rolling(length).mean()
    if method == "EMA":
        return s.ewm(span=length, adjust=False).mean()
    if method == "RMA":
        return rma(s, length)
    if method == "WMA":
        w = np.arange(1, length + 1, dtype=float)
        return s.rolling(length).apply(lambda x: np.dot(x, w) / w.sum(), raw=True)
    if method == "HMA":
        half = length // 2
        wma_half = s.rolling(half).apply(
            lambda x: np.dot(x, np.arange(1, half + 1)) / np.arange(1, half + 1).sum(),
            raw=True)
        wma_full = s.rolling(length).apply(
            lambda x: np.dot(x, np.arange(1, length + 1)) / np.arange(1, length + 1).sum(),
            raw=True)
        return (2 * wma_half - wma_full).rolling(int(np.sqrt(length))).mean()
    return s


# --------------------------------------------------------------------- #
# strategy
# --------------------------------------------------------------------- #
@dataclass
class TRTSParams:
    rev_pct: float = 3.0
    src: str = "High/Low"           # "High/Low" | "Close"
    htf_on: bool = False
    htf_tf: str = "D"
    htf_len: int = 50
    adx_on: bool = False
    adx_thr: float = 20.0
    adx_len: int = 14
    smoothing: str = "None"         # None|SMA|EMA|RMA|WMA|HMA
    smooth_len: int = 3
    sl_atr: float = 1.5
    r1: float = 1.0
    r2: float = 2.0
    r3: float = 3.0
    atr_len: int = 14


@dataclass
class Trade:
    direction: str
    entry: float
    sl: float
    tp1: float
    tp2: float
    tp3: float
    entry_bar: int
    entry_time: object
    exit_price: float = field(default=float("nan"))
    exit_reason: str = "open"
    exit_bar: int = -1
    tp1_hit: bool = False
    tp2_hit: bool = False
    tp3_hit: bool = False
    sl_hit: bool = False
    reversed: bool = False
    sl_basis: str = "—"

    @property
    def risk(self) -> float:
        return abs(self.entry - self.sl)

    @property
    def realized_r(self) -> float:
        if np.isnan(self.exit_price) or self.risk == 0:
            return float("nan")
        d = 1.0 if self.direction == "long" else -1.0
        return d * (self.exit_price - self.entry) / self.risk


class TrailingReversalSystem:
    def __init__(self, p: TRTSParams | None = None):
        self.p = p or TRTSParams()

    # -- helpers ----------------------------------------------------- #
    def _prep(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.reset_index(drop=True)
        n = self.p.rev_pct / 100.0
        if self.p.src == "Close":
            raw_hi = df["close"]
            raw_lo = df["close"]
        else:
            raw_hi = df["high"]
            raw_lo = df["low"]
        df["_hiS"] = smooth(raw_hi, self.p.smooth_len, self.p.smoothing)
        df["_loS"] = smooth(raw_lo, self.p.smooth_len, self.p.smoothing)
        df["_atr"] = atr(df, self.p.atr_len)
        df["_n"] = n
        if self.p.adx_on:
            _, _, adx_ = dmi_adx(df, self.p.adx_len)
            df["_adx"] = adx_.shift(1)   # previous confirmed bar's ADX
        else:
            df["_adx"] = np.nan
        if self.p.htf_on:
            df["_htf_bull"], df["_htf_bear"] = self._htf_filter(df)
        else:
            df["_htf_bull"] = True
            df["_htf_bear"] = True
        return df

    def _htf_filter(self, df: pd.DataFrame):
        """HTF close vs EMA(htf_len), using the *previous* completed HTF bar."""
        res = df.set_index("date").resample(self.p.htf_tf).agg(
            {"close": "last"}).dropna()
        ema = res["close"].ewm(span=self.p.htf_len, adjust=False).mean()
        prev_close = res["close"].shift(1)
        prev_ema = ema.shift(1)
        bull = (prev_close > prev_ema).reindex(df["date"], method="ffill").to_numpy()
        bear = (prev_close < prev_ema).reindex(df["date"], method="ffill").to_numpy()
        return pd.Series(bull, index=df.index), pd.Series(bear, index=df.index)

    # -- main loop --------------------------------------------------- #
    def run(self, df: pd.DataFrame) -> tuple[list[Trade], pd.DataFrame]:
        d = self._prep(df.copy())
        n = self.p.rev_pct / 100.0
        hiS = d["_hiS"].to_numpy()
        loS = d["_loS"].to_numpy()
        atrV = d["_atr"].to_numpy()
        adxV = d["_adx"].to_numpy()
        hbull = d["_htf_bull"].to_numpy()
        hbear = d["_htf_bear"].to_numpy()

        swing_dir = 0
        ext_hi = ext_lo = np.nan
        ext_hi_bar = ext_lo_bar = -1
        piv_hi = piv_lo = np.nan

        # open trade state
        is_long = False
        entry = sl = tp1 = tp2 = tp3 = np.nan
        sig_bar = -1
        tp1_hit = tp2_hit = tp3_hit = sl_hit = reversed_ = False

        trades: list[Trade] = []
        cur: Trade | None = None

        for i in range(len(d)):
            hi = hiS[i]
            lo = loS[i]
            close = d["close"].iloc[i]
            src_ok = not np.isnan(hi) and not np.isnan(lo)

            # ---- track extremes ----------------------------------- #
            if src_ok and swing_dir >= 0 and (np.isnan(ext_hi) or hi > ext_hi):
                ext_hi, ext_hi_bar = hi, i
            if src_ok and swing_dir <= 0 and (np.isnan(ext_lo) or lo < ext_lo):
                ext_lo, ext_lo_bar = lo, i

            # ---- swing flips -------------------------------------- #
            up_raw = src_ok and swing_dir <= 0 and not np.isnan(ext_lo) \
                and i > ext_lo_bar and hi >= ext_lo * (1 + n)
            dn_raw = src_ok and swing_dir >= 0 and not np.isnan(ext_hi) \
                and i > ext_hi_bar and lo <= ext_hi * (1 - n)
            tie_up = ext_lo_bar > ext_hi_bar or (
                ext_lo_bar == ext_hi_bar
                and close >= (ext_hi + ext_lo) / 2)
            flip_up = up_raw and (not dn_raw or tie_up)
            flip_dn = dn_raw and (not up_raw or not tie_up)

            if flip_up:
                piv_lo = ext_lo
                swing_dir = 1
                ext_hi, ext_hi_bar = hi, i
            if flip_dn:
                piv_hi = ext_hi
                swing_dir = -1
                ext_lo, ext_lo_bar = lo, i

            # ---- manage open trade -------------------------------- #
            if cur is not None:
                live = np.isnan(cur.exit_price)  # still open while exit_price is NaN
                if live:
                    # intrabar TP touches
                    if is_long:
                        if not cur.tp1_hit and d["high"].iloc[i] >= cur.tp1:
                            cur.tp1_hit = True
                        if not cur.tp2_hit and d["high"].iloc[i] >= cur.tp2:
                            cur.tp2_hit = True
                        if not cur.tp3_hit and d["high"].iloc[i] >= cur.tp3:
                            cur.tp3_hit = True
                            cur.exit_price = cur.tp3
                            cur.exit_reason = "tp3"
                            cur.exit_bar = i
                        if not cur.sl_hit and not cur.tp3_hit and close < cur.sl:
                            cur.sl_hit = True
                            cur.exit_price = close
                            cur.exit_reason = "sl"
                            cur.exit_bar = i
                    else:
                        if not cur.tp1_hit and d["low"].iloc[i] <= cur.tp1:
                            cur.tp1_hit = True
                        if not cur.tp2_hit and d["low"].iloc[i] <= cur.tp2:
                            cur.tp2_hit = True
                        if not cur.tp3_hit and d["low"].iloc[i] <= cur.tp3:
                            cur.tp3_hit = True
                            cur.exit_price = cur.tp3
                            cur.exit_reason = "tp3"
                            cur.exit_bar = i
                        if not cur.sl_hit and not cur.tp3_hit and close > cur.sl:
                            cur.sl_hit = True
                            cur.exit_price = close
                            cur.exit_reason = "sl"
                            cur.exit_bar = i
                    # reversal exit (still open and opposite flip)
                    if (not cur.sl_hit and not cur.tp3_hit
                            and (flip_dn if is_long else flip_up)):
                        cur.reversed = True
                        cur.exit_price = close
                        cur.exit_reason = "reversal"
                        cur.exit_bar = i

            # ---- new signal --------------------------------------- #
            adx_ok = not self.p.adx_on or (not np.isnan(adxV[i]) and adxV[i] >= self.p.adx_thr)
            long_sig = flip_up and hbull[i] and adx_ok
            short_sig = flip_dn and hbear[i] and adx_ok

            new_long = long_sig
            new_short = short_sig
            if new_long or new_short:
                c_entry = close
                if new_long:
                    atr_stop = c_entry - atrV[i] * self.p.sl_atr
                    c_stop = max(piv_lo, atr_stop) if not np.isnan(atrV[i]) and not np.isnan(piv_lo) else atr_stop
                    basis = "Pivot" if atr_stop <= piv_lo else "ATR"
                else:
                    atr_stop = c_entry + atrV[i] * self.p.sl_atr
                    c_stop = min(piv_hi, atr_stop) if not np.isnan(atrV[i]) and not np.isnan(piv_hi) else atr_stop
                    basis = "Pivot" if atr_stop >= piv_hi else "ATR"
                c_risk = abs(c_entry - c_stop)
                ra, rb, rc = sorted([self.p.r1, self.p.r2, self.p.r3])
                if c_risk > 0 and not np.isnan(c_risk):
                    if new_long:
                        t1 = c_entry + c_risk * ra
                        t2 = c_entry + c_risk * rb
                        t3 = c_entry + c_risk * rc
                    else:
                        t1 = c_entry - c_risk * ra
                        t2 = c_entry - c_risk * rb
                        t3 = c_entry - c_risk * rc
                    cur = Trade(
                        direction="long" if new_long else "short",
                        entry=c_entry, sl=c_stop, tp1=t1, tp2=t2, tp3=t3,
                        entry_bar=i, entry_time=d["date"].iloc[i],
                        sl_basis=basis,
                    )
                    trades.append(cur)
                    is_long = new_long

        return trades, d


# --------------------------------------------------------------------- #
# metrics
# --------------------------------------------------------------------- #
def metrics(trades: list[Trade]) -> dict:
    closed = [t for t in trades if not np.isnan(t.exit_price)]
    if not closed:
        return {"trades": len(trades), "closed": 0}
    r = np.array([t.realized_r for t in closed])
    wins = r > 0
    gross_profit = r[wins].sum()
    gross_loss = abs(r[~wins].sum())
    return {
        "signals": len(trades),
        "closed": len(closed),
        "open": len(trades) - len(closed),
        "win_rate": float(wins.mean()),
        "avg_r": float(r.mean()),
        "net_rr": float(gross_profit / gross_loss) if gross_loss > 0 else float("inf"),
        "tp3_hits": sum(1 for t in closed if t.exit_reason == "tp3"),
        "sl_hits": sum(1 for t in closed if t.exit_reason == "sl"),
        "reversals": sum(1 for t in closed if t.exit_reason == "reversal"),
        "tp1_hits": sum(1 for t in trades if t.tp1_hit),
        "tp2_hits": sum(1 for t in trades if t.tp2_hit),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", required=True)
    ap.add_argument("--rev-pct", type=float, default=3.0)
    ap.add_argument("--src", default="High/Low", choices=["High/Low", "Close"])
    ap.add_argument("--htf-on", action="store_true")
    ap.add_argument("--htf-tf", default="D")
    ap.add_argument("--htf-len", type=int, default=50)
    ap.add_argument("--adx-on", action="store_true")
    ap.add_argument("--adx-thr", type=float, default=20.0)
    ap.add_argument("--smoothing", default="None", choices=["None", "SMA", "EMA", "RMA", "WMA", "HMA"])
    ap.add_argument("--sl-atr", type=float, default=1.5)
    ap.add_argument("--r1", type=float, default=1.0)
    ap.add_argument("--r2", type=float, default=2.0)
    ap.add_argument("--r3", type=float, default=3.0)
    ap.add_argument("--atr-len", type=int, default=14)
    args = ap.parse_args()

    df = pd.read_csv(args.csv, parse_dates=["date"])
    params = TRTSParams(
        rev_pct=args.rev_pct, src=args.src, htf_on=args.htf_on,
        htf_tf=args.htf_tf, htf_len=args.htf_len, adx_on=args.adx_on,
        adx_thr=args.adx_thr, smoothing=args.smoothing, sl_atr=args.sl_atr,
        r1=args.r1, r2=args.r2, r3=args.r3, atr_len=args.atr_len,
    )
    sys = TrailingReversalSystem(params)
    trades, _ = sys.run(df)
    m = metrics(trades)
    print(f"bars={len(df)}  params={params}")
    print(m)
    # print last few trades
    if trades:
        print("\nlast 5 trades:")
        cols = ["direction", "entry", "sl", "tp1", "tp2", "tp3", "exit_reason", "exit_price", "realized_r"]
        rows = [{
            "direction": t.direction, "entry": round(t.entry, 4),
            "sl": round(t.sl, 4), "tp1": round(t.tp1, 4), "tp2": round(t.tp2, 4),
            "tp3": round(t.tp3, 4), "exit_reason": t.exit_reason,
            "exit_price": round(t.exit_price, 4) if not np.isnan(t.exit_price) else None,
            "realized_r": round(t.realized_r, 3) if not np.isnan(t.realized_r) else None,
        } for t in trades[-5:]]
        print(pd.DataFrame(rows).to_string(index=False))


if __name__ == "__main__":
    main()
