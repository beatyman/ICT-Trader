"""缠论（缠中说禅）分析层，基于 chan.py（Vespa314），叠加在 ICT 之上。

用 chan.py 做完整缠论（分型/笔/线段/中枢/买卖点 + MACD 背驰），
这是 chan.py 相对 czsc 1.0.2 的优势——czsc 1.0.2 已移除 signals.cxt 买卖点函数。

chan.py 输出（只做辅助方向/点位判定，不单独发入场）：
  * 笔（BI）          —— 相邻顶底分型之间的一段走势
  * 线段（SEG）       —— 至少 3 笔构成的更大级别走势
  * 中枢（ZS）        —— 至少 3 笔重叠的盘整区（zd 下沿 / zg 上沿）
  * 买卖点（BSP）     —— 一买/一卖(背驰) / 二买/二卖(确认) / 三买/三卖(突破回踩)

依赖：chan.py 仓库（pip 未收录），通过环境变量 CHANPY_PATH 指定路径，
默认在 external/chan.py。无 chan.py 时回退到 czsc（仅结构，无买卖点）。
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

__all__ = ["ChanlunResult", "analyze_chanlun", "df_to_klus", "FREQ_MAP", "chanpy_available"]

# chan.py KL_TYPE 映射（chan.py 无 K_240M，4h 用 K_60M 近似）
FREQ_MAP = {"1m": "K_1M", "5m": "K_5M", "15m": "K_15M", "30m": "K_30M",
            "1h": "K_60M", "4h": "K_60M", "1d": "K_DAY", "1w": "K_WEEK"}

# chan.py 已 vendored 进本项目（vendor/chan.py），不依赖项目外目录
_DEFAULT_CHANPY = os.path.join(os.path.dirname(__file__), "..", "..", "vendor", "chan.py")


def _chanpy_dir() -> str:
    return os.environ.get("CHANPY_PATH", os.path.abspath(_DEFAULT_CHANPY))


def chanpy_available() -> bool:
    """chan.py 仓库是否可导入。"""
    d = _chanpy_dir()
    if os.path.isdir(d) and d not in sys.path:
        sys.path.insert(0, d)
    try:
        import Chan  # noqa: F401
        return True
    except Exception:
        return False


def _ensure_chanpy():
    d = _chanpy_dir()
    if d not in sys.path:
        sys.path.insert(0, d)
    if not chanpy_available():
        raise ImportError(
            f"chan.py not found at {d}; set CHANPY_PATH or clone "
            "https://github.com/Vespa314/chan.py")


@dataclass
class ChanlunResult:
    bi_count: int = 0
    seg_count: int = 0
    zs_count: int = 0
    last_bi_direction: str = "none"        # up / down / none
    last_zs_zd: float | None = None        # 中枢下沿
    last_zs_zg: float | None = None        # 中枢上沿
    price: float = float("nan")
    position: str = "none"                 # above / inside / below / none
    bias: str = "wait"                     # long / short / wait
    bsp_type: str = "none"                 # 1买/1卖/2买/2卖/3买/3卖/none
    bsp_count: int = 0
    bis: list = field(default_factory=list)
    segs: list = field(default_factory=list)
    zss: list = field(default_factory=list)
    bsps: list = field(default_factory=list)
    divergences: list = field(default_factory=list)
    reasons: list = field(default_factory=list)

    def to_public(self) -> dict:
        return {
            "bi_count": self.bi_count,
            "seg_count": self.seg_count,
            "zs_count": self.zs_count,
            "last_bi_direction": self.last_bi_direction,
            "last_zs_zd": self.last_zs_zd,
            "last_zs_zg": self.last_zs_zg,
            "price": self.price,
            "position": self.position,
            "bias": self.bias,
            "bsp_type": self.bsp_type,
            "bsp_count": self.bsp_count,
            "bis": self.bis,
            "segs": self.segs,
            "zss": self.zss,
            "bsps": self.bsps,
            "divergences": self.divergences,
            "reasons": self.reasons,
        }


def df_to_klus(df: pd.DataFrame, auto: bool = False):
    """OHLCV DataFrame -> chan.py CKLine_Unit 列表（按时间正序）。

    auto=True：把 00:00 的 bar 当作「当日收盘」（日线/周线，区间套用），
    auto=False：保留精确时刻（分钟级，单周期用）。
    """
    _ensure_chanpy()
    from Common.CEnum import DATA_FIELD
    from Common.CTime import CTime
    from KLine.KLine_Unit import CKLine_Unit

    klus = []
    df = df.sort_values("date").reset_index(drop=True)
    for i, r in df.iterrows():
        dt = pd.Timestamp(r["date"]).to_pydatetime()
        klus.append(CKLine_Unit({
            DATA_FIELD.FIELD_TIME: CTime(dt.year, dt.month, dt.day, dt.hour, dt.minute,
                                         auto=auto),
            DATA_FIELD.FIELD_OPEN: float(r["open"]),
            DATA_FIELD.FIELD_CLOSE: float(r["close"]),
            DATA_FIELD.FIELD_HIGH: float(r["high"]),
            DATA_FIELD.FIELD_LOW: float(r["low"]),
        }))
    return klus


def _bsp_type_to_str(types) -> str:
    """BSP_TYPE 列表 -> 中文买卖点标签。"""
    from Common.CEnum import BSP_TYPE
    m = {BSP_TYPE.T1: "1买", BSP_TYPE.T1P: "1卖", BSP_TYPE.T2: "2买",
         BSP_TYPE.T2S: "2卖", BSP_TYPE.T3A: "3买", BSP_TYPE.T3B: "3卖"}
    return ",".join(m.get(t, str(t)) for t in types)


def _ctime_ms(t) -> int:
    """CTime is local-naive; chan.py input bars are UTC, so rebuild as UTC."""
    return int(datetime(t.year, t.month, t.day, t.hour, t.minute,
                        getattr(t, "second", 0), tzinfo=timezone.utc).timestamp() * 1000)


def _klu_ms(klu) -> int:
    return _ctime_ms(klu.time)


def _bi_dict(bi) -> dict:
    return {
        "start_time": _klu_ms(bi.get_begin_klu()),
        "end_time": _klu_ms(bi.get_end_klu()),
        "start_price": float(bi.get_begin_val()),
        "end_price": float(bi.get_end_val()),
        "direction": "up" if bi.is_up() else "down",
        "sure": bool(getattr(bi, "is_sure", True)),
    }


def _seg_dict(seg) -> dict:
    return {
        "start_time": _klu_ms(seg.get_begin_klu()),
        "end_time": _klu_ms(seg.get_end_klu()),
        "start_price": float(seg.get_begin_val()),
        "end_price": float(seg.get_end_val()),
        "direction": "up" if seg.is_up() else "down",
        "sure": bool(getattr(seg, "is_sure", True)),
    }


def _zs_dict(zs) -> dict:
    return {
        "start_time": _klu_ms(zs.begin),
        "end_time": _klu_ms(zs.end),
        "low": float(zs.low),
        "high": float(zs.high),
        "mid": float((zs.low + zs.high) / 2.0),
        "begin_bi": int(zs.begin_bi.idx),
        "end_bi": int(zs.end_bi.idx),
    }


def _bsp_dict(bsp) -> dict:
    types = bsp.type2str()
    return {
        "time": _klu_ms(bsp.klu),
        "price": float(bsp.klu.low if bsp.is_buy else bsp.klu.high),
        "is_buy": bool(bsp.is_buy),
        "type": types,
        "bi_idx": int(getattr(bsp.bi, "idx", -1)),
        "divergence": types.startswith("1") or "1p" in types or "1买" in types or "1卖" in types,
    }


def analyze_chanlun(df: pd.DataFrame, symbol: str = "SYM", freq: str = "1d") -> ChanlunResult:
    """跑 chan.py，派生缠论结构 + 方向 + 中枢位置 + 买卖点。"""
    res = ChanlunResult()
    if len(df) < 30:
        res.reasons.append("insufficient bars (<30)")
        return res

    try:
        _ensure_chanpy()
    except ImportError as e:
        # 回退到 czsc（仅结构，无买卖点）
        res.reasons.append(f"chan.py unavailable ({e}); fallback to czsc structure")
        return _fallback_czsc(df, symbol, freq)

    from Chan import CChan
    from ChanConfig import CChanConfig
    from Common.CEnum import KL_TYPE

    klus = df_to_klus(df)
    kl_type = getattr(KL_TYPE, FREQ_MAP.get(freq, "K_DAY"))
    config = CChanConfig({
        "bi_strict": True, "trigger_step": True, "skip_step": 0,
        "divergence_rate": float("inf"), "bsp2_follow_1": False, "bsp3_follow_1": False,
        "min_zs_cnt": 0, "bs1_peak": False, "macd_algo": "peak",
        "bs_type": "1,2,3a,1p,2s,3b", "print_warning": False, "zs_algo": "normal",
    })
    chan = CChan(code=symbol, data_src=None, lv_list=[kl_type], config=config)
    chan.trigger_load({kl_type: klus})
    chan.kl_datas[kl_type].cal_seg_and_zs()
    kline = chan[kl_type]

    res.bi_count = len(kline.bi_list)
    res.seg_count = len(kline.seg_list)
    res.zs_count = len(kline.zs_list)
    res.price = float(df["close"].iloc[-1])

    res.bis = [_bi_dict(b) for b in list(kline.bi_list)[-200:]]
    res.segs = [_seg_dict(s) for s in list(kline.seg_list)[-100:]]
    res.zss = [_zs_dict(z) for z in list(kline.zs_list)[-30:]]
    bsp_list = kline.bs_point_lst
    all_bsp_raw = bsp_list.getSortedBspList() if hasattr(bsp_list, "getSortedBspList") else []
    res.bsps = [_bsp_dict(b) for b in list(all_bsp_raw)[-60:]]
    res.divergences = [b for b in res.bsps if b.get("divergence")]
    if kline.bi_list:
        last = kline.bi_list[-1]
        res.last_bi_direction = "up" if last.dir == 1 else "down"

    if kline.zs_list:
        zs = kline.zs_list[-1]
        res.last_zs_zd = float(zs.low)
        res.last_zs_zg = float(zs.high)

    if res.last_zs_zd is not None and res.last_zs_zg is not None:
        if res.price > res.last_zs_zg:
            res.position = "above"
        elif res.price < res.last_zs_zd:
            res.position = "below"
        else:
            res.position = "inside"
    else:
        res.position = "none"

    # 买卖点（chan.py 完整实现）
    bsp = kline.bs_point_lst
    all_bsp = bsp.getSortedBspList() if hasattr(bsp, "getSortedBspList") else []
    res.bsp_count = len(all_bsp)
    if all_bsp:
        latest = all_bsp[-1]
        res.bsp_type = _bsp_type_to_str(latest.type)
        if latest.is_buy:
            res.bias = "long"
        else:
            res.bias = "short"
        res.reasons.append(f"latest bsp {res.bsp_type}")
    else:
        # 无买卖点 → 用中枢位置 + 笔方向
        if res.position == "inside":
            res.bias = "wait"
            res.reasons.append("price inside zhongshu, wait")
        elif res.position == "above" and res.last_bi_direction == "up":
            res.bias = "long"
            res.reasons.append("above zhongshu + up bi")
        elif res.position == "below" and res.last_bi_direction == "down":
            res.bias = "short"
            res.reasons.append("below zhongshu + down bi")
        else:
            res.bias = "long" if res.last_bi_direction == "up" else "short"
            res.reasons.append(f"follow last bi {res.last_bi_direction}")
    return res


def _fallback_czsc(df: pd.DataFrame, symbol: str, freq: str) -> ChanlunResult:
    """chan.py 不可用时的 czsc 回退（仅结构）。"""
    from czsc import CZSC
    bars = []
    from czsc import RawBar, Freq
    f = getattr(Freq, {"1d": "D", "15m": "F15", "1h": "F60"}.get(freq, "D"))
    df = df.sort_values("date").reset_index(drop=True)
    for i, r in df.iterrows():
        dt = pd.Timestamp(r["date"]).to_pydatetime()
        bars.append(RawBar(symbol=symbol, dt=dt, freq=f, open=float(r["open"]),
                           close=float(r["close"]), high=float(r["high"]),
                           low=float(r["low"]), vol=0.0, amount=0.0, id=i))
    c = CZSC(bars)
    res = ChanlunResult()
    res.bi_count = len(c.bi_list)
    res.zs_count = len(c.zs_list)
    res.price = float(df["close"].iloc[-1])
    if c.bi_list:
        res.last_bi_direction = "up" if str(c.bi_list[-1].direction).lower().startswith("up") else "down"
    if c.zs_list:
        res.last_zs_zd = float(c.zs_list[-1].zd)
        res.last_zs_zg = float(c.zs_list[-1].zg)
        if res.price > res.last_zs_zg:
            res.position = "above"
        elif res.price < res.last_zs_zd:
            res.position = "below"
        else:
            res.position = "inside"
    res.bias = "long" if res.last_bi_direction == "up" else "short" if res.last_bi_direction == "down" else "wait"
    return res
