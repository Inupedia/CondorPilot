from datetime import UTC, datetime

import pytest

from condorpilot.backtest import BacktestConfig, BacktestDataError, ExitReason, run_backtest
from condorpilot.execution import ExecutionConfig
from condorpilot.history import OptionChainSnapshot, build_synthetic_history
from condorpilot.models import StrategyConfig
from condorpilot.strategy import build_iron_condor

START = datetime(2026, 1, 2, 21, 0, tzinfo=UTC)


def test_backtest_closes_at_configured_time_exit() -> None:
    history = build_synthetic_history(
        symbol="SPY",
        start=START,
        spots=[100.0] * 25,
        volatility=0.25,
        target_dte=45,
        strike_increment=5.0,
    )
    strategy = StrategyConfig(
        target_dte=45,
        short_delta=0.15,
        wing_width=5.0,
        profit_target_fraction=0.999,
        stop_loss_credit_multiple=100.0,
        exit_dte=21,
        min_credit_to_width=0.0,
    )
    result = run_backtest(
        history,
        config=BacktestConfig(
            initial_equity=50_000,
            strategy=strategy,
            execution=ExecutionConfig(slippage_fraction=0, commission_per_contract_per_leg=0),
        ),
    )

    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.exit_reason is ExitReason.TIME_EXIT
    assert trade.holding_days == 24
    assert trade.contracts > 0
    assert result.final_equity == pytest.approx(50_000 + trade.net_pnl)


def test_backtest_forces_liquidation_at_end_of_data() -> None:
    history = build_synthetic_history(
        symbol="SPY",
        start=START,
        spots=[100.0, 100.0],
        volatility=0.25,
        target_dte=45,
        strike_increment=5.0,
    )
    strategy = StrategyConfig(
        target_dte=45,
        short_delta=0.15,
        wing_width=5.0,
        profit_target_fraction=0.999,
        stop_loss_credit_multiple=100.0,
        exit_dte=0,
        min_credit_to_width=0.0,
    )
    result = run_backtest(
        history,
        config=BacktestConfig(
            strategy=strategy,
            execution=ExecutionConfig(slippage_fraction=0, commission_per_contract_per_leg=0),
        ),
    )

    assert len(result.trades) == 1
    assert result.trades[0].exit_reason is ExitReason.END_OF_DATA
    assert result.trades[0].holding_days == 1


def test_missing_held_leg_fails_instead_of_interpolating_history() -> None:
    history = build_synthetic_history(
        symbol="SPY",
        start=START,
        spots=[100.0, 100.0],
        volatility=0.25,
        target_dte=45,
        strike_increment=5.0,
    )
    strategy = StrategyConfig(min_credit_to_width=0.0)
    selected = build_iron_condor(
        list(history[0].quotes),
        spot=history[0].spot,
        as_of=history[0].as_of,
        config=strategy,
    )
    broken_quotes = tuple(
        quote
        for quote in history[1].quotes
        if not (
            quote.expiration == selected.expiration
            and quote.strike == selected.short_put.strike
            and quote.option_type is selected.short_put.option_type
        )
    )
    broken_history = (
        history[0],
        OptionChainSnapshot(
            observed_at=history[1].observed_at,
            spot=history[1].spot,
            quotes=broken_quotes,
        ),
    )

    with pytest.raises(BacktestDataError, match="missing historical quote"):
        run_backtest(
            broken_history,
            config=BacktestConfig(
                strategy=strategy,
                execution=ExecutionConfig(slippage_fraction=0),
            ),
        )


def test_result_reports_drawdown_and_win_rate() -> None:
    history = build_synthetic_history(
        symbol="SPY",
        start=START,
        spots=[100.0] * 25,
        volatility=0.25,
        target_dte=45,
        strike_increment=5.0,
    )
    result = run_backtest(
        history,
        config=BacktestConfig(
            strategy=StrategyConfig(
                profit_target_fraction=0.999,
                stop_loss_credit_multiple=100.0,
                min_credit_to_width=0.0,
            ),
            execution=ExecutionConfig(slippage_fraction=0),
        ),
    )

    assert 0 <= result.max_drawdown < 1
    assert 0 <= result.win_rate <= 1
    assert result.total_return == pytest.approx(result.net_profit / result.initial_equity)
