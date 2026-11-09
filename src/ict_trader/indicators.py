"""Ichimoku, MACD and HTF alignment helpers.

HTF frames are labelled at the bucket open (``label="left"``) and only the
completed buckets are returned. A base bar therefore sees an HTF bar only after
that HTF period has actually closed.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def ichimoku(df: pd.DataFrame, tenkan: int = 9, kijun: int = 26, senkou: int = 52) -> pd.DataFrame:
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    close = df["close"].astype(float)
    conv = (high.rolling(tenkan).max() + low.rolling(tenkan).min()) / 2
    base = (high.rolling(kijun).max() + low.rolling(kijun).min()) / 2
    span_a = ((conv + base) / 2).shift(kijun)
    span_b = ((high.rolling(senkou).max() + low.rolling(senkou).min()) / 2).shift(kijun)
    # Chikou is the current close compared with the close kijun bars ago.
    chikou_ok = close > close.shift(kijun)
    out = pd.DataFrame(index=df.index)
    out["tenkan"] = conv
    out["kijun"] = base
    out["span_a"] = span_a
    out["span_b"] = span_b
    out["chikou_ok"] = chikou_ok
    out["cloud_top"] = out[["span_a", "span_b"]].max(axis=1)
    out["cloud_bottom"] = out[["span_a", "span_b"]].min(axis=1)
    return out


def macd(df: pd.DataFrame, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    close = df["close"].astype(float)
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    line = ema_fast - ema_slow
    sig = line.ewm(span=signal, adjust=False).mean()
    hist = line - sig
    return pd.DataFrame({"macd": line, "signal": sig, "hist": hist}, index=df.index)


def add_indicators(out: pd.DataFrame) -> pd.DataFrame:
    """Add Ichimoku/MACD columns to an OHLCV frame."""
    i = ichimoku(out)
    out = out.copy()
    out["tenkan"] = i["tenkan"].to_numpy()
    out["kijun"] = i["kijun"].to_numpy()
    out["span_a"] = i["span_a"].to_numpy()
    out["span_b"] = i["span_b"].to_numpy()
    out["chikou_ok"] = i["chikou_ok"].to_numpy()
    out["cloud_top"] = out[["span_a", "span_b"]].max(axis=1)
    out["cloud_bottom"] = out[["span_a", "span_b"]].min(axis=1)
    m = macd(out)
    out["macd"] = m["macd"].to_numpy()
    out["macd_signal"] = m["signal"].to_numpy()
    out["macd_hist"] = m["hist"].to_numpy()
    return out


# Backward-compatible internal name.
_add_indicators = add_indicators


def resample_htf(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    x = df.copy()
    x["date"] = pd.to_datetime(x["date"])
    x = x.set_index("date")
    # label="left", closed="left" aligns to the exchange's bar-open boundary
    # (UTC midnight for 1D, 00:00/04:00/... for 4H).  The old right/right
    # shifted the HTF bar by one 15m bar, so open/close were wrong.
    out = x.resample(rule, label="left", closed="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    ).dropna(subset=["open", "high", "low", "close"])
    # A left-labelled bucket at 01:00 covers [01:00, 02:00). If the last base
    # bar is 01:45, that bucket is still forming and must not be visible to a
    # base-bar decision. Drop it before computing indicators/alignment.
    if len(out) and len(df):
        step = pd.tseries.frequencies.to_offset(rule)
        last_base = pd.Timestamp(df["date"].iloc[-1])
        if pd.Timestamp(out.index[-1]) + step > last_base:
            out = out.iloc[:-1]
    out = out.reset_index()
    return _add_indicators(out)


def resample_cn_4h(df: pd.DataFrame) -> pd.DataFrame:
    """A-share 4H bars: 09:30-11:30 and 13:00-15:00, timestamped at the close."""
    x = df.copy()
    x["date"] = pd.to_datetime(x["date"])
    t = x["date"].dt.time
    x["_sess"] = np.where(t <= pd.Timestamp("1900-01-01 11:30").time(), "AM", "PM")
    grouped = x.groupby([x["date"].dt.date, "_sess"], sort=True).agg(
        date=("date", "last"),
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        volume=("volume", "sum"),
    ).reset_index(drop=True)
    grouped = grouped.dropna(subset=["open", "high", "low", "close"]).sort_values("date")
    return _add_indicators(grouped)


def mark_htf_known_at(htf: pd.DataFrame, rule: str) -> pd.DataFrame:
    """Move left-labelled bucket opens to their actual availability time.

    A 09:00-10:00 bucket has a left label of 09:00 but is only knowable at
    10:00.  ``align_htf`` uses backward as-of joins, so passing the left label
    would leak the bucket into 09:15-09:45 decisions.  The close-time label is
    the correct ``known_at`` boundary.
    """
    if htf is None or len(htf) == 0:
        return htf
    out = htf.copy()
    out["date"] = pd.to_datetime(out["date"]) + pd.tseries.frequencies.to_offset(rule)
    return out


def align_htf(base: pd.DataFrame, htf: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    left = base[["date"]].copy()
    left["date"] = pd.to_datetime(left["date"])
    right = htf[["date"] + cols].copy()
    right["date"] = pd.to_datetime(right["date"])
    left = left.sort_values("date")
    right = right.sort_values("date")
    merged = pd.merge_asof(left, right, on="date", direction="backward", allow_exact_matches=True)
    return merged

