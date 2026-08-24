from datetime import UTC, datetime

import pytest

from condorpilot.history import build_synthetic_history
from condorpilot.importers import load_option_chain_csv, save_option_chain_csv


def test_csv_round_trip_preserves_implied_volatility(tmp_path) -> None:
    history = build_synthetic_history(
        symbol="SPY",
        start=datetime(2025, 1, 2, 21, 0, tzinfo=UTC),
        spots=[100.0],
        volatility=0.27,
        target_dte=30,
        strike_increment=5.0,
    )
    path = tmp_path / "history.csv"

    save_option_chain_csv(history, path)
    loaded = load_option_chain_csv(path)

    assert loaded[0].quotes[0].implied_volatility == pytest.approx(0.27)


def test_v03_csv_without_iv_column_remains_readable(tmp_path) -> None:
    path = tmp_path / "legacy.csv"
    path.write_text(
        "observed_at,symbol,spot,expiration,strike,option_type,bid,ask,delta\n"
        "2025-01-02T21:00:00+00:00,SPY,100,2025-02-01,90,put,1,1.1,-0.1\n",
        encoding="utf-8",
    )

    loaded = load_option_chain_csv(path)

    assert loaded[0].quotes[0].implied_volatility is None
