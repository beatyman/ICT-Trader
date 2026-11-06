"""补足两个参考脚本（LuxAlgo ICT Concepts + CISD Model+）的策略缺口。

缺口 1：SMT 跨品种背离的自动配对（resolve_pair，来自 CISD Model+）。
缺口 2：BPR（Balance Price Range，均衡区，来自 LuxAlgo ICT Concepts）。
缺口 3：VI（Volume Imbalance，量能不平衡，来自 LuxAlgo ICT Concepts）。

另含 fetch_binance_klines：从 Binance data-api 镜像拉 K 线，供 SMT 拉相关品种用
（ETH -> BTC，山寨 -> ETH）。
"""
from __future__ import annotations

import json
import subprocess

import numpy as np
import pandas as pd

__all__ = ["resolve_pair", "detect_bpr", "detect_vimbalances",
           "fetch_binance_klines", "fetch_pair_data"]


# CISD Model+ resolvePair() 的自动配对映射（crypto + 主要期货/外汇）
_PAIR_MAP = {
    "BTCUSDT": "ETHUSDT", "ETHUSDT": "BTCUSDT",
    "BTCUSD": "ETHUSD", "ETHUSD": "BTCUSD",
    "BTC": "ETH", "ETH": "BTC",
    "ES": "NQ", "NQ": "ES", "YM": "ES", "RTY": "ES",
    "GC": "SI", "SI": "GC", "CL": "RB", "RB": "CL",
    "6E": "6B", "6B": "6E", "6A": "6N", "6N": "6A",
    "XAUUSD": "XAGUSD", "XAGUSD": "XAUUSD",
    "EURUSD": "GBPUSD", "GBPUSD": "EURUSD",
    "AUDUSD": "NZDUSD", "NZDUSD": "AUDUSD",
    "USDJPY": "DXY", "USDCHF": "DXY", "USDCAD": "DXY", "DXY": "USDJPY",
    "EURJPY": "GBPJPY", "GBPJPY": "EURJPY",
    "AUDJPY": "NZDJPY", "NZDJPY": "AUDJPY",
}

# 恒生科技指数（HSTECH, HK.800700）主要成分股代码
_HK_TECH_CODES = {
    "00700",  # 腾讯
    "03690",  # 美团
    "09988",  # 阿里巴巴
    "09618",  # 京东集团
    "09888",  # 百度
    "09999",  # 网易
    "01810",  # 小米
    "02382",  # 舜宇光学
    "01024",  # 快手
    "00981",  # 中芯国际
    "00285",  # 比亚迪电子
    "00268",  # 金蝶国际
    "00992",  # 联想集团
    "06618",  # 京东健康
    "00241",  # 阿里健康
    "09961",  # 携程集团
    "06690",  # 海尔智家
    "02618",  # 京东物流
    "01347",  # 华虹半导体
    "09626",  # 哔哩哔哩
    "06060",  # 众安在线
    "00772",  # 阅文集团
    "01833",  # 平安好医生
    "02015",  # 理想汽车
    "09868",  # 小鹏汽车
    "09866",  # 蔚来
    "00909",  # 明源云
    "09698",  # 万国数据
    "03888",  # 金山软件
    "02013",  # 微盟集团
}

# 指数基准
_IDX_HSTECH = "HK.800700"   # 恒生科技指数
_IDX_HSI = "HK.800000"      # 恒生指数
_IDX_NDX = "US.IXIC"        # 纳斯达克综合
_IDX_SH = "SH.000001"       # 上证指数
_IDX_CHINEXT = "SZ.399006"  # 创业板指
_IDX_STAR50 = "SH.000688"   # 科创50


def resolve_pair(symbol: str) -> str:
    """自动解析相关品种（SMT 用）。

    规则（用户约定）：
      ETH        -> BTC（以太坊对基准比特币）
      BTC        -> ETH
      其他山寨   -> ETH（USDT/USD 结尾的加密品种，以太坊作为市场基准）
      期货/外汇  -> 内置映射（ES->NQ、XAU->XAG 等）
    未知且非 USDT/USD 结尾 -> 空串。

    归一化：去掉交易所前缀、去掉期货 ``1!`` 后缀、转大写。
    """
    s = (symbol or "").upper()
    # 去掉前缀（如 BINANCE:BTCUSDT / CME_MINI:ES1!）
    if ":" in s:
        s = s.split(":")[-1]
    # 去掉期货连续合约后缀 1!
    if s.endswith("1!"):
        s = s[:-2]
    if s in _PAIR_MAP:
        return _PAIR_MAP[s]
    # 山寨默认：USDT/USD 结尾的加密品种 -> ETH 作为市场基准
    if s.endswith("USDT") or s.endswith("USD"):
        return "ETHUSDT"
    # 港股：科技类 -> 恒生科技指数，其余 -> 恒生指数
    if s.startswith("HK."):
        code = s[3:]
        return _IDX_HSTECH if code in _HK_TECH_CODES else _IDX_HSI
    # 美股 -> 纳斯达克综合
    if s.startswith("US."):
        return _IDX_NDX
    # A股：科创50(688) -> 科创50，创业板(30) -> 创业板指，其余 -> 上证指数
    if s.startswith("SH.688"):
        return _IDX_STAR50
    if s.startswith("SZ.30"):
        return _IDX_CHINEXT
    if s.startswith("SH.") or s.startswith("SZ."):
        return _IDX_SH
    return ""


def fetch_binance_klines(symbol: str, interval: str = "15m", limit: int = 1000,
                         drop_forming: bool = True) -> pd.DataFrame | None:
    """从 Binance data-api 镜像拉 K 线（无代理直连）。

    用于 SMT 拉相关品种：resolve_pair(ETH) -> BTCUSDT，resolve_pair(山寨) -> ETHUSDT。
    返回 [date, open, high, low, close, volume]；失败返回 None。
    """
    sym = (symbol or "").upper()
    if ":" in sym:
        sym = sym.split(":")[-1]
    url = f"https://data-api.binance.vision/api/v3/klines?symbol={sym}&interval={interval}&limit={limit}"
    try:
        out = subprocess.run(["curl.exe", "-s", "--max-time", "30", url],
                             capture_output=True, text=True, encoding="utf-8", timeout=40)
        data = json.loads(out.stdout)
    except Exception:
        return None
    if not isinstance(data, list) or not data:
        return None
    df = pd.DataFrame(data, columns=["ot", "open", "high", "low", "close", "volume",
                                     "ct", "qv", "n", "tb", "tq", "ig"])
    df["date"] = pd.to_datetime(df["ot"], unit="ms")
    for c in ["open", "high", "low", "close", "volume"]:
        df[c] = df[c].astype(float)
    if drop_forming and len(df) > 0:
        df = df.iloc[:-1]
    return df[["date", "open", "high", "low", "close", "volume"]].reset_index(drop=True)


def fetch_pair_data(symbol: str, interval: str = "15m", limit: int = 1000,
                    start: str = "2023-01-01", end: str = "2026-12-31"):
    """解析配对并拉相关品种数据，返回 (df, pair)。

    加密品种 -> Binance data-api；港股/美股/A股指数 -> Futu OpenD。
    Futu 失败时返回 (None, pair)。
    """
    pair = resolve_pair(symbol)
    if not pair:
        return None, ""
    if pair.endswith("USDT") or pair.endswith("USD") or pair in ("BTCUSDT", "ETHUSDT"):
        return fetch_binance_klines(pair, interval, limit), pair
    # 指数（HK./US./SH./SZ.）→ Futu
    try:
        from futu import AuType, KLType, OpenQuoteContext
        kt = {"1m": KLType.K_1M, "5m": KLType.K_5M, "15m": KLType.K_15M,
              "30m": KLType.K_30M, "60m": KLType.K_60M, "1h": KLType.K_60M,
              "1d": KLType.K_DAY, "1w": KLType.K_WEEK}.get(interval, KLType.K_15M)
        ctx = OpenQuoteContext(host="127.0.0.1", port=11111)
        try:
            ret, data, page_key = ctx.request_history_kline(
                code=pair, start=start, end=end, ktype=kt,
                autype=AuType.QFQ, max_count=1000)
            if ret != 0:
                return None, pair
            frames = [data]
            while page_key:
                ret, data, page_key = ctx.request_history_kline(
                    code=pair, start=start, end=end, ktype=kt,
                    autype=AuType.QFQ, max_count=1000, page_req_key=page_key)
                if ret != 0:
                    break
                frames.append(data)
        finally:
            ctx.close()
        df = pd.concat(frames).drop_duplicates("time_key").sort_values("time_key")
        df = df.rename(columns={"time_key": "date"})
        return df[["date", "open", "high", "low", "close", "volume"]].reset_index(drop=True), pair
    except Exception:
        return None, pair


def detect_bpr(fvgs, price: float | None = None) -> tuple[float, float] | None:
    """BPR（Balance Price Range）：最新 bull FVG 与最新 bear FVG 的重叠区。

    这是「均衡区」，等于 OTE 高质量回踩区。返回 (bottom, top) 或 None。
    ``fvgs`` 为 ICTResult.fvgs（有 .direction / .top / .bottom / .active）。
    """
    bulls = [f for f in fvgs if getattr(f, "direction", None) == "bull" and getattr(f, "active", True)]
    bears = [f for f in fvgs if getattr(f, "direction", None) == "bear" and getattr(f, "active", True)]
    if not bulls or not bears:
        return None
    b = max(bulls, key=lambda f: getattr(f, "created", 0))
    s = max(bears, key=lambda f: getattr(f, "created", 0))
    top = min(float(b.top), float(s.top))
    bottom = max(float(b.bottom), float(s.bottom))
    if bottom >= top:
        return None
    # 当前价必须在 BPR 之外才构成有效回踩区（在区内说明已进入均衡）
    if price is not None and bottom <= price <= top:
        return None
    return (round(bottom, 6), round(top, 6))


def detect_vimbalances(df: pd.DataFrame, vol_mult: float = 1.2) -> pd.DataFrame:
    """VI（Volume Imbalance）：量能不平衡（缺口 + 放量）。

    返回与 df 对齐的 DataFrame，含 vim_bull / vim_bear 布尔列。
    无未来函数：只用当前及过去 bar。
    """
    n = len(df)
    open_ = df["open"].astype(float).to_numpy()
    high = df["high"].astype(float).to_numpy()
    low = df["low"].astype(float).to_numpy()
    close = df["close"].astype(float).to_numpy()
    vol = df["volume"].astype(float).to_numpy() if "volume" in df else np.ones(n)
    vol_sma = pd.Series(vol).rolling(20, min_periods=1).mean().to_numpy()

    vim_bull = np.zeros(n, dtype=bool)
    vim_bear = np.zeros(n, dtype=bool)
    for i in range(1, n):
        mx = max(close[i], open_[i])
        mn = min(close[i], open_[i])
        vol_ok = vol[i] >= vol_sma[i] * vol_mult
        # LuxAlgo vImb_Bl: 开盘 > 前收 且 前高 > 现低 且 现收 > 前收 且 现开 > 前开 且 前高 < 现低(mn)
        bull = (open_[i] > close[i - 1] and high[i - 1] > low[i]
                and close[i] > close[i - 1] and open_[i] > open_[i - 1]
                and high[i - 1] < mn and vol_ok)
        # vImb_Br: 开盘 < 前收 且 前低 < 现高 且 现收 < 前收 且 现开 < 前开 且 前低 > 现高(mx)
        bear = (open_[i] < close[i - 1] and low[i - 1] < high[i]
                and close[i] < close[i - 1] and open_[i] < open_[i - 1]
                and low[i - 1] > mx and vol_ok)
        vim_bull[i] = bool(bull)
        vim_bear[i] = bool(bear)
    return pd.DataFrame({"vim_bull": vim_bull, "vim_bear": vim_bear}, index=df.index)
