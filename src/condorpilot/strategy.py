"""Mechanical Iron Condor selection logic."""

from __future__ import annotations

from datetime import date

from condorpilot.models import IronCondor, OptionQuote, OptionType, StrategyConfig


class NoTradeError(RuntimeError):
    """Raised when the option chain does not satisfy the configured entry rules."""


def _closest_expiration(
    quotes: list[OptionQuote], *, as_of: date, target_dte: int, minimum_dte: int
) -> date:
    expirations = sorted({quote.expiration for quote in quotes})
    eligible = [
        expiration
        for expiration in expirations
        if (expiration - as_of).days > minimum_dte
    ]
    if not eligible:
        raise NoTradeError("no eligible expiration is available")
    return min(eligible, key=lambda expiration: abs((expiration - as_of).days - target_dte))


def _short_by_delta(
    quotes: list[OptionQuote],
    *,
    option_type: OptionType,
    spot: float,
    target_delta: float,
) -> OptionQuote:
    if option_type is OptionType.PUT:
        candidates = [
            quote
            for quote in quotes
            if quote.option_type is OptionType.PUT
            and quote.strike < spot
            and quote.bid > 0
            and quote.delta < 0
        ]
    else:
        candidates = [
            quote
            for quote in quotes
            if quote.option_type is OptionType.CALL
            and quote.strike > spot
            and quote.bid > 0
            and quote.delta > 0
        ]

    if not candidates:
        raise NoTradeError(f"no liquid OTM {option_type.value} candidates are available")
    return min(candidates, key=lambda quote: abs(abs(quote.delta) - target_delta))


def _quote_at_strike(
    quotes: list[OptionQuote], *, option_type: OptionType, strike: float
) -> OptionQuote:
    for quote in quotes:
        if quote.option_type is option_type and abs(quote.strike - strike) < 1e-9:
            return quote
    raise NoTradeError(
        f"required {option_type.value} wing at strike {strike:g} is not available"
    )


def build_iron_condor(
    quotes: list[OptionQuote],
    *,
    spot: float,
    as_of: date,
    config: StrategyConfig | None = None,
) -> IronCondor:
    """Select one Iron Condor from a normalized option chain.

    The first strategy version intentionally stays simple and auditable:
    - expiration nearest target DTE, but still beyond the configured time exit;
    - short put/call nearest the configured absolute delta;
    - exact-width protective wings;
    - reject trades whose entry credit is too small for the defined risk width.
    """
    if spot <= 0:
        raise ValueError("spot must be positive")
    if not quotes:
        raise NoTradeError("option chain is empty")

    config = config or StrategyConfig()
    expiration = _closest_expiration(
        quotes,
        as_of=as_of,
        target_dte=config.target_dte,
        minimum_dte=config.exit_dte,
    )
    chain = [quote for quote in quotes if quote.expiration == expiration]

    short_put = _short_by_delta(
        chain,
        option_type=OptionType.PUT,
        spot=spot,
        target_delta=config.short_delta,
    )
    short_call = _short_by_delta(
        chain,
        option_type=OptionType.CALL,
        spot=spot,
        target_delta=config.short_delta,
    )
    long_put = _quote_at_strike(
        chain,
        option_type=OptionType.PUT,
        strike=short_put.strike - config.wing_width,
    )
    long_call = _quote_at_strike(
        chain,
        option_type=OptionType.CALL,
        strike=short_call.strike + config.wing_width,
    )

    condor = IronCondor(
        long_put=long_put,
        short_put=short_put,
        short_call=short_call,
        long_call=long_call,
    )
    if condor.net_credit <= 0:
        raise NoTradeError("selected condor does not produce a net credit")
    if condor.net_credit / condor.max_width < config.min_credit_to_width:
        raise NoTradeError(
            "entry credit is below the configured credit-to-width threshold"
        )
    return condor
