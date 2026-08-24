from datetime import UTC, datetime

from condorpilot.backtest import BacktestConfig, ExitReason, run_backtest
from condorpilot.execution import ExecutionConfig
from condorpilot.history import build_synthetic_history
from condorpilot.models import StrategyConfig


def test_entry_filter_only_controls_new_entries() -> None:
    history = build_synthetic_history(
        symbol="SPY",
        start=datetime(2026, 1, 2, 21, 0, tzinfo=UTC),
        spots=[100.0] * 25,
        volatility=0.25,
        target_dte=45,
        strike_increment=5.0,
    )
    strategy = StrategyConfig(
        target_dte=45,
        profit_target_fraction=0.999,
        stop_loss_credit_multiple=100.0,
        exit_dte=21,
        min_credit_to_width=0.0,
    )
    config = BacktestConfig(
        strategy=strategy,
        execution=ExecutionConfig(
            slippage_fraction=0,
            commission_per_contract_per_leg=0,
        ),
    )
    first_day = history[0].as_of

    result = run_backtest(
        history,
        config=config,
        entry_filter=lambda snapshot: snapshot.as_of == first_day,
    )

    assert len(result.trades) == 1
    assert result.trades[0].exit_reason is ExitReason.TIME_EXIT
    assert result.trades[0].holding_days == 24


def test_entry_filter_can_block_all_entries() -> None:
    history = build_synthetic_history(
        symbol="SPY",
        start=datetime(2026, 1, 2, 21, 0, tzinfo=UTC),
        spots=[100.0] * 5,
        volatility=0.25,
        target_dte=45,
        strike_increment=5.0,
    )

    result = run_backtest(history, entry_filter=lambda snapshot: False)

    assert result.trades == ()
    assert all(not point.has_open_position for point in result.equity_curve)
