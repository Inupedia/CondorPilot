import math
from datetime import UTC, datetime

import pytest

from condorpilot.backtest import BacktestConfig
from condorpilot.execution import ExecutionConfig
from condorpilot.history import build_synthetic_history
from condorpilot.models import StrategyConfig
from condorpilot.research import (
    ParameterGrid,
    expand_parameter_grid,
    rank_runs,
    run_parameter_sweep,
)


def test_parameter_grid_drops_invalid_exit_dte_cases() -> None:
    grid = ParameterGrid(
        target_dte=(14, 30),
        short_delta=(0.15,),
        wing_width=(5.0,),
        profit_target_fraction=(0.50,),
        stop_loss_credit_multiple=(2.0,),
        exit_dte=(14, 21),
        max_risk_fraction=(0.01,),
    )

    cases = expand_parameter_grid(grid)

    assert [(case.target_dte, case.exit_dte) for case in cases] == [(30, 14), (30, 21)]


def test_parameter_sweep_uses_same_history_and_produces_metrics() -> None:
    spots = [100.0 * (1.0 + 0.008 * math.sin(index / 5.0)) for index in range(70)]
    history = build_synthetic_history(
        symbol="SPY",
        start=datetime(2025, 1, 2, 21, 0, tzinfo=UTC),
        spots=spots,
        volatility=0.25,
        target_dte=30,
        expiration_interval_days=15,
        strike_increment=5.0,
    )
    grid = ParameterGrid(
        target_dte=(30, 45),
        short_delta=(0.10, 0.15),
        wing_width=(5.0,),
        profit_target_fraction=(0.50,),
        stop_loss_credit_multiple=(2.0,),
        exit_dte=(14,),
        max_risk_fraction=(0.01,),
    )
    base = BacktestConfig(
        initial_equity=50_000.0,
        strategy=StrategyConfig(min_credit_to_width=0.0),
        execution=ExecutionConfig(slippage_fraction=0.0, commission_per_contract_per_leg=0.0),
    )

    runs = run_parameter_sweep(history, grid=grid, base_config=base)

    assert len(runs) == 4
    assert {run.parameters.target_dte for run in runs} == {30, 45}
    assert {run.parameters.short_delta for run in runs} == {0.10, 0.15}
    assert all(run.result.initial_equity == 50_000.0 for run in runs)
    assert all(len(run.result.equity_curve) == len(history) for run in runs)
    assert all(0.0 <= run.metrics.exposure <= 1.0 for run in runs)
    assert all(run.metrics.trade_count == len(run.result.trades) for run in runs)


def test_rank_runs_rejects_unknown_metric() -> None:
    with pytest.raises(ValueError, match="unsupported ranking metric"):
        rank_runs([], metric="magic")
