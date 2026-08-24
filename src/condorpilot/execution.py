"""Execution assumptions for historical and paper-trading simulations."""

from __future__ import annotations

from dataclasses import dataclass

from condorpilot.models import IronCondor, OptionQuote


class ExecutionDataError(ValueError):
    """Raised when quotes cannot produce a coherent executable mark."""


@dataclass(frozen=True, slots=True)
class ExecutionConfig:
    """Simple auditable fill model.

    slippage_fraction moves fills from mid (0.0) toward the natural bid/ask price (1.0).
    Commissions are charged per option contract per leg on both entry and exit.
    """

    slippage_fraction: float = 0.25
    commission_per_contract_per_leg: float = 0.65

    def __post_init__(self) -> None:
        if not 0 <= self.slippage_fraction <= 1:
            raise ValueError("slippage_fraction must be between 0 and 1")
        if self.commission_per_contract_per_leg < 0:
            raise ValueError("commission must be non-negative")

    def commission(self, contracts: int) -> float:
        if contracts < 0:
            raise ValueError("contracts must be non-negative")
        return contracts * 4 * self.commission_per_contract_per_leg


def _sell_fill(quote: OptionQuote, slippage_fraction: float) -> float:
    return quote.mid - slippage_fraction * (quote.mid - quote.bid)


def _buy_fill(quote: OptionQuote, slippage_fraction: float) -> float:
    return quote.mid + slippage_fraction * (quote.ask - quote.mid)


def entry_credit(condor: IronCondor, config: ExecutionConfig | None = None) -> float:
    """Return modeled opening credit per share for selling the condor."""
    config = config or ExecutionConfig()
    slip = config.slippage_fraction
    return (
        _sell_fill(condor.short_put, slip)
        + _sell_fill(condor.short_call, slip)
        - _buy_fill(condor.long_put, slip)
        - _buy_fill(condor.long_call, slip)
    )


def close_debit(condor: IronCondor, config: ExecutionConfig | None = None) -> float:
    """Return modeled closing debit per share for buying back the condor.

    A negative debit indicates internally inconsistent leg quotes. Older versions clamped that
    case to zero, which could fabricate a free close. Research now fails loudly instead.
    """
    config = config or ExecutionConfig()
    slip = config.slippage_fraction
    debit = (
        _buy_fill(condor.short_put, slip)
        + _buy_fill(condor.short_call, slip)
        - _sell_fill(condor.long_put, slip)
        - _sell_fill(condor.long_call, slip)
    )
    if debit < -1e-9:
        raise ExecutionDataError(
            f"modeled close debit is negative ({debit:.4f}); option quotes are inconsistent"
        )
    return max(0.0, debit)
