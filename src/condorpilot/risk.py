"""Position sizing and deterministic exit rules."""

from __future__ import annotations

import math
from enum import StrEnum

from condorpilot.models import StrategyConfig


class ExitAction(StrEnum):
    HOLD = "hold"
    TAKE_PROFIT = "take_profit"
    STOP_LOSS = "stop_loss"
    TIME_EXIT = "time_exit"


def contracts_for_risk_budget(
    *,
    account_equity: float,
    max_loss_per_contract: float,
    max_risk_fraction: float,
) -> int:
    """Return the largest whole-contract size within a fixed account-risk budget."""
    if account_equity <= 0:
        raise ValueError("account_equity must be positive")
    if max_loss_per_contract <= 0:
        raise ValueError("max_loss_per_contract must be positive")
    if not 0 < max_risk_fraction <= 1:
        raise ValueError("max_risk_fraction must be between 0 and 1")

    risk_budget = account_equity * max_risk_fraction
    return max(0, math.floor(risk_budget / max_loss_per_contract))


def evaluate_exit(
    *,
    entry_credit: float,
    current_close_debit: float,
    dte: int,
    config: StrategyConfig | None = None,
) -> ExitAction:
    """Evaluate the mechanical 50% TP / credit-multiple SL / time-exit policy.

    Prices are per share. For example, an entry credit of 1.00 and a current
    close debit of 0.50 represents a 50% profit target being reached.
    """
    if entry_credit <= 0:
        raise ValueError("entry_credit must be positive")
    if current_close_debit < 0:
        raise ValueError("current_close_debit must be non-negative")
    if dte < 0:
        raise ValueError("dte must be non-negative")

    config = config or StrategyConfig()
    profit = entry_credit - current_close_debit
    loss = current_close_debit - entry_credit

    if profit >= entry_credit * config.profit_target_fraction:
        return ExitAction.TAKE_PROFIT
    if loss >= entry_credit * config.stop_loss_credit_multiple:
        return ExitAction.STOP_LOSS
    if dte <= config.exit_dte:
        return ExitAction.TIME_EXIT
    return ExitAction.HOLD
