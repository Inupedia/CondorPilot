"""Walk-forward and out-of-sample validation for CondorPilot research."""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta

from condorpilot.backtest import (
    BacktestConfig,
    BacktestResult,
    EntryFilter,
    ExitReason,
    run_backtest,
)
from condorpilot.diagnostics import DatasetDiagnostics, DiagnosticThresholds, diagnose_dataset
from condorpilot.history import OptionChainSnapshot, validate_history
from condorpilot.models import StrategyConfig
from condorpilot.research import (
    ParameterGrid,
    ResearchMetrics,
    ResearchParameters,
    ResearchRun,
    expand_parameter_grid,
    rank_runs,
    summarize_result,
)

_SUPPORTED_RANK_METRICS = {
    "total_return",
    "cagr",
    "sharpe",
    "sortino",
    "win_rate",
    "profit_factor",
    "average_trade",
}


class WalkForwardError(ValueError):
    """Raised when walk-forward evidence cannot be produced without invalid assumptions."""


@dataclass(frozen=True, slots=True)
class WalkForwardConfig:
    """Windowing and selection rules for rolling or anchored validation."""

    train_size: int = 252
    test_size: int = 63
    step_size: int = 63
    anchored: bool = False
    rank_by: str = "sortino"
    min_train_trades: int = 3
    require_dataset_pass: bool = True

    def __post_init__(self) -> None:
        if self.train_size < 2:
            raise ValueError("train_size must be at least 2 snapshots")
        if self.test_size < 2:
            raise ValueError("test_size must be at least 2 snapshots")
        if self.step_size < self.test_size:
            raise ValueError("step_size must be at least test_size to avoid overlapping OOS folds")
        if self.rank_by not in _SUPPORTED_RANK_METRICS:
            choices = ", ".join(sorted(_SUPPORTED_RANK_METRICS))
            raise ValueError(f"unsupported rank_by {self.rank_by!r}; choose one of {choices}")
        if self.min_train_trades < 0:
            raise ValueError("min_train_trades must be non-negative")


@dataclass(frozen=True, slots=True)
class WalkForwardFold:
    index: int
    train_start: datetime
    train_end: datetime
    test_start: datetime
    test_end: datetime
    train_snapshot_count: int
    test_snapshot_count: int
    candidate_count: int
    selected_parameters: ResearchParameters
    selection_metric: str
    selection_score: float
    train_metrics: ResearchMetrics
    test_metrics: ResearchMetrics
    buy_hold_return: float
    test_result: BacktestResult = field(repr=False, compare=False)
    cash_return: float = 0.0


@dataclass(frozen=True, slots=True)
class WalkForwardResult:
    config: WalkForwardConfig
    dataset: DatasetDiagnostics
    source_snapshot_count: int
    initial_equity: float
    folds: tuple[WalkForwardFold, ...]

    @property
    def final_equity(self) -> float:
        if not self.folds:
            return self.initial_equity
        return self.folds[-1].test_result.final_equity

    @property
    def oos_total_return(self) -> float:
        return self.final_equity / self.initial_equity - 1.0

    @property
    def buy_hold_total_return(self) -> float:
        compounded = 1.0
        for fold in self.folds:
            compounded *= 1.0 + fold.buy_hold_return
        return compounded - 1.0

    @property
    def cash_total_return(self) -> float:
        return 0.0

    @property
    def oos_trade_count(self) -> int:
        return sum(fold.test_metrics.trade_count for fold in self.folds)

    @property
    def oos_win_rate(self) -> float:
        trades = [trade for fold in self.folds for trade in fold.test_result.trades]
        if not trades:
            return 0.0
        return sum(trade.net_pnl > 0 for trade in trades) / len(trades)

    @property
    def oos_max_drawdown(self) -> float:
        peak = self.initial_equity
        maximum = 0.0
        for fold in self.folds:
            for point in fold.test_result.equity_curve:
                peak = max(peak, point.equity)
                if peak > 0:
                    maximum = max(maximum, (peak - point.equity) / peak)
        return maximum

    @property
    def tested_snapshot_count(self) -> int:
        return sum(fold.test_snapshot_count for fold in self.folds)

    @property
    def tested_snapshot_fraction(self) -> float:
        if self.source_snapshot_count <= 0:
            return 0.0
        return self.tested_snapshot_count / self.source_snapshot_count

    @property
    def selection_stability(self) -> float:
        if not self.folds:
            return 0.0
        counts = Counter(fold.selected_parameters for fold in self.folds)
        return max(counts.values()) / len(self.folds)

    @property
    def selected_parameter_counts(self) -> tuple[tuple[ResearchParameters, int], ...]:
        counts = Counter(fold.selected_parameters for fold in self.folds)
        return tuple(sorted(counts.items(), key=lambda item: (-item[1], repr(item[0]))))


def _strategy_for(parameters: ResearchParameters, base: BacktestConfig) -> StrategyConfig:
    return replace(
        base.strategy,
        target_dte=parameters.target_dte,
        short_delta=parameters.short_delta,
        wing_width=parameters.wing_width,
        profit_target_fraction=parameters.profit_target_fraction,
        stop_loss_credit_multiple=parameters.stop_loss_credit_multiple,
        exit_dte=parameters.exit_dte,
        max_risk_fraction=parameters.max_risk_fraction,
    )


def _fold_entry_filter(
    *,
    fold_end: datetime,
    strategy: StrategyConfig,
    external: EntryFilter | None,
) -> EntryFilter:
    """Block entries that cannot reach the configured time exit before the fold ends."""
    required_days = (
        strategy.target_dte + strategy.max_dte_deviation_days - strategy.exit_dte
    )
    latest_entry = fold_end.date() - timedelta(days=required_days)

    def allowed(snapshot: OptionChainSnapshot) -> bool:
        if snapshot.as_of > latest_entry:
            return False
        return external(snapshot) if external is not None else True

    return allowed


def _contains_forced_end_of_data(result: BacktestResult) -> bool:
    return any(trade.exit_reason is ExitReason.END_OF_DATA for trade in result.trades)


def _selection_score(run: ResearchRun, metric: str) -> float:
    score = float(getattr(run.metrics, metric))
    if math.isnan(score):
        return -math.inf
    return score


def _training_candidates(
    history: tuple[OptionChainSnapshot, ...],
    *,
    grid: ParameterGrid,
    base_config: BacktestConfig,
    selection: WalkForwardConfig,
    entry_filter: EntryFilter | None,
) -> tuple[ResearchRun, ...]:
    candidates: list[ResearchRun] = []
    fold_end = history[-1].observed_at
    for parameters in expand_parameter_grid(grid):
        strategy = _strategy_for(parameters, base_config)
        result = run_backtest(
            history,
            config=BacktestConfig(
                initial_equity=base_config.initial_equity,
                strategy=strategy,
                execution=base_config.execution,
            ),
            entry_filter=_fold_entry_filter(
                fold_end=fold_end,
                strategy=strategy,
                external=entry_filter,
            ),
        )
        if _contains_forced_end_of_data(result):
            continue
        metrics = summarize_result(result)
        if metrics.trade_count < selection.min_train_trades:
            continue
        candidates.append(
            ResearchRun(
                parameters=parameters,
                metrics=metrics,
                result=result,
            )
        )
    return tuple(candidates)


def run_walk_forward(
    snapshots: tuple[OptionChainSnapshot, ...] | list[OptionChainSnapshot],
    *,
    grid: ParameterGrid | None = None,
    base_config: BacktestConfig | None = None,
    config: WalkForwardConfig | None = None,
    entry_filter: EntryFilter | None = None,
    diagnostic_thresholds: DiagnosticThresholds | None = None,
) -> WalkForwardResult:
    """Select parameters only on past data, then evaluate frozen parameters on future folds."""
    history = validate_history(snapshots)
    grid = grid or ParameterGrid()
    base_config = base_config or BacktestConfig()
    config = config or WalkForwardConfig()

    dataset = diagnose_dataset(
        history,
        strategy=base_config.strategy,
        thresholds=diagnostic_thresholds,
    )
    if config.require_dataset_pass and not dataset.passed:
        details = "; ".join(dataset.issues) or "unknown dataset-quality failure"
        raise WalkForwardError(f"dataset failed research-quality gate: {details}")

    required = config.train_size + config.test_size
    if len(history) < required:
        raise WalkForwardError(
            f"walk-forward requires at least {required} snapshots; received {len(history)}"
        )
    if not expand_parameter_grid(grid):
        raise WalkForwardError("parameter grid contains no valid strategy combinations")

    folds: list[WalkForwardFold] = []
    oos_equity = base_config.initial_equity
    test_start_index = config.train_size
    fold_index = 1

    while test_start_index + config.test_size <= len(history):
        train_end_index = test_start_index
        train_start_index = 0 if config.anchored else train_end_index - config.train_size
        test_end_index = test_start_index + config.test_size

        train_history = history[train_start_index:train_end_index]
        test_history = history[test_start_index:test_end_index]
        candidates = _training_candidates(
            train_history,
            grid=grid,
            base_config=base_config,
            selection=config,
            entry_filter=entry_filter,
        )
        if not candidates:
            raise WalkForwardError(
                f"fold {fold_index} has no eligible training candidate after integrity filters"
            )

        ranked = rank_runs(candidates, metric=config.rank_by)
        selected = ranked[0]
        selected_strategy = _strategy_for(selected.parameters, base_config)
        test_result = run_backtest(
            test_history,
            config=BacktestConfig(
                initial_equity=oos_equity,
                strategy=selected_strategy,
                execution=base_config.execution,
            ),
            entry_filter=_fold_entry_filter(
                fold_end=test_history[-1].observed_at,
                strategy=selected_strategy,
                external=entry_filter,
            ),
        )
        if _contains_forced_end_of_data(test_result):
            raise WalkForwardError(
                f"fold {fold_index} produced end_of_data liquidation despite entry embargo"
            )

        test_metrics = summarize_result(test_result)
        buy_hold_return = test_history[-1].spot / test_history[0].spot - 1.0
        folds.append(
            WalkForwardFold(
                index=fold_index,
                train_start=train_history[0].observed_at,
                train_end=train_history[-1].observed_at,
                test_start=test_history[0].observed_at,
                test_end=test_history[-1].observed_at,
                train_snapshot_count=len(train_history),
                test_snapshot_count=len(test_history),
                candidate_count=len(candidates),
                selected_parameters=selected.parameters,
                selection_metric=config.rank_by,
                selection_score=_selection_score(selected, config.rank_by),
                train_metrics=selected.metrics,
                test_metrics=test_metrics,
                buy_hold_return=buy_hold_return,
                test_result=test_result,
            )
        )
        oos_equity = test_result.final_equity
        fold_index += 1
        test_start_index += config.step_size

    if not folds:
        raise WalkForwardError("history produced no complete out-of-sample folds")

    return WalkForwardResult(
        config=config,
        dataset=dataset,
        source_snapshot_count=len(history),
        initial_equity=base_config.initial_equity,
        folds=tuple(folds),
    )
