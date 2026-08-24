"""CSV import/export helpers for timestamped option-chain history."""

from __future__ import annotations

import csv
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

from condorpilot.history import HistoricalDataError, OptionChainSnapshot, validate_history
from condorpilot.models import OptionQuote, OptionType

CSV_COLUMNS = (
    "observed_at",
    "symbol",
    "spot",
    "expiration",
    "strike",
    "option_type",
    "bid",
    "ask",
    "delta",
)


class CsvHistoryError(HistoricalDataError):
    """Raised when a CSV history file is malformed or internally inconsistent."""


def _parse_datetime(value: str, *, row_number: int) -> datetime:
    normalized = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise CsvHistoryError(f"row {row_number}: invalid observed_at {value!r}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CsvHistoryError(f"row {row_number}: observed_at must include a timezone")
    return parsed


def _parse_float(value: str, *, field: str, row_number: int) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise CsvHistoryError(f"row {row_number}: invalid {field} {value!r}") from exc


def load_option_chain_csv(path: str | Path) -> tuple[OptionChainSnapshot, ...]:
    """Load a normalized long-form option-chain CSV into immutable snapshots.

    Each CSV row is one option quote. Rows sharing ``observed_at`` form one snapshot.
    The importer normalizes snapshot ordering but rejects duplicate contracts and inconsistent
    spot/symbol values within the same timestamp.
    """
    csv_path = Path(path)
    groups: dict[datetime, list[OptionQuote]] = defaultdict(list)
    metadata: dict[datetime, tuple[str, float]] = {}
    seen_contracts: dict[datetime, set[tuple[date, float, OptionType]]] = defaultdict(set)

    with csv_path.open("r", newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise CsvHistoryError("CSV file is missing a header")
        missing = [column for column in CSV_COLUMNS if column not in reader.fieldnames]
        if missing:
            raise CsvHistoryError(f"CSV header is missing required columns: {', '.join(missing)}")

        for row_number, row in enumerate(reader, start=2):
            observed_at = _parse_datetime(row["observed_at"], row_number=row_number)
            symbol = row["symbol"].strip()
            if not symbol:
                raise CsvHistoryError(f"row {row_number}: symbol must not be empty")
            spot = _parse_float(row["spot"], field="spot", row_number=row_number)
            try:
                expiration = date.fromisoformat(row["expiration"].strip())
            except ValueError as exc:
                raise CsvHistoryError(
                    f"row {row_number}: invalid expiration {row['expiration']!r}"
                ) from exc
            strike = _parse_float(row["strike"], field="strike", row_number=row_number)
            bid = _parse_float(row["bid"], field="bid", row_number=row_number)
            ask = _parse_float(row["ask"], field="ask", row_number=row_number)
            delta = _parse_float(row["delta"], field="delta", row_number=row_number)
            try:
                option_type = OptionType(row["option_type"].strip().lower())
            except ValueError as exc:
                raise CsvHistoryError(
                    f"row {row_number}: option_type must be 'put' or 'call'"
                ) from exc

            existing = metadata.get(observed_at)
            if existing is None:
                metadata[observed_at] = (symbol, spot)
            elif existing[0] != symbol or abs(existing[1] - spot) > 1e-9:
                snapshot = observed_at.isoformat()
                raise CsvHistoryError(
                    f"row {row_number}: symbol/spot differs within snapshot {snapshot}"
                )

            key = (expiration, strike, option_type)
            if key in seen_contracts[observed_at]:
                raise CsvHistoryError(
                    f"row {row_number}: duplicate contract for {expiration} {strike:g} "
                    f"{option_type.value}"
                )
            seen_contracts[observed_at].add(key)
            groups[observed_at].append(
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

    if not groups:
        raise CsvHistoryError("CSV file contains no option quotes")

    snapshots = [
        OptionChainSnapshot(
            observed_at=observed_at,
            spot=metadata[observed_at][1],
            quotes=tuple(groups[observed_at]),
        )
        for observed_at in sorted(groups)
    ]
    return validate_history(snapshots)


def save_option_chain_csv(
    snapshots: tuple[OptionChainSnapshot, ...] | list[OptionChainSnapshot],
    path: str | Path,
) -> None:
    """Write normalized snapshots to the documented long-form CSV schema."""
    history = validate_history(snapshots)
    csv_path = Path(path)
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for snapshot in history:
            for quote in snapshot.quotes:
                writer.writerow(
                    {
                        "observed_at": snapshot.observed_at.isoformat(),
                        "symbol": quote.symbol,
                        "spot": f"{snapshot.spot:.10g}",
                        "expiration": quote.expiration.isoformat(),
                        "strike": f"{quote.strike:.10g}",
                        "option_type": quote.option_type.value,
                        "bid": f"{quote.bid:.10g}",
                        "ask": f"{quote.ask:.10g}",
                        "delta": f"{quote.delta:.10g}",
                    }
                )
