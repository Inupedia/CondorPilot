from datetime import UTC, datetime

import pytest

from condorpilot.history import build_synthetic_history
from condorpilot.importers import CsvHistoryError, load_option_chain_csv, save_option_chain_csv


def test_csv_history_round_trip(tmp_path) -> None:
    history = build_synthetic_history(
        symbol="SPY",
        start=datetime(2025, 1, 2, 21, 0, tzinfo=UTC),
        spots=[100.0, 101.0],
        volatility=0.25,
        target_dte=30,
        expiration_interval_days=15,
        strike_increment=5.0,
    )
    path = tmp_path / "history.csv"

    save_option_chain_csv(history, path)
    loaded = load_option_chain_csv(path)

    assert len(loaded) == len(history)
    assert loaded[0].observed_at == history[0].observed_at
    assert loaded[0].spot == pytest.approx(history[0].spot)
    assert len(loaded[0].quotes) == len(history[0].quotes)
    assert loaded[0].quotes[0].strike == pytest.approx(history[0].quotes[0].strike)
    assert loaded[0].quotes[0].bid == pytest.approx(history[0].quotes[0].bid)


def test_csv_import_rejects_naive_timestamp(tmp_path) -> None:
    path = tmp_path / "bad.csv"
    path.write_text(
        "observed_at,symbol,spot,expiration,strike,option_type,bid,ask,delta\n"
        "2025-01-02T21:00:00,SPY,100,2025-02-01,90,put,1,1.1,-0.1\n",
        encoding="utf-8",
    )

    with pytest.raises(CsvHistoryError, match="timezone"):
        load_option_chain_csv(path)


def test_csv_import_rejects_duplicate_contract(tmp_path) -> None:
    path = tmp_path / "duplicate.csv"
    header = "observed_at,symbol,spot,expiration,strike,option_type,bid,ask,delta\n"
    row = "2025-01-02T21:00:00+00:00,SPY,100,2025-02-01,90,put,1,1.1,-0.1\n"
    path.write_text(header + row + row, encoding="utf-8")

    with pytest.raises(CsvHistoryError, match="duplicate contract"):
        load_option_chain_csv(path)
