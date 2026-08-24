import pytest

from condorpilot.models import OptionType
from condorpilot.pricing import black_scholes_price_delta


def test_atm_call_and_put_obey_put_call_parity_at_zero_rate() -> None:
    call_price, call_delta = black_scholes_price_delta(
        spot=100,
        strike=100,
        time_to_expiry=45 / 365,
        volatility=0.20,
        option_type=OptionType.CALL,
    )
    put_price, put_delta = black_scholes_price_delta(
        spot=100,
        strike=100,
        time_to_expiry=45 / 365,
        volatility=0.20,
        option_type=OptionType.PUT,
    )

    assert call_price - put_price == pytest.approx(0.0, abs=1e-10)
    assert call_delta - put_delta == pytest.approx(1.0, abs=1e-10)
    assert 0 < call_delta < 1
    assert -1 < put_delta < 0
