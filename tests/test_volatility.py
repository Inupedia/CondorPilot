from datetime import UTC, date, datetime, timedelta

import pytest

from condorpilot.history import OptionChainSnapshot
from condorpilot.models import OptionQuote, OptionType
from condorpilot.volatility import (
    VixObservation,
    VolatilityRegime,
    build_regime_entry_filter,
    build_volatility_regimes,
    estimate_atm_iv,
    parse_vix_csv,
)


def _snapshot(day: int, iv: float) -> OptionChainSnapshot:
    observed_at = datetime(2025, 1, day, 21, 0, tzinfo=UTC)
    expiration = observed_at.date() + timedelta(days=30)
    quotes = (
        OptionQuote(
            symbol="SPY",
            expiration=expiration,
            strike=99.0,
            option_type=OptionType.PUT,
            bid=2.0,
            ask=2.2,
            delta=-0.49,
            implied_volatility=iv,
        ),
        OptionQuote(
            symbol="SPY",
            expiration=expiration,
            strike=101.0,
            option_type=OptionType.CALL,
            bid=2.0,
            ask=2.2,
            delta=0.51,
            implied_volatility=iv + 0.01,
        ),
    )
    return OptionChainSnapshot(observed_at=observed_at, spot=100.0, quotes=quotes)


def test_parse_cboe_vix_csv_and_classify_levels() -> None:
    vix = parse_vix_csv(
        "DATE,OPEN,HIGH,LOW,CLOSE\n"
        "01/02/2025,12,13,11,12\n"
        "01/03/2025,20,21,19,20\n"
        "01/04/2025,28,29,27,28\n"
        "01/05/2025,40,41,39,40\n"
    )
    history = tuple(_snapshot(day, 0.20) for day in range(2, 6))

    observations = build_volatility_regimes(history, vix_history=vix, iv_lookback=1)

    assert [item.regime for item in observations] == [
        VolatilityRegime.LOW,
        VolatilityRegime.NORMAL,
        VolatilityRegime.HIGH,
        VolatilityRegime.STRESS,
    ]


def test_estimate_atm_iv_uses_nearest_half_delta_call_and_put() -> None:
    snapshot = _snapshot(2, 0.20)
    assert estimate_atm_iv(snapshot) == pytest.approx(0.205)


def test_regime_entry_filter_rejects_unlisted_dates() -> None:
    history = (_snapshot(2, 0.20), _snapshot(3, 0.20))
    observations = build_volatility_regimes(
        history,
        vix_history=(
            VixObservation(date(2025, 1, 2), 12.0),
            VixObservation(date(2025, 1, 3), 28.0),
        ),
        iv_lookback=1,
    )
    entry_filter = build_regime_entry_filter(
        observations,
        allowed_regimes=(VolatilityRegime.HIGH,),
    )

    assert entry_filter(history[0]) is False
    assert entry_filter(history[1]) is True
