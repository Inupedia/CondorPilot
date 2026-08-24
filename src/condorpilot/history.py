"""Timestamped historical option-chain data models and synthetic research fixtures."""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from condorpilot.models import OptionQuote, OptionType
from condorpilot.pricing import black_scholes_price_delta


class HistoricalDataError(ValueError):
    """Raised when historical option data is incomplete or internally inconsistent."""


@dataclass(frozen=True, slots=True)
class OptionChainSnapshot:
    """A point-in-time normalized option chain with the underlying spot price."""

    observed_at: datetime
    spot: float
    quotes: tuple[OptionQuote, ...]

    def __post_init__(self) -> None:
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise HistoricalDataError("observed_at must be timezone-aware")
        if self.spot <= 0:
            raise HistoricalDataError("spot must be positive")
        if not self.quotes:
            raise HistoricalDataError("snapshot must contain at least one option quote")
        symbols = {quote.symbol for quote in self.quotes}
        if len(symbols) != 1:
            raise HistoricalDataError("all quotes in a snapshot must share one symbol")

    @property
    def symbol(self) -> str:
        return self.quotes[0].symbol

    @property
    def as_of(self) -> date:
        return self.observed_at.date()

    def quote_for(
        self,
        *,
        expiration: date,
        strike: float,
        option_type: OptionType,
    ) -> OptionQuote:
        """Return an exact contract quote or fail loudly instead of interpolating history."""
        for quote in self.quotes:
            if (
                quote.expiration == expiration
                and quote.option_type is option_type
                and abs(quote.strike - strike) < 1e-9
            ):
                return quote
        raise HistoricalDataError(
            "missing historical quote for "
            f"{self.symbol} {expiration} {strike:g} {option_type.value}"
        )


def validate_history(snapshots: Iterable[OptionChainSnapshot]) -> tuple[OptionChainSnapshot, ...]:
    """Validate chronology and symbol consistency, returning an immutable history."""
    history = tuple(snapshots)
    if not history:
        raise HistoricalDataError("history must contain at least one snapshot")

    symbols = {snapshot.symbol for snapshot in history}
    if len(symbols) != 1:
        raise HistoricalDataError("all snapshots must share one symbol")

    timestamps = [snapshot.observed_at for snapshot in history]
    if timestamps != sorted(timestamps):
        raise HistoricalDataError("snapshots must be ordered chronologically")
    if len(set(timestamps)) != len(timestamps):
        raise HistoricalDataError("snapshot timestamps must be unique")
    return history


def build_synthetic_history(
    *,
    symbol: str,
    start: datetime,
    spots: Sequence[float],
    volatility: float = 0.22,
    risk_free_rate: float = 0.04,
    target_dte: int = 45,
    expiration_interval_days: int = 30,
    strike_increment: float = 5.0,
    spread: float = 0.10,
) -> tuple[OptionChainSnapshot, ...]:
    """Build deterministic multi-expiration history for demos and engine tests.

    This helper deliberately creates synthetic research data. It is not a substitute for
    timestamped vendor option-chain history when evaluating a strategy for real use.
    """
    if start.tzinfo is None or start.utcoffset() is None:
        raise ValueError("start must be timezone-aware")
    if not spots or any(spot <= 0 for spot in spots):
        raise ValueError("spots must contain positive prices")
    if volatility <= 0:
        raise ValueError("volatility must be positive")
    if target_dte <= 0 or expiration_interval_days <= 0:
        raise ValueError("DTE and expiration interval must be positive")
    if strike_increment <= 0 or spread < 0:
        raise ValueError("strike increment must be positive and spread non-negative")

    start_date = start.date()
    end_date = start_date + timedelta(days=len(spots) - 1)
    last_needed_expiration = end_date + timedelta(days=target_dte + expiration_interval_days)

    expirations: list[date] = []
    expiration = start_date + timedelta(days=target_dte)
    while expiration <= last_needed_expiration:
        expirations.append(expiration)
        expiration += timedelta(days=expiration_interval_days)

    min_strike = math.floor(min(spots) * 0.60 / strike_increment) * strike_increment
    max_strike = math.ceil(max(spots) * 1.40 / strike_increment) * strike_increment
    min_strike = max(strike_increment, min_strike)
    strike_count = int(round((max_strike - min_strike) / strike_increment))
    strikes = [min_strike + index * strike_increment for index in range(strike_count + 1)]

    snapshots: list[OptionChainSnapshot] = []
    for index, spot in enumerate(spots):
        observed_at = start + timedelta(days=index)
        as_of = observed_at.date()
        quotes: list[OptionQuote] = []

        for expiration in expirations:
            dte = (expiration - as_of).days
            if dte < 0:
                continue
            time_to_expiry = dte / 365.0
            for strike in strikes:
                for option_type in (OptionType.PUT, OptionType.CALL):
                    price, delta = black_scholes_price_delta(
                        spot=spot,
                        strike=strike,
                        time_to_expiry=time_to_expiry,
                        volatility=volatility,
                        option_type=option_type,
                        risk_free_rate=risk_free_rate,
                    )
                    half_spread = spread / 2.0
                    bid = max(0.0, price - half_spread)
                    ask = max(bid, price + half_spread)
                    quotes.append(
                        OptionQuote(
                            symbol=symbol,
                            expiration=expiration,
                            strike=strike,
                            option_type=option_type,
                            bid=bid,
                            ask=ask,
                            delta=delta,
                        )
                    )

        snapshots.append(
            OptionChainSnapshot(
                observed_at=observed_at,
                spot=spot,
                quotes=tuple(quotes),
            )
        )

    return validate_history(snapshots)
