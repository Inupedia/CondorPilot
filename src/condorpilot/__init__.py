"""CondorPilot public package API."""

from condorpilot.backtest import BacktestConfig, BacktestResult, ExitReason, run_backtest
from condorpilot.execution import ExecutionConfig
from condorpilot.history import OptionChainSnapshot
from condorpilot.importers import CsvHistoryError, load_option_chain_csv, save_option_chain_csv
from condorpilot.models import IronCondor, OptionQuote, OptionType, StrategyConfig
from condorpilot.research import (
    ParameterGrid,
    ResearchMetrics,
    ResearchParameters,
    ResearchRun,
    rank_runs,
    run_parameter_sweep,
    summarize_result,
)
from condorpilot.strategy import NoTradeError, build_iron_condor

__all__ = [
    "BacktestConfig",
    "BacktestResult",
    "CsvHistoryError",
    "ExecutionConfig",
    "ExitReason",
    "IronCondor",
    "NoTradeError",
    "OptionChainSnapshot",
    "OptionQuote",
    "OptionType",
    "ParameterGrid",
    "ResearchMetrics",
    "ResearchParameters",
    "ResearchRun",
    "StrategyConfig",
    "build_iron_condor",
    "load_option_chain_csv",
    "rank_runs",
    "run_backtest",
    "run_parameter_sweep",
    "save_option_chain_csv",
    "summarize_result",
]

__version__ = "0.3.0"
