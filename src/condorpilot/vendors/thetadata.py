"""ThetaData v3 adapter for normalized historical option-chain snapshots."""

from __future__ import annotations

import json
import math
import statistics
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from condorpilot.history import OptionChainSnapshot, validate_history
from condorpilot.models import OptionQuote, OptionType

JsonRequester = Callable[[str, Mapping[str, str]], object]


class ThetaDataError(RuntimeError):
    """Raised when ThetaData cannot be queried or normalized safely."""


class ThetaDataNoData(ThetaDataError):
    """Raised when a requested market date has no usable option data."""


@dataclass(frozen=True, slots=True)
class ThetaDataConfig:
    """Settings for the local Theta Terminal v3 REST API."""

    base_url: str = "http://127.0.0.1:25503/v3"
    interval: str = "30m"
    start_time: str = "15:30:00"
    end_time: str = "16:00:00"
    max_dte: int = 90
    strike_range: int = 40
    market_timezone: str = "America/New_York"
    spot_dispersion_fraction: float = 0.02
    minimum_quotes: int = 4
    timeout: float = 60.0

    def __post_init__(self) -> None:
        if not self.base_url.startswith(("http://", "https://")):
            raise ValueError("base_url must be an HTTP(S) URL")
        if self.max_dte <= 0:
            raise ValueError("max_dte must be positive")
        if self.strike_range <= 0:
            raise ValueError("strike_range must be positive")
        if not 0 <= self.spot_dispersion_fraction <= 1:
            raise ValueError("spot_dispersion_fraction must be between 0 and 1")
        if self.minimum_quotes <= 0:
            raise ValueError("minimum_quotes must be positive")
        if self.timeout <= 0:
            raise ValueError("timeout must be positive")
        ZoneInfo(self.market_timezone)


def _extract_rows(payload: object) -> list[Mapping[str, Any]]:
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        rows = None
        for key in ("response", "data", "results"):
            value = payload.get(key)
            if isinstance(value, list):
                rows = value
                break
        if rows is None:
            raise ThetaDataError("ThetaData JSON response does not contain a row array")
    else:
        raise ThetaDataError("ThetaData JSON response must be an array or object")

    normalized: list[Mapping[str, Any]] = []
    for row in rows:
        if isinstance(row, Mapping):
            normalized.append(row)
    return normalized


def _parse_float(row: Mapping[str, Any], field: str) -> float | None:
    value = row.get(field)
    if value in (None, "", "NaN", "nan"):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed):
        return None
    return parsed


def _parse_timestamp(value: object, timezone: ZoneInfo) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        timestamp = datetime.fromisoformat(text)
    except ValueError:
        return None
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        timestamp = timestamp.replace(tzinfo=timezone)
    else:
        timestamp = timestamp.astimezone(timezone)
    return timestamp


def _parse_right(value: object) -> OptionType | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    if normalized in {"call", "c"}:
        return OptionType.CALL
    if normalized in {"put", "p"}:
        return OptionType.PUT
    return None


def _parse_expiration(value: object) -> date | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    for pattern in ("%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(text, pattern).date()
        except ValueError:
            continue
    return None


def normalize_greeks_payload(
    payload: object,
    *,
    symbol: str,
    config: ThetaDataConfig | None = None,
) -> OptionChainSnapshot:
    """Normalize one ThetaData ``option/history/greeks/all`` response.

    Bulk responses may contain several intervals per contract. CondorPilot keeps the latest
    usable row for each contract and fails if the selected rows imply materially inconsistent
    underlying prices.
    """
    config = config or ThetaDataConfig()
    timezone = ZoneInfo(config.market_timezone)
    rows = _extract_rows(payload)
    latest: dict[tuple[date, float, OptionType], tuple[datetime, OptionQuote, float]] = {}

    for row in rows:
        expiration = _parse_expiration(row.get("expiration"))
        option_type = _parse_right(row.get("right"))
        timestamp = _parse_timestamp(row.get("timestamp"), timezone)
        strike = _parse_float(row, "strike")
        bid = _parse_float(row, "bid")
        ask = _parse_float(row, "ask")
        delta = _parse_float(row, "delta")
        implied_vol = _parse_float(row, "implied_vol")
        underlying_price = _parse_float(row, "underlying_price")

        if None in (
            expiration,
            option_type,
            timestamp,
            strike,
            bid,
            ask,
            delta,
            underlying_price,
        ):
            continue
        assert expiration is not None
        assert option_type is not None
        assert timestamp is not None
        assert strike is not None
        assert bid is not None
        assert ask is not None
        assert delta is not None
        assert underlying_price is not None
        if strike <= 0 or bid < 0 or ask < bid or not -1 <= delta <= 1:
            continue
        if underlying_price <= 0:
            continue
        if implied_vol is not None and implied_vol < 0:
            implied_vol = None

        quote = OptionQuote(
            symbol=symbol,
            expiration=expiration,
            strike=strike,
            option_type=option_type,
            bid=bid,
            ask=ask,
            delta=delta,
            implied_volatility=implied_vol,
        )
        key = (expiration, strike, option_type)
        existing = latest.get(key)
        if existing is None or timestamp > existing[0]:
            latest[key] = (timestamp, quote, underlying_price)

    if len(latest) < config.minimum_quotes:
        raise ThetaDataNoData(
            f"ThetaData returned only {len(latest)} usable quotes for {symbol}"
        )

    selected = tuple(latest.values())
    spots = [item[2] for item in selected]
    spot = statistics.median(spots)
    dispersion = (max(spots) - min(spots)) / spot
    if dispersion > config.spot_dispersion_fraction:
        raise ThetaDataError(
            "ThetaData selected rows have excessive underlying-price dispersion: "
            f"{dispersion:.2%}"
        )
    observed_at = max(item[0] for item in selected)
    quotes = tuple(
        item[1]
        for item in sorted(
            selected,
            key=lambda item: (
                item[1].expiration,
                item[1].strike,
                item[1].option_type.value,
            ),
        )
    )
    return OptionChainSnapshot(observed_at=observed_at, spot=spot, quotes=quotes)


class ThetaDataClient:
    """Small dependency-free client for Theta Terminal's local v3 REST API."""

    def __init__(
        self,
        config: ThetaDataConfig | None = None,
        *,
        requester: JsonRequester | None = None,
    ) -> None:
        self.config = config or ThetaDataConfig()
        self._requester = requester or self._request_json

    def _request_json(self, path: str, params: Mapping[str, str]) -> object:
        query = urllib.parse.urlencode(params)
        url = f"{self.config.base_url.rstrip('/')}/{path.lstrip('/')}?{query}"
        request = urllib.request.Request(
            url,
            headers={"Accept": "application/json", "User-Agent": "CondorPilot/0.4"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.config.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ThetaDataError(
                "ThetaData request failed. Confirm Theta Terminal v3 is running at "
                f"{self.config.base_url}: {exc}"
            ) from exc

    def fetch_day(self, symbol: str, observed_on: date) -> OptionChainSnapshot:
        if not symbol.strip():
            raise ValueError("symbol must not be empty")
        params = {
            "symbol": symbol.upper(),
            "expiration": "*",
            "date": observed_on.isoformat(),
            "right": "both",
            "interval": self.config.interval,
            "start_time": self.config.start_time,
            "end_time": self.config.end_time,
            "max_dte": str(self.config.max_dte),
            "strike_range": str(self.config.strike_range),
            "format": "json",
        }
        payload = self._requester("option/history/greeks/all", params)
        return normalize_greeks_payload(payload, symbol=symbol.upper(), config=self.config)

    def fetch_history(
        self,
        symbol: str,
        *,
        start_date: date,
        end_date: date,
        skip_no_data: bool = True,
    ) -> tuple[OptionChainSnapshot, ...]:
        """Fetch daily snapshots with single-day bulk requests.

        Single-day requests avoid ThetaData's multi-day restrictions for bulk expirations.
        Weekends/holidays can be skipped without hiding connectivity or schema errors.
        """
        if end_date < start_date:
            raise ValueError("end_date must not be before start_date")
        snapshots: list[OptionChainSnapshot] = []
        current = start_date
        while current <= end_date:
            try:
                snapshots.append(self.fetch_day(symbol, current))
            except ThetaDataNoData:
                if not skip_no_data:
                    raise
            current += timedelta(days=1)
        if not snapshots:
            raise ThetaDataNoData(
                f"ThetaData returned no usable snapshots from {start_date} to {end_date}"
            )
        return validate_history(snapshots)
