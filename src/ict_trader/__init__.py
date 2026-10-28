"""ICT Trader — a reproducible ICT/SMC analysis and backtest engine.

The package is intentionally small and dependency-light:

    ict_trader.concepts   key levels, FVG, OB, CISD, liquidity sweeps
    ict_trader.setup      one frozen multi-timeframe setup
    ict_trader.strategy   execution, risk, MAE/MFE, portfolio metrics
    ict_trader.walk_forward purged out-of-sample evaluation
"""

from .concepts import ICTConfig, ICTResult, analyze, load_csv, plot, signals_frame
from .cisd import CISD, CISDConfig, detect_cisd
from .setup import SetupConfig, run_one
from .strategy import StrategyConfig, simulate_cisd, portfolio_curve
from .walk_forward import purged_walk_forward

__all__ = [
    "ICTConfig", "ICTResult", "analyze", "load_csv", "plot", "signals_frame",
    "CISD", "CISDConfig", "detect_cisd",
    "SetupConfig", "run_one",
    "StrategyConfig", "simulate_cisd", "portfolio_curve",
    "purged_walk_forward",
]
__version__ = "0.1.0"
