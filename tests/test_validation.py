from datetime import UTC, datetime

import pytest

from condorpilot.backtest import BacktestConfig
from condorpilot.diagnostics import DiagnosticThresholds
from condorpilot.execution import ExecutionConfig
from condorpilot.history import build_synthetic_history
from condorpilot.models import StrategyConfig
from condorpilot.research import ParameterGrid
from condorpilot.validation import WalkForwardConfig, WalkForwardError, run_walk_forward

START = datetime(2026, 1, 5, 21, 0, tzinfo=UTC)


def _spots(days: int) -> list[float]:
    return [100.0 + index * 0.08 + (0.35 if index % 9 < 4 else -0.20) for index in range(days)]


def _history(spots: list[float] | None = None):
    values = spots or _spots(60)
    return build_synthetic_history(
        symbol="SPY",
        start=START,
        spots=values,
        volatility=0.25,
        target_dte=10,
        expiration_interval_days=7,
        strike_increment=1.0,
        spread=0.02,
    )


def _base() -> BacktestConfig:
    return BacktestConfig(
        initial_equity=50_000,
        strategy=StrategyConfig(
            target_dte=10,
            max_dte_deviation_days=4,
            short_delta=0.15,
            max_delta_deviation=0.08,
            wing_width=5.0,
            max_bid_ask_spread_fraction=1.0,
            profit_target_fraction=0.50,
            stop_loss_credit_multiple=2.0,
            exit_dte=3,
            max_risk_fraction=0.01,
            min_credit_to_width=0.0,
        ),
        execution=ExecutionConfig(
            slippage_fraction=0,
            commission_per_contract_per_leg=0,
        ),
    )


def _grid() -> ParameterGrid:
    return ParameterGrid(
        target_dte=(10,),
        short_delta=(0.10, 0.15),
        wing_width=(5.0,),
        profit_target_fraction=(0.50,),
        stop_loss_credit_multiple=(2.0,),
        exit_dte=(3,),
        max_risk_fraction=(0.01,),
    )


def _thresholds(**overrides) -> DiagnosticThresholds:
    values = dict(
        minimum_weekday_coverage=0.0,
        maximum_weekend_fraction=1.0,
        maximum_missing_iv_fraction=1.0,
        maximum_wide_quote_fraction=1.0,
        minimum_contract_continuity=0.0,
        minimum_dte_coverage=0.0,
        minimum_delta_coverage=0.0,
        minimum_wing_coverage=0.0,
        minimum_executable_coverage=0.0,
    )
    values.update(overrides)
    return DiagnosticThresholds(**values)


def _config(*, anchored: bool = False, require_dataset_pass: bool = True):
    return WalkForwardConfig(
        train_size=30,
        test_size=15,
        step_size=15,
        anchored=anchored,
        rank_by="sortino",
        min_train_trades=1,
        require_dataset_pass=require_dataset_pass,
    )


def test_walk_forward_uses_disjoint_train_and_future_test_windows() -> None:
    result = run_walk_forward(
        _history(),
        grid=_grid(),
        base_config=_base(),
        config=_config(),
        diagnostic_thresholds=_thresholds(),
    )

    assert len(result.folds) == 2
    assert len(result.dataset.fingerprint) == 64
    assert result.dataset.passed
    assert result.tested_snapshot_count == 30
    assert result.folds[0].train_end < result.folds[0].test_start
    assert result.folds[1].train_end < result.folds[1].test_start
    assert 1 <= result.folds[0].candidate_count <= 2
    assert result.folds[1].test_result.initial_equity == pytest.approx(
        result.folds[0].test_result.final_equity
    )
    assert 0 <= result.selection_stability <= 1
    assert result.oos_trade_count >= 1
    assert result.oos_max_drawdown >= 0


def test_future_changes_do_not_change_first_fold_selection_or_result() -> None:
    original_spots = _spots(60)
    changed_spots = original_spots[:45] + [value + 12.0 for value in original_spots[45:]]

    original = run_walk_forward(
        _history(original_spots),
        grid=_grid(),
        base_config=_base(),
        config=_config(require_dataset_pass=False),
        diagnostic_thresholds=_thresholds(),
    )
    changed = run_walk_forward(
        _history(changed_spots),
        grid=_grid(),
        base_config=_base(),
        config=_config(require_dataset_pass=False),
        diagnostic_thresholds=_thresholds(),
    )

    assert original.dataset.fingerprint != changed.dataset.fingerprint
    assert original.folds[0].selected_parameters == changed.folds[0].selected_parameters
    assert original.folds[0].train_metrics == changed.folds[0].train_metrics
    assert original.folds[0].test_metrics == changed.folds[0].test_metrics


def test_anchored_mode_expands_training_window_while_rolling_mode_moves_it() -> None:
    history = _history()
    rolling = run_walk_forward(
        history,
        grid=_grid(),
        base_config=_base(),
        config=_config(anchored=False),
        diagnostic_thresholds=_thresholds(),
    )
    anchored = run_walk_forward(
        history,
        grid=_grid(),
        base_config=_base(),
        config=_config(anchored=True),
        diagnostic_thresholds=_thresholds(),
    )

    assert rolling.folds[1].train_start > rolling.folds[0].train_start
    assert anchored.folds[1].train_start == anchored.folds[0].train_start
    assert anchored.folds[1].train_snapshot_count > anchored.folds[0].train_snapshot_count


def test_walk_forward_reports_cash_and_price_only_buy_hold_benchmarks() -> None:
    result = run_walk_forward(
        _history(),
        grid=_grid(),
        base_config=_base(),
        config=_config(),
        diagnostic_thresholds=_thresholds(),
    )

    assert result.cash_total_return == 0.0
    assert all(fold.cash_return == 0.0 for fold in result.folds)
    assert all(fold.buy_hold_return > 0 for fold in result.folds)
    assert result.buy_hold_total_return > 0


def test_walk_forward_requires_enough_history() -> None:
    with pytest.raises(WalkForwardError, match="requires at least"):
        run_walk_forward(
            _history(_spots(40)),
            grid=_grid(),
            base_config=_base(),
            config=_config(),
            diagnostic_thresholds=_thresholds(),
        )


def test_walk_forward_can_enforce_dataset_quality_gate() -> None:
    bad_history = build_synthetic_history(
        symbol="SPY",
        start=START,
        spots=_spots(60),
        volatility=0.25,
        target_dte=100,
        expiration_interval_days=30,
        strike_increment=1.0,
        spread=0.02,
    )
    thresholds = _thresholds(minimum_dte_coverage=0.80)

    with pytest.raises(WalkForwardError, match="dataset failed research-quality gate"):
        run_walk_forward(
            bad_history,
            grid=_grid(),
            base_config=_base(),
            config=_config(),
            diagnostic_thresholds=thresholds,
        )


def test_walk_forward_rejects_overlapping_oos_windows() -> None:
    with pytest.raises(ValueError, match="avoid overlapping OOS folds"):
        WalkForwardConfig(train_size=30, test_size=15, step_size=10)
