from datetime import date

import pytest

from condorpilot.execution import ExecutionConfig, ExecutionDataError, close_debit, entry_credit
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


def condor() -> IronCondor:
    return IronCondor(
        long_put=quote(90, OptionType.PUT, 0.40, -0.08),
        short_put=quote(95, OptionType.PUT, 1.40, -0.15),
        short_call=quote(105, OptionType.CALL, 1.30, 0.15),
        long_call=quote(110, OptionType.CALL, 0.30, 0.08),
    )


def test_slippage_moves_fills_from_mid_toward_natural_prices() -> None:
    trade = condor()

    assert entry_credit(trade, ExecutionConfig(slippage_fraction=0)) == pytest.approx(2.0)
    assert close_debit(trade, ExecutionConfig(slippage_fraction=0)) == pytest.approx(2.0)
    assert entry_credit(trade, ExecutionConfig(slippage_fraction=1)) == pytest.approx(1.8)
    assert close_debit(trade, ExecutionConfig(slippage_fraction=1)) == pytest.approx(2.2)


def test_negative_close_debit_is_data_error_not_free_close() -> None:
    broken = IronCondor(
        long_put=OptionQuote("SPY", EXPIRATION, 90, OptionType.PUT, 1.00, 1.00, -0.08),
        short_put=OptionQuote("SPY", EXPIRATION, 95, OptionType.PUT, 0.00, 0.00, -0.15),
        short_call=OptionQuote("SPY", EXPIRATION, 105, OptionType.CALL, 0.00, 0.00, 0.15),
        long_call=OptionQuote("SPY", EXPIRATION, 110, OptionType.CALL, 1.00, 1.00, 0.08),
    )

    with pytest.raises(ExecutionDataError, match="negative"):
        close_debit(broken, ExecutionConfig(slippage_fraction=0))


def test_commissions_are_charged_per_leg_and_contract() -> None:
    config = ExecutionConfig(commission_per_contract_per_leg=0.65)

    assert config.commission(0) == 0
    assert config.commission(1) == pytest.approx(2.60)
    assert config.commission(3) == pytest.approx(7.80)
