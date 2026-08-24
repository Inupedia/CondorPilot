"""Parameter-sweep research utilities and risk-adjusted backtest metrics."""

from __future__ import annotations

import itertools
import math
import statistics
from dataclasses import dataclass, field

from condorpilot.backtest import BacktestConfig, BacktestResult, run_backtest
from condorpilot.history import OptionChainSnapshot, validate_history
from condorpilot.models import StrategyConfig


@dataclass(frozen=True, slots=True)
class ParameterGrid:
    """Discrete strategy values expanded into a deterministic Cartesian product."""

    target_dte: tuple[int, ...] = (30, 45, 60)
    short_delta: tuple[float, ...] = (0.10, 0.15, 0.20)
    wing_width: tuple[float, ...] = (5.0,)
    profit_target_fraction: tuple[float, ...] = (0.50,)
    stop_loss_credit_multiple: tuple[float, ...] = (2.0,)
    exit_dte: tuple[int, ...] = (14, 21)
    max_risk_fraction: tuple[float, ...] = (0.01,)

    def __post_init__(self) -> None:
        fields = (
            self.target_dte,
            self.short_delta,
            self.wing_width,
            self.profit_target_fraction,
            self.stop_loss_credit_multiple,
            self.exit_dte,
            self.max_risk_fraction,
        )
        if any(not values for values in fields):
            raise ValueError("parameter grid dimensions must not be empty")

    @property
    def raw_case_count(self) -> int:
        sizes = (
            len(self.target_dte),
            len(self.short_delta),
            len(self.wing_width),
            len(self.profit_target_fraction),
            len(self.stop_loss_credit_multiple),
            len(self.exit_dte),
            len(self.max_risk_fraction),
        )
        return math.prod(sizes)


@dataclass(frozen=True, slots=True)
class ResearchParameters:
    target_dte: int
    short_delta: float
    wing_width: float
    profit_target_fraction: float
    stop_loss_credit_multiple: float
    exit_dte: int
    max_risk_fraction: float

    def to_strategy_config(self, *, min_credit_to_width: float) -> StrategyConfig:
        return StrategyConfig(
            target_dte=self.target_dte,
            short_delta=self.short_delta,
            wing_width=self.wing_width,
            profit_target_fraction=self.profit_target_fraction,
            stop_loss_credit_multiple=self.stop_loss_credit_multiple,
            exit_dte=self.exit_dte,
            max_risk_fraction=self.max_risk_fraction,
            min_credit_to_width=min_credit_to_width,
        )


@dataclass(frozen=True, slots=True)
class ResearchMetrics:
    total_return: float
    cagr: float
    max_drawdown: float
    sharpe: float
    sortino: float
    win_rate: float
    profit_factor: float
    average_trade: float
    worst_trade: float
    exposure: float
    trade_count: int


@dataclass(frozen=True, slots=True)
class ResearchRun:
    parameters: ResearchParameters
    metrics: ResearchMetrics
    result: BacktestResult = field(repr=False, compare=False)


def expand_parameter_grid(grid: ParameterGrid) -> tuple[ResearchParameters, ...]:
    """Expand a grid, dropping combinations invalid under StrategyConfig constraints."""
    cases: list[ResearchParameters] = []
    for values in itertools.product(
        grid.target_dte,
        grid.short_delta,
        grid.wing_width,
        grid.profit_target_fraction,
        grid.stop_loss_credit_multiple,
        grid.exit_dte,
        grid.max_risk_fraction,
    ):
        case = ResearchParameters(*values)
        if case.exit_dte >= case.target_dte:
            continue
        cases.append(case)
    return tuple(cases)


def _equity_returns(result: BacktestResult) -> list[float]:
    if not result.equity_curve:
        return []
    values = [result.initial_equity, *(point.equity for point in result.equity_curve)]
    returns: list[float] = []
    for previous, current in zip(values, values[1:], strict=False):
        if previous > 0:
            returns.append(current / previous - 1.0)
    return returns


def _periods_per_year(result: BacktestResult, returns_count: int) -> float:
    if returns_count <= 0 or len(result.equity_curve) < 2:
        return 0.0
    elapsed_days = (
        result.equity_curve[-1].observed_at - result.equity_curve[0].observed_at
    ).total_seconds() / 86_400.0
    if elapsed_days <= 0:
        return 0.0
    return returns_count / (elapsed_days / 365.25)


def _cagr(result: BacktestResult) -> float:
    if len(result.equity_curve) < 2 or result.initial_equity <= 0 or result.final_equity <= 0:
        return 0.0
    elapsed_days = (
        result.equity_curve[-1].observed_at - result.equity_curve[0].observed_at
    ).total_seconds() / 86_400.0
    if elapsed_days <= 0:
        return 0.0
    return (result.final_equity / result.initial_equity) ** (365.25 / elapsed_days) - 1.0


def _exposure(result: BacktestResult) -> float:
    points = result.equity_curve
    if len(points) < 2:
        return 0.0
    total_seconds = 0.0
    exposed_seconds = 0.0
    for previous, current in zip(points, points[1:], strict=False):
        seconds = (current.observed_at - previous.observed_at).total_seconds()
        if seconds <= 0:
            continue
        total_seconds += seconds
        if previous.has_open_position:
            exposed_seconds += seconds
    if total_seconds == 0:
        return 0.0
    return exposed_seconds / total_seconds


def summarize_result(result: BacktestResult) -> ResearchMetrics:
    """Compute portfolio and trade metrics without third-party analytics dependencies."""
    returns = _equity_returns(result)
    periods_per_year = _periods_per_year(result, len(returns))
    sharpe = 0.0
    sortino = 0.0
    if returns and periods_per_year > 0:
        mean_return = statistics.fmean(returns)
        if len(returns) > 1:
            volatility = statistics.pstdev(returns)
            if volatility > 0:
                sharpe = mean_return / volatility * math.sqrt(periods_per_year)
        downside_deviation = math.sqrt(
            statistics.fmean(min(value, 0.0) ** 2 for value in returns)
        )
        if downside_deviation > 0:
            sortino = mean_return / downside_deviation * math.sqrt(periods_per_year)

    pnls = [trade.net_pnl for trade in result.trades]
    gross_profit = sum(value for value in pnls if value > 0)
    gross_loss = -sum(value for value in pnls if value < 0)
    if gross_loss > 0:
        profit_factor = gross_profit / gross_loss
    elif gross_profit > 0:
        profit_factor = math.inf
    else:
        profit_factor = 0.0

    return ResearchMetrics(
        total_return=result.total_return,
        cagr=_cagr(result),
        max_drawdown=result.max_drawdown,
        sharpe=sharpe,
        sortino=sortino,
        win_rate=result.win_rate,
        profit_factor=profit_factor,
        average_trade=statistics.fmean(pnls) if pnls else 0.0,
        worst_trade=min(pnls) if pnls else 0.0,
        exposure=_exposure(result),
        trade_count=len(result.trades),
    )


def run_parameter_sweep(
    snapshots: tuple[OptionChainSnapshot, ...] | list[OptionChainSnapshot],
    *,
    grid: ParameterGrid | None = None,
    base_config: BacktestConfig | None = None,
) -> tuple[ResearchRun, ...]:
    """Backtest every valid parameter combination against the exact same history."""
    history = validate_history(snapshots)
    grid = grid or ParameterGrid()
    base_config = base_config or BacktestConfig()
    runs: list[ResearchRun] = []

    for parameters in expand_parameter_grid(grid):
        strategy = parameters.to_strategy_config(
            min_credit_to_width=base_config.strategy.min_credit_to_width
        )
        result = run_backtest(
            history,
            config=BacktestConfig(
                initial_equity=base_config.initial_equity,
                strategy=strategy,
                execution=base_config.execution,
            ),
        )
        runs.append(
            ResearchRun(
                parameters=parameters,
                metrics=summarize_result(result),
                result=result,
            )
        )
    return tuple(runs)


def rank_runs(
    runs: tuple[ResearchRun, ...] | list[ResearchRun],
    *,
    metric: str = "sortino",
) -> tuple[ResearchRun, ...]:
    """Rank research runs by one supported metric, descending by default quality."""
    supported = {
        "total_return",
        "cagr",
        "sharpe",
        "sortino",
        "win_rate",
        "profit_factor",
        "average_trade",
    }
    if metric not in supported:
        raise ValueError(f"unsupported ranking metric {metric!r}; choose one of {sorted(supported)}")
    return tuple(sorted(runs, key=lambda run: getattr(run.metrics, metric), reverse=True))
