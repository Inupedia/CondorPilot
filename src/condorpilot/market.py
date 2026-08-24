"""Market-data abstractions and a synthetic option-chain generator."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Protocol

from condorpilot.models import OptionQuote, OptionType
from condorpilot.pricing import black_scholes_price_delta


class OptionChainProvider(Protocol):
    """Broker/data-vendor boundary used by the strategy engine."""

    def get_chain(self, symbol: str) -> list[OptionQuote]:
        """Return normalized option quotes for a symbol."""


@dataclass(frozen=True, slots=True)
class SyntheticChainSpec:
    symbol: str = "SPY"
    spot: float = 650.0
    dte: int = 45
    volatility: float = 0.22
    risk_free_rate: float = 0.04
    strike_increment: float = 5.0
    strikes_each_side: int = 30
    bid_ask_spread: float = 0.10


def build_synthetic_chain(spec: SyntheticChainSpec, *, as_of: date) -> list[OptionQuote]:
    """Generate a deterministic option chain for demos and unit tests."""
    if spec.spot <= 0:
        raise ValueError("spot must be positive")
    if spec.dte <= 0:
        raise ValueError("dte must be positive")
    if spec.strike_increment <= 0:
        raise ValueError("strike_increment must be positive")
    if spec.strikes_each_side <= 0:
        raise ValueError("strikes_each_side must be positive")
    if spec.bid_ask_spread < 0:
        raise ValueError("bid_ask_spread must be non-negative")

    expiration = as_of + timedelta(days=spec.dte)
    center = round(spec.spot / spec.strike_increment) * spec.strike_increment
    strikes = [
        center + offset * spec.strike_increment
        for offset in range(-spec.strikes_each_side, spec.strikes_each_side + 1)
        if center + offset * spec.strike_increment > 0
    ]
    time_to_expiry = spec.dte / 365.0
    quotes: list[OptionQuote] = []

    for strike in strikes:
        for option_type in (OptionType.PUT, OptionType.CALL):
            mid, delta = black_scholes_price_delta(
                spot=spec.spot,
                strike=strike,
                time_to_expiry=time_to_expiry,
                volatility=spec.volatility,
                option_type=option_type,
                risk_free_rate=spec.risk_free_rate,
            )
            half_spread = spec.bid_ask_spread / 2.0
            bid = max(mid - half_spread, 0.0)
            ask = max(mid + half_spread, bid)
            quotes.append(
                OptionQuote(
                    symbol=spec.symbol,
                    expiration=expiration,
                    strike=float(strike),
                    option_type=option_type,
                    bid=round(bid, 4),
                    ask=round(ask, 4),
                    delta=delta,
                )
            )

    return quotes
