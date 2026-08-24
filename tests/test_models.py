from datetime import date

import pytest

from condorpilot.models import IronCondor, OptionQuote, OptionType


EXPIRATION = date(2026, 10, 16)


def quote(strike: float, option_type: OptionType, mid: float, delta: float) -> OptionQuote:
    return OptionQuote(
        symbol="SPY",
        expiration=EXPIRATION,
        strike=strike,
        option_type=option_type,
        bid=mid - 0.05,
        ask=mid + 0.05,
        delta=delta,
    )


def test_iron_condor_risk_and_breakevens() -> None:
    condor = IronCondor(
        long_put=quote(90, OptionType.PUT, 0.40, -0.08),
        short_put=quote(95, OptionType.PUT, 1.40, -0.15),
        short_call=quote(105, OptionType.CALL, 1.30, 0.15),
        long_call=quote(110, OptionType.CALL, 0.30, 0.08),
    )

    assert condor.net_credit == pytest.approx(2.0)
    assert condor.max_profit == pytest.approx(200.0)
    assert condor.max_loss == pytest.approx(300.0)
    assert condor.lower_breakeven == pytest.approx(93.0)
    assert condor.upper_breakeven == pytest.approx(107.0)


def test_expiration_pnl_is_bounded_by_defined_risk() -> None:
    condor = IronCondor(
        long_put=quote(90, OptionType.PUT, 0.40, -0.08),
        short_put=quote(95, OptionType.PUT, 1.40, -0.15),
        short_call=quote(105, OptionType.CALL, 1.30, 0.15),
        long_call=quote(110, OptionType.CALL, 0.30, 0.08),
    )

    assert condor.pnl_at_expiration(100) == pytest.approx(200.0)
    assert condor.pnl_at_expiration(80) == pytest.approx(-300.0)
    assert condor.pnl_at_expiration(120) == pytest.approx(-300.0)
