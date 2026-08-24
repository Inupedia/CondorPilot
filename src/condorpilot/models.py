"""Core domain models for options and Iron Condor positions."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from enum import StrEnum


class OptionType(StrEnum):
    CALL = "call"
    PUT = "put"


@dataclass(frozen=True, slots=True)
class OptionQuote:
    """A normalized option quote independent of any broker or data vendor."""

    symbol: str
    expiration: date
    strike: float
    option_type: OptionType
    bid: float
    ask: float
    delta: float
    implied_volatility: float | None = None

    def __post_init__(self) -> None:
        if self.strike <= 0:
            raise ValueError("strike must be positive")
        if self.bid < 0 or self.ask < 0:
            raise ValueError("bid and ask must be non-negative")
        if self.ask < self.bid:
            raise ValueError("ask must be greater than or equal to bid")
        if not -1.0 <= self.delta <= 1.0:
            raise ValueError("delta must be between -1 and 1")
        if self.implied_volatility is not None:
            if not math.isfinite(self.implied_volatility) or self.implied_volatility < 0:
                raise ValueError("implied_volatility must be finite and non-negative")

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0


@dataclass(frozen=True, slots=True)
class IronCondor:
    """A short Iron Condor represented by four normalized option quotes."""

    long_put: OptionQuote
    short_put: OptionQuote
    short_call: OptionQuote
    long_call: OptionQuote
    multiplier: int = 100

    def __post_init__(self) -> None:
        legs = (self.long_put, self.short_put, self.short_call, self.long_call)
        expirations = {leg.expiration for leg in legs}
        symbols = {leg.symbol for leg in legs}
        if len(expirations) != 1:
            raise ValueError("all legs must share the same expiration")
        if len(symbols) != 1:
            raise ValueError("all legs must share the same symbol")
        if self.long_put.option_type is not OptionType.PUT:
            raise ValueError("long_put must be a put")
        if self.short_put.option_type is not OptionType.PUT:
            raise ValueError("short_put must be a put")
        if self.short_call.option_type is not OptionType.CALL:
            raise ValueError("short_call must be a call")
        if self.long_call.option_type is not OptionType.CALL:
            raise ValueError("long_call must be a call")
        if not (
            self.long_put.strike
            < self.short_put.strike
            < self.short_call.strike
            < self.long_call.strike
        ):
            raise ValueError("Iron Condor strikes must be ordered LP < SP < SC < LC")
        if self.multiplier <= 0:
            raise ValueError("multiplier must be positive")

    @property
    def symbol(self) -> str:
        return self.short_put.symbol

    @property
    def expiration(self) -> date:
        return self.short_put.expiration

    @property
    def put_width(self) -> float:
        return self.short_put.strike - self.long_put.strike

    @property
    def call_width(self) -> float:
        return self.long_call.strike - self.short_call.strike

    @property
    def max_width(self) -> float:
        return max(self.put_width, self.call_width)

    @property
    def net_credit(self) -> float:
        """Entry credit per share using quote mids."""
        return (
            self.short_put.mid
            + self.short_call.mid
            - self.long_put.mid
            - self.long_call.mid
        )

    @property
    def max_profit(self) -> float:
        return self.net_credit * self.multiplier

    @property
    def max_loss(self) -> float:
        return (self.max_width - self.net_credit) * self.multiplier

    @property
    def lower_breakeven(self) -> float:
        return self.short_put.strike - self.net_credit

    @property
    def upper_breakeven(self) -> float:
        return self.short_call.strike + self.net_credit

    def pnl_at_expiration(self, underlying_price: float) -> float:
        """Return expiration P/L in dollars for one condor."""
        if underlying_price < 0:
            raise ValueError("underlying_price must be non-negative")

        long_put = max(self.long_put.strike - underlying_price, 0.0)
        short_put = -max(self.short_put.strike - underlying_price, 0.0)
        short_call = -max(underlying_price - self.short_call.strike, 0.0)
        long_call = max(underlying_price - self.long_call.strike, 0.0)
        payoff_per_share = self.net_credit + long_put + short_put + short_call + long_call
        return payoff_per_share * self.multiplier


@dataclass(frozen=True, slots=True)
class StrategyConfig:
    """Mechanical defaults for the initial CondorPilot strategy."""

    target_dte: int = 45
    short_delta: float = 0.15
    wing_width: float = 5.0
    profit_target_fraction: float = 0.50
    stop_loss_credit_multiple: float = 2.0
    exit_dte: int = 21
    max_risk_fraction: float = 0.02
    min_credit_to_width: float = 0.10

    def __post_init__(self) -> None:
        if self.target_dte <= 0:
            raise ValueError("target_dte must be positive")
        if not 0 < self.short_delta < 0.5:
            raise ValueError("short_delta must be between 0 and 0.5")
        if self.wing_width <= 0:
            raise ValueError("wing_width must be positive")
        if not 0 < self.profit_target_fraction < 1:
            raise ValueError("profit_target_fraction must be between 0 and 1")
        if self.stop_loss_credit_multiple <= 0:
            raise ValueError("stop_loss_credit_multiple must be positive")
        if not 0 <= self.exit_dte < self.target_dte:
            raise ValueError("exit_dte must be between 0 and target_dte")
        if not 0 < self.max_risk_fraction <= 1:
            raise ValueError("max_risk_fraction must be between 0 and 1")
        if not 0 <= self.min_credit_to_width < 1:
            raise ValueError("min_credit_to_width must be between 0 and 1")
