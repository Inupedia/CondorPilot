"""CondorPilot public package API."""

from condorpilot.backtest import BacktestConfig, BacktestResult, ExitReason, run_backtest
from condorpilot.execution import ExecutionConfig
from condorpilot.history import OptionChainSnapshot
from condorpilot.models import IronCondor, OptionQuote, OptionType, StrategyConfig
from condorpilot.strategy import NoTradeError, build_iron_condor

__all__ = [
    "BacktestConfig",
    "BacktestResult",
    "ExecutionConfig",
    "ExitReason",
    "IronCondor",
    "NoTradeError",
    "OptionChainSnapshot",
    "OptionQuote",
    "OptionType",
    "StrategyConfig",
    "build_iron_condor",
    "run_backtest",
]

__version__ = "0.2.0"
