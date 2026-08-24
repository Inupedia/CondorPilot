from datetime import date

import pytest

from condorpilot.market import SyntheticChainSpec, build_synthetic_chain
from condorpilot.models import StrategyConfig
from condorpilot.strategy import NoTradeError, build_iron_condor


AS_OF = date(2026, 8, 24)


def test_strategy_selects_target_delta_and_exact_wings() -> None:
    chain = build_synthetic_chain(
        SyntheticChainSpec(
            symbol="SPY",
            spot=100.0,
            dte=45,
            volatility=0.25,
            strike_increment=5.0,
            strikes_each_side=15,
        ),
        as_of=AS_OF,
    )
    config = StrategyConfig(
        target_dte=45,
        short_delta=0.15,
        wing_width=5.0,
        min_credit_to_width=0.0,
    )

    condor = build_iron_condor(chain, spot=100.0, as_of=AS_OF, config=config)

    assert (condor.expiration - AS_OF).days == 45
    assert condor.put_width == pytest.approx(5.0)
    assert condor.call_width == pytest.approx(5.0)
    assert abs(abs(condor.short_put.delta) - 0.15) < 0.08
    assert abs(abs(condor.short_call.delta) - 0.15) < 0.08
    assert condor.net_credit > 0


def test_strategy_rejects_unattractive_credit() -> None:
    chain = build_synthetic_chain(
        SyntheticChainSpec(
            symbol="SPY",
            spot=100.0,
            dte=45,
            volatility=0.20,
            strike_increment=5.0,
            strikes_each_side=15,
        ),
        as_of=AS_OF,
    )
    config = StrategyConfig(min_credit_to_width=0.95)

    with pytest.raises(NoTradeError, match="credit-to-width"):
        build_iron_condor(chain, spot=100.0, as_of=AS_OF, config=config)
