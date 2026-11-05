"""一目均衡表（Ichimoku Kinko Hyo）指标库与可回测策略。

模块::

    indicators   Tenkan / Kijun / SenkouA / SenkouB + 偏移模式
    signals      趋势 / 动能 / 迟行 / 三役 + 信号分级
    backtest     回测引擎 / 绩效指标 / 参数扫描
"""
from .indicators import ichimoku, midpoint, span_offset, OFFSET_MODES
from .signals import signals, crossover, crossunder, GRADE_COEF
from .backtest import (
    Trade, StrategyConfig, run_backtest, compute_metrics, scan_params, TIMEFRAME_RANK,
)

__all__ = [
    "ichimoku", "midpoint", "span_offset", "OFFSET_MODES",
    "signals", "crossover", "crossunder", "GRADE_COEF",
    "Trade", "StrategyConfig", "run_backtest", "compute_metrics",
    "scan_params", "TIMEFRAME_RANK",
]
