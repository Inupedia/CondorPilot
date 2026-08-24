from condorpilot.models import StrategyConfig
from condorpilot.risk import ExitAction, contracts_for_risk_budget, evaluate_exit


def test_position_size_respects_account_risk_budget() -> None:
    assert (
        contracts_for_risk_budget(
            account_equity=50_000,
            max_loss_per_contract=300,
            max_risk_fraction=0.02,
        )
        == 3
    )


def test_exit_policy_is_mechanical() -> None:
    config = StrategyConfig(
        profit_target_fraction=0.50,
        stop_loss_credit_multiple=2.0,
        exit_dte=21,
    )

    assert (
        evaluate_exit(entry_credit=1.00, current_close_debit=0.50, dte=35, config=config)
        is ExitAction.TAKE_PROFIT
    )
    assert (
        evaluate_exit(entry_credit=1.00, current_close_debit=2.00, dte=35, config=config)
        is ExitAction.STOP_LOSS
    )
    assert (
        evaluate_exit(entry_credit=1.00, current_close_debit=1.99, dte=35, config=config)
        is ExitAction.HOLD
    )
    assert (
        evaluate_exit(entry_credit=1.00, current_close_debit=1.00, dte=21, config=config)
        is ExitAction.TIME_EXIT
    )
    assert (
        evaluate_exit(entry_credit=1.00, current_close_debit=0.90, dte=30, config=config)
        is ExitAction.HOLD
    )
