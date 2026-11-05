"""一目均衡表信号：趋势 / 动能 / 迟行 / 三役 + 信号分级。

约定（与验收标准一一对应）::

    趋势（云区，用 leading_a/leading_b，即 [kijun] 偏移，不用 [2*kijun]）
      多头：close >  max(leading_a, leading_b)
      空头：close <  min(leading_a, leading_b)
      云内：介于两者之间 → 中性，不交易
      云色：leading_a > leading_b → 偏多，否则偏空

    动能
      TK 金叉：crossover(Tenkan, Kijun)；死叉：crossunder
      价/基准线金叉：crossover(close, Kijun)；死叉：crossunder

    迟行确认（Chikou 不作为输出，仅比较 close 与 close[kijun]）
      多头：close[t] >  close[t - kijun_period]
      空头：close[t] <  close[t - kijun_period]

    三役好轉：TK 金叉 + 迟行多头 + 价格上破云区
    三役逆轉：TK 死叉 + 迟行空头 + 价格下破云区

    信号分级（映射仓位系数）
      强：信号在云上（多）/ 云下（空），且云色同向   -> 1.0
      中：信号在云内                                  -> 0.5
      弱：信号逆云方向                                -> 0.0（可配 0.25）
"""
from __future__ import annotations

import numpy as np
import pandas as pd

__all__ = ["crossover", "crossunder", "signals", "GRADE_COEF"]


GRADE_COEF = {"strong": 1.0, "medium": 0.5, "weak": 0.0}


def crossover(a: pd.Series, b: pd.Series) -> pd.Series:
    """a 上穿 b（前一根 <=，当前根 >）。"""
    return (a > b) & (a.shift(1) <= b.shift(1))


def crossunder(a: pd.Series, b: pd.Series) -> pd.Series:
    """a 下穿 b（前一根 >=，当前根 <）。"""
    return (a < b) & (a.shift(1) >= b.shift(1))


def signals(
    df: pd.DataFrame,
    ind: pd.DataFrame,
    kijun_period: int = 26,
    weak_coef: float = 0.0,
) -> pd.DataFrame:
    """计算全部信号，返回与 ``df`` 对齐的 DataFrame。

    返回列（布尔或浮点）::

        cloud_top, cloud_bottom, cloud_thick   云区上下沿与厚度
        above_cloud, below_cloud, in_cloud    云区状态
        cloud_bull                             云色（leading_a > leading_b）
        tk_golden, tk_death                    TK 金叉/死叉
        price_above_kijun, price_below_kijun  价格 vs 基准线（交叉用 signals 层）
        chikou_bull, chikou_bear              迟行确认
        base_bull, base_bear                  基础信号（动能 + 迟行）
        three_bull, three_bear                三役好轉 / 三役逆轉
        grade                                  'strong' / 'medium' / 'weak' / ''
        coef                                   仓位系数
    """
    close = df["close"].astype(float)
    tenkan = ind["tenkan"]
    kijun = ind["kijun"]
    la = ind["leading_a"]
    lb = ind["leading_b"]

    cloud_top = np.maximum(la, lb)
    cloud_bottom = np.minimum(la, lb)
    cloud_thick = (cloud_top - cloud_bottom).abs()

    above_cloud = close > cloud_top
    below_cloud = close < cloud_bottom
    in_cloud = ~(above_cloud | below_cloud)
    cloud_bull = la > lb

    tk_golden = crossover(tenkan, kijun)
    tk_death = crossunder(tenkan, kijun)
    price_above_kijun = crossover(close, kijun)
    price_below_kijun = crossunder(close, kijun)

    # 迟行确认：close 与 close[kijun] 比较（Chikou 不作为输出线）
    chikou_bull = close > close.shift(kijun_period)
    chikou_bear = close < close.shift(kijun_period)

    base_bull = tk_golden & chikou_bull
    base_bear = tk_death & chikou_bear

    three_bull = base_bull & above_cloud
    three_bear = base_bear & below_cloud

    # 信号分级：按云区位置 + 云色
    grade = pd.Series("", index=df.index, dtype=object)
    # 多
    grade[base_bull & above_cloud & cloud_bull] = "strong"
    grade[base_bull & in_cloud] = "medium"
    grade[base_bull & below_cloud] = "weak"
    # 空
    grade[base_bear & below_cloud & ~cloud_bull] = "strong"
    grade[base_bear & in_cloud] = "medium"
    grade[base_bear & above_cloud] = "weak"

    coef = grade.map(lambda g: weak_coef if g == "weak" else GRADE_COEF.get(g, 0.0))

    out = pd.DataFrame(
        {
            "cloud_top": cloud_top,
            "cloud_bottom": cloud_bottom,
            "cloud_thick": cloud_thick,
            "above_cloud": above_cloud,
            "below_cloud": below_cloud,
            "in_cloud": in_cloud,
            "cloud_bull": cloud_bull,
            "tk_golden": tk_golden,
            "tk_death": tk_death,
            "price_above_kijun": price_above_kijun,
            "price_below_kijun": price_below_kijun,
            "chikou_bull": chikou_bull,
            "chikou_bear": chikou_bear,
            "base_bull": base_bull,
            "base_bear": base_bear,
            "three_bull": three_bull,
            "three_bear": three_bear,
            "grade": grade,
            "coef": coef,
        },
        index=df.index,
    )
    return out
