"""CondorPilot public package API."""

from condorpilot.backtest import (
    BacktestConfig,
    BacktestResult,
    EntryFilter,
    ExitReason,
    run_backtest,
)
from condorpilot.diagnostics import (
    DatasetDiagnostics,
    DiagnosticThresholds,
    dataset_fingerprint,
    diagnose_dataset,
)
from condorpilot.evidence import (
    EvidenceResult,
    EvidenceThresholds,
    EvidenceVerdict,
    RegimePerformance,
    evidence_to_dict,
    render_evidence_json,
    render_evidence_markdown,
    run_evidence,
    write_evidence_report,
)
from condorpilot.execution import ExecutionConfig, ExecutionDataError
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
from condorpilot.validation import (
    WalkForwardConfig,
    WalkForwardError,
    WalkForwardFold,
    WalkForwardResult,
    run_walk_forward,
)
from condorpilot.vendors.thetadata import ThetaDataClient, ThetaDataConfig, ThetaDataError
from condorpilot.volatility import (
    RegimeThresholds,
    VixObservation,
    VolatilityObservation,
    VolatilityRegime,
    build_regime_entry_filter,
    build_volatility_regimes,
    estimate_atm_iv,
    fetch_cboe_vix_history,
    load_vix_csv,
)

__all__ = [
    "BacktestConfig",
    "BacktestResult",
    "CsvHistoryError",
    "DatasetDiagnostics",
    "DiagnosticThresholds",
    "EntryFilter",
    "EvidenceResult",
    "EvidenceThresholds",
    "EvidenceVerdict",
    "ExecutionConfig",
    "ExecutionDataError",
    "ExitReason",
    "IronCondor",
    "NoTradeError",
    "OptionChainSnapshot",
    "OptionQuote",
    "OptionType",
    "ParameterGrid",
    "RegimePerformance",
    "RegimeThresholds",
    "ResearchMetrics",
    "ResearchParameters",
    "ResearchRun",
    "StrategyConfig",
    "ThetaDataClient",
    "ThetaDataConfig",
    "ThetaDataError",
    "VixObservation",
    "VolatilityObservation",
    "VolatilityRegime",
    "WalkForwardConfig",
    "WalkForwardError",
    "WalkForwardFold",
    "WalkForwardResult",
    "build_iron_condor",
    "build_regime_entry_filter",
    "build_volatility_regimes",
    "dataset_fingerprint",
    "diagnose_dataset",
    "estimate_atm_iv",
    "evidence_to_dict",
    "fetch_cboe_vix_history",
    "load_option_chain_csv",
    "load_vix_csv",
    "rank_runs",
    "render_evidence_json",
    "render_evidence_markdown",
    "run_backtest",
    "run_evidence",
    "run_parameter_sweep",
    "run_walk_forward",
    "save_option_chain_csv",
    "summarize_result",
    "write_evidence_report",
]

__version__ = "0.8.0"
