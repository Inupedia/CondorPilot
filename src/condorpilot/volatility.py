"""Volatility observations, Cboe VIX history, and entry-regime classification."""

from __future__ import annotations

import csv
import io
import math
import statistics
import urllib.request
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from pathlib import Path

from condorpilot.backtest import EntryFilter
from condorpilot.history import OptionChainSnapshot, validate_history
from condorpilot.models import OptionType

CBOE_VIX_HISTORY_URL = "https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv"


class VolatilityDataError(ValueError):
    """Raised when volatility data cannot be parsed or aligned safely."""


class VolatilityRegime(StrEnum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    STRESS = "stress"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class RegimeThresholds:
    low_vix: float = 15.0
    high_vix: float = 25.0
    stress_vix: float = 35.0
    low_iv_percentile: float = 0.25
    high_iv_percentile: float = 0.75
    stress_iv_percentile: float = 0.90

    def __post_init__(self) -> None:
        if not 0 < self.low_vix < self.high_vix < self.stress_vix:
            raise ValueError("VIX thresholds must increase from low to stress")
        if not (
            0
            <= self.low_iv_percentile
            < self.high_iv_percentile
            < self.stress_iv_percentile
            <= 1
        ):
            raise ValueError("IV percentile thresholds must increase within [0, 1]")


@dataclass(frozen=True, slots=True)
class VixObservation:
    observed_on: date
    close: float

    def __post_init__(self) -> None:
        if not math.isfinite(self.close) or self.close < 0:
            raise ValueError("VIX close must be finite and non-negative")


@dataclass(frozen=True, slots=True)
class VolatilityObservation:
    observed_on: date
    atm_iv: float | None
    iv_percentile: float | None
    iv_rank: float | None
    vix_close: float | None
    regime: VolatilityRegime


def _parse_vix_date(value: str) -> date:
    text = value.strip()
    for parser in (
        date.fromisoformat,
        lambda item: datetime.strptime(item, "%m/%d/%Y").date(),
        lambda item: datetime.strptime(item, "%m/%d/%y").date(),
    ):
        try:
            return parser(text)
        except ValueError:
            continue
    raise VolatilityDataError(f"invalid VIX date {value!r}")


def parse_vix_csv(text: str) -> tuple[VixObservation, ...]:
    """Parse Cboe's daily VIX CSV format into chronological close observations."""
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
    if reader.fieldnames is None:
        raise VolatilityDataError("VIX CSV is missing a header")
    fields = {field.strip().upper(): field for field in reader.fieldnames}
    if "DATE" not in fields or "CLOSE" not in fields:
        raise VolatilityDataError("VIX CSV must contain DATE and CLOSE columns")

    observations: list[VixObservation] = []
    for row_number, row in enumerate(reader, start=2):
        date_text = row.get(fields["DATE"], "")
        close_text = row.get(fields["CLOSE"], "")
        if not date_text or not close_text:
            continue
        observed_on = _parse_vix_date(date_text)
        try:
            close = float(close_text)
        except ValueError as exc:
            raise VolatilityDataError(
                f"row {row_number}: invalid VIX close {close_text!r}"
            ) from exc
        observations.append(VixObservation(observed_on=observed_on, close=close))

    if not observations:
        raise VolatilityDataError("VIX CSV contains no observations")
    observations.sort(key=lambda item: item.observed_on)
    if len({item.observed_on for item in observations}) != len(observations):
        raise VolatilityDataError("VIX CSV contains duplicate dates")
    return tuple(observations)


def load_vix_csv(path: str | Path) -> tuple[VixObservation, ...]:
    return parse_vix_csv(Path(path).read_text(encoding="utf-8-sig"))


def fetch_cboe_vix_history(
    *,
    url: str = CBOE_VIX_HISTORY_URL,
    timeout: float = 30.0,
) -> tuple[VixObservation, ...]:
    """Fetch Cboe's public daily VIX history using only the Python standard library."""
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "CondorPilot/0.4 (+https://github.com/Inupedia/CondorPilot)"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read().decode("utf-8-sig")
    except OSError as exc:
        raise VolatilityDataError(f"failed to fetch Cboe VIX history: {exc}") from exc
    return parse_vix_csv(payload)


def estimate_atm_iv(
    snapshot: OptionChainSnapshot,
    *,
    target_dte: int = 30,
) -> float | None:
    """Estimate ATM IV from the call/put quotes nearest 0.50 absolute delta."""
    candidates = [
        quote
        for quote in snapshot.quotes
        if quote.implied_volatility is not None and quote.expiration >= snapshot.as_of
    ]
    if not candidates:
        return None

    expirations = sorted({quote.expiration for quote in candidates})
    expiration = min(
        expirations,
        key=lambda item: abs((item - snapshot.as_of).days - target_dte),
    )
    chain = [quote for quote in candidates if quote.expiration == expiration]
    selected = []
    for option_type in (OptionType.PUT, OptionType.CALL):
        side = [quote for quote in chain if quote.option_type is option_type]
        if side:
            selected.append(min(side, key=lambda quote: abs(abs(quote.delta) - 0.50)))
    values = [
        quote.implied_volatility
        for quote in selected
        if quote.implied_volatility is not None
    ]
    if not values:
        return None
    return statistics.median(values)


def _percentile_rank(values: list[float], current: float) -> float:
    below = sum(value < current for value in values)
    equal = sum(value == current for value in values)
    return (below + 0.5 * equal) / len(values)


def _iv_rank(values: list[float], current: float) -> float:
    low = min(values)
    high = max(values)
    if math.isclose(low, high):
        return 0.5
    return (current - low) / (high - low)


def classify_regime(
    *,
    vix_close: float | None,
    iv_percentile: float | None,
    thresholds: RegimeThresholds | None = None,
) -> VolatilityRegime:
    """Classify a regime using VIX level and rolling underlying-option IV percentile."""
    thresholds = thresholds or RegimeThresholds()
    if vix_close is None and iv_percentile is None:
        return VolatilityRegime.UNKNOWN
    if (
        vix_close is not None
        and vix_close >= thresholds.stress_vix
        or iv_percentile is not None
        and iv_percentile >= thresholds.stress_iv_percentile
    ):
        return VolatilityRegime.STRESS
    if (
        vix_close is not None
        and vix_close >= thresholds.high_vix
        or iv_percentile is not None
        and iv_percentile >= thresholds.high_iv_percentile
    ):
        return VolatilityRegime.HIGH
    if vix_close is not None:
        if vix_close < thresholds.low_vix and (
            iv_percentile is None or iv_percentile <= 0.50
        ):
            return VolatilityRegime.LOW
    elif iv_percentile is not None and iv_percentile <= thresholds.low_iv_percentile:
        return VolatilityRegime.LOW
    return VolatilityRegime.NORMAL


def build_volatility_regimes(
    snapshots: Iterable[OptionChainSnapshot],
    *,
    vix_history: Iterable[VixObservation] = (),
    iv_lookback: int = 252,
    target_iv_dte: int = 30,
    thresholds: RegimeThresholds | None = None,
) -> tuple[VolatilityObservation, ...]:
    """Align option IV and VIX into one daily volatility-regime series."""
    if iv_lookback <= 0:
        raise ValueError("iv_lookback must be positive")
    history = validate_history(snapshots)
    thresholds = thresholds or RegimeThresholds()
    vix_by_date = {item.observed_on: item.close for item in vix_history}
    iv_window: list[float] = []
    observations: list[VolatilityObservation] = []

    for snapshot in history:
        atm_iv = estimate_atm_iv(snapshot, target_dte=target_iv_dte)
        iv_percentile = None
        iv_rank = None
        if atm_iv is not None:
            iv_window.append(atm_iv)
            rolling = iv_window[-iv_lookback:]
            iv_percentile = _percentile_rank(rolling, atm_iv)
            iv_rank = _iv_rank(rolling, atm_iv)
        vix_close = vix_by_date.get(snapshot.as_of)
        observations.append(
            VolatilityObservation(
                observed_on=snapshot.as_of,
                atm_iv=atm_iv,
                iv_percentile=iv_percentile,
                iv_rank=iv_rank,
                vix_close=vix_close,
                regime=classify_regime(
                    vix_close=vix_close,
                    iv_percentile=iv_percentile,
                    thresholds=thresholds,
                ),
            )
        )
    return tuple(observations)


def build_regime_entry_filter(
    observations: Iterable[VolatilityObservation],
    *,
    allowed_regimes: Iterable[VolatilityRegime],
) -> EntryFilter:
    """Return an entry filter that allows only explicitly selected daily regimes."""
    allowed = frozenset(allowed_regimes)
    if not allowed:
        raise ValueError("allowed_regimes must not be empty")
    regime_by_date = {item.observed_on: item.regime for item in observations}

    def entry_filter(snapshot: OptionChainSnapshot) -> bool:
        return regime_by_date.get(snapshot.as_of, VolatilityRegime.UNKNOWN) in allowed

    return entry_filter


def regime_counts(
    observations: Iterable[VolatilityObservation],
) -> dict[VolatilityRegime, int]:
    counts = {regime: 0 for regime in VolatilityRegime}
    for observation in observations:
        counts[observation.regime] += 1
    return counts
