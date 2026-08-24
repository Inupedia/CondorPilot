"""Small dependency-free Black-Scholes helpers used for demos and tests."""

from __future__ import annotations

import math

from condorpilot.models import OptionType


def _norm_cdf(value: float) -> float:
    return 0.5 * (1.0 + math.erf(value / math.sqrt(2.0)))


def black_scholes_price_delta(
    *,
    spot: float,
    strike: float,
    time_to_expiry: float,
    volatility: float,
    option_type: OptionType,
    risk_free_rate: float = 0.0,
    dividend_yield: float = 0.0,
) -> tuple[float, float]:
    """Return Black-Scholes price and delta for a European option.

    This is intentionally lightweight. Production live trading should consume
    vendor/broker greeks rather than treating this helper as a market-data source.
    """
    if spot <= 0 or strike <= 0:
        raise ValueError("spot and strike must be positive")
    if time_to_expiry < 0:
        raise ValueError("time_to_expiry must be non-negative")
    if volatility < 0:
        raise ValueError("volatility must be non-negative")

    if time_to_expiry == 0 or volatility == 0:
        if option_type is OptionType.CALL:
            return max(spot - strike, 0.0), 1.0 if spot > strike else 0.0
        return max(strike - spot, 0.0), -1.0 if spot < strike else 0.0

    sqrt_t = math.sqrt(time_to_expiry)
    d1 = (
        math.log(spot / strike)
        + (risk_free_rate - dividend_yield + 0.5 * volatility**2) * time_to_expiry
    ) / (volatility * sqrt_t)
    d2 = d1 - volatility * sqrt_t
    discount_r = math.exp(-risk_free_rate * time_to_expiry)
    discount_q = math.exp(-dividend_yield * time_to_expiry)

    if option_type is OptionType.CALL:
        price = spot * discount_q * _norm_cdf(d1) - strike * discount_r * _norm_cdf(d2)
        delta = discount_q * _norm_cdf(d1)
    else:
        price = strike * discount_r * _norm_cdf(-d2) - spot * discount_q * _norm_cdf(-d1)
        delta = -discount_q * _norm_cdf(-d1)

    return max(price, 0.0), delta
