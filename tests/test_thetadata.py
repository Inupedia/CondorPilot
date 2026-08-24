from datetime import date

import pytest

from condorpilot.models import OptionType
from condorpilot.vendors.thetadata import ThetaDataClient, ThetaDataConfig, normalize_greeks_payload


def _row(
    *,
    strike: float,
    right: str,
    timestamp: str,
    bid: float,
    ask: float,
    delta: float,
    iv: float,
    spot: float,
) -> dict[str, object]:
    return {
        "symbol": "SPY",
        "expiration": "2025-02-21",
        "strike": strike,
        "right": right,
        "timestamp": timestamp,
        "bid": bid,
        "ask": ask,
        "delta": delta,
        "implied_vol": iv,
        "underlying_price": spot,
    }


def _payload() -> list[dict[str, object]]:
    contracts = (
        (90.0, "put", -0.10),
        (95.0, "put", -0.20),
        (105.0, "call", 0.20),
        (110.0, "call", 0.10),
    )
    rows: list[dict[str, object]] = []
    for strike, right, delta in contracts:
        rows.append(
            _row(
                strike=strike,
                right=right,
                timestamp="2025-01-10T15:30:00.000",
                bid=1.0,
                ask=1.2,
                delta=delta,
                iv=0.24,
                spot=100.0,
            )
        )
        rows.append(
            _row(
                strike=strike,
                right=right,
                timestamp="2025-01-10T16:00:00.000",
                bid=1.1,
                ask=1.3,
                delta=delta,
                iv=0.25,
                spot=101.0,
            )
        )
    return rows


def test_normalize_greeks_payload_keeps_latest_contract_rows() -> None:
    snapshot = normalize_greeks_payload(_payload(), symbol="SPY")

    assert snapshot.as_of == date(2025, 1, 10)
    assert snapshot.observed_at.hour == 16
    assert snapshot.observed_at.utcoffset() is not None
    assert snapshot.spot == pytest.approx(101.0)
    assert len(snapshot.quotes) == 4
    assert all(quote.bid == pytest.approx(1.1) for quote in snapshot.quotes)
    assert all(quote.implied_volatility == pytest.approx(0.25) for quote in snapshot.quotes)
    assert {quote.option_type for quote in snapshot.quotes} == {
        OptionType.PUT,
        OptionType.CALL,
    }


def test_client_requests_bulk_expirations_and_json() -> None:
    captured: dict[str, object] = {}

    def requester(path: str, params) -> object:
        captured["path"] = path
        captured["params"] = dict(params)
        return _payload()

    client = ThetaDataClient(
        ThetaDataConfig(interval="30m", max_dte=75, strike_range=30),
        requester=requester,
    )
    snapshot = client.fetch_day("spy", date(2025, 1, 10))

    assert snapshot.symbol == "SPY"
    assert captured["path"] == "option/history/greeks/all"
    params = captured["params"]
    assert isinstance(params, dict)
    assert params["expiration"] == "*"
    assert params["date"] == "2025-01-10"
    assert params["format"] == "json"
    assert params["max_dte"] == "75"
    assert params["strike_range"] == "30"
