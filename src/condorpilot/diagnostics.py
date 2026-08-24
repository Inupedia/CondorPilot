"""Research-grade diagnostics for normalized historical option-chain datasets."""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, replace
from datetime import date, timedelta

from condorpilot.history import OptionChainSnapshot, validate_history
from condorpilot.models import OptionQuote, OptionType, StrategyConfig
from condorpilot.strategy import NoTradeError, build_iron_condor


@dataclass(frozen=True, slots=True)
class DiagnosticThresholds:
    """Quality gates used to decide whether a normalized dataset is research-grade."""

    minimum_weekday_coverage: float = 0.95
    maximum_weekend_fraction: float = 0.01
    maximum_missing_iv_fraction: float = 0.10
    maximum_wide_quote_fraction: float = 0.25
    minimum_contract_continuity: float = 0.90
    minimum_dte_coverage: float = 0.90
    minimum_delta_coverage: float = 0.85
    minimum_wing_coverage: float = 0.85
    minimum_executable_coverage: float = 0.80
    stale_unchanged_fraction: float = 0.98
    stale_minimum_overlap: int = 20

    def __post_init__(self) -> None:
        fractions = (
            self.minimum_weekday_coverage,
            self.maximum_weekend_fraction,
            self.maximum_missing_iv_fraction,
            self.maximum_wide_quote_fraction,
            self.minimum_contract_continuity,
            self.minimum_dte_coverage,
            self.minimum_delta_coverage,
            self.minimum_wing_coverage,
            self.minimum_executable_coverage,
            self.stale_unchanged_fraction,
        )
        if any(not 0 <= value <= 1 for value in fractions):
            raise ValueError("diagnostic fractions must be between 0 and 1")
        if self.stale_minimum_overlap <= 0:
            raise ValueError("stale_minimum_overlap must be positive")


@dataclass(frozen=True, slots=True)
class DatasetDiagnostics:
    symbol: str
    start_date: date
    end_date: date
    snapshot_count: int
    quote_count: int
    fingerprint: str
    weekday_expected_count: int
    weekday_observed_count: int
    weekday_coverage: float
    weekend_snapshot_count: int
    weekend_snapshot_fraction: float
    zero_bid_count: int
    zero_bid_fraction: float
    wide_quote_count: int
    wide_quote_fraction: float
    missing_iv_count: int
    missing_iv_fraction: float
    stale_snapshot_count: int
    contract_continuity: float
    dte_available_snapshots: int
    dte_coverage: float
    delta_available_snapshots: int
    delta_coverage: float
    exact_wing_snapshots: int
    wing_coverage: float
    executable_snapshots: int
    executable_coverage: float
    research_grade: str
    issues: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return self.research_grade == "PASS"


def _fraction(numerator: int | float, denominator: int | float) -> float:
    if denominator <= 0:
        return 0.0
    return numerator / denominator


def dataset_fingerprint(
    snapshots: tuple[OptionChainSnapshot, ...] | list[OptionChainSnapshot],
) -> str:
    """Return a deterministic SHA-256 fingerprint of normalized research inputs."""
    history = validate_history(snapshots)
    digest = hashlib.sha256()
    for snapshot in history:
        digest.update(snapshot.observed_at.isoformat().encode())
        digest.update(f"|{snapshot.spot:.10f}|".encode())
        ordered = sorted(
            snapshot.quotes,
            key=lambda quote: (
                quote.expiration,
                quote.strike,
                quote.option_type.value,
            ),
        )
        for quote in ordered:
            iv = "" if quote.implied_volatility is None else f"{quote.implied_volatility:.10f}"
            row = (
                f"{quote.symbol}|{quote.expiration.isoformat()}|{quote.strike:.10f}|"
                f"{quote.option_type.value}|{quote.bid:.10f}|{quote.ask:.10f}|"
                f"{quote.delta:.10f}|{iv}\n"
            )
            digest.update(row.encode())
    return digest.hexdigest()


def _weekday_dates(start: date, end: date) -> set[date]:
    dates: set[date] = set()
    current = start
    while current <= end:
        if current.weekday() < 5:
            dates.add(current)
        current += timedelta(days=1)
    return dates


def _contract_key(quote: OptionQuote) -> tuple[date, float, OptionType]:
    return quote.expiration, quote.strike, quote.option_type


def _usable(quote: OptionQuote, strategy: StrategyConfig) -> bool:
    return quote.bid > 0 and quote.relative_spread <= strategy.max_bid_ask_spread_fraction


def _target_expiration(
    snapshot: OptionChainSnapshot,
    strategy: StrategyConfig,
) -> date | None:
    expirations = sorted({quote.expiration for quote in snapshot.quotes})
    candidates = [
        expiration
        for expiration in expirations
        if abs((expiration - snapshot.as_of).days - strategy.target_dte)
        <= strategy.max_dte_deviation_days
    ]
    if not candidates:
        return None
    return min(
        candidates,
        key=lambda expiration: abs((expiration - snapshot.as_of).days - strategy.target_dte),
    )


def _short_candidate(
    snapshot: OptionChainSnapshot,
    *,
    expiration: date,
    option_type: OptionType,
    strategy: StrategyConfig,
) -> OptionQuote | None:
    quotes = [
        quote
        for quote in snapshot.quotes
        if quote.expiration == expiration
        and quote.option_type is option_type
        and _usable(quote, strategy)
        and (
            (option_type is OptionType.PUT and quote.strike < snapshot.spot and quote.delta < 0)
            or (
                option_type is OptionType.CALL
                and quote.strike > snapshot.spot
                and quote.delta > 0
            )
        )
        and abs(abs(quote.delta) - strategy.short_delta) <= strategy.max_delta_deviation
    ]
    if not quotes:
        return None
    return min(quotes, key=lambda quote: abs(abs(quote.delta) - strategy.short_delta))


def _has_exact_wing(
    snapshot: OptionChainSnapshot,
    *,
    expiration: date,
    strike: float,
    option_type: OptionType,
    strategy: StrategyConfig,
) -> bool:
    return any(
        quote.expiration == expiration
        and quote.option_type is option_type
        and math.isclose(quote.strike, strike, abs_tol=1e-9)
        and _usable(quote, strategy)
        for quote in snapshot.quotes
    )


def _strategy_availability(
    snapshot: OptionChainSnapshot,
    strategy: StrategyConfig,
) -> tuple[bool, bool, bool, bool]:
    expiration = _target_expiration(snapshot, strategy)
    if expiration is None:
        return False, False, False, False

    short_put = _short_candidate(
        snapshot,
        expiration=expiration,
        option_type=OptionType.PUT,
        strategy=strategy,
    )
    short_call = _short_candidate(
        snapshot,
        expiration=expiration,
        option_type=OptionType.CALL,
        strategy=strategy,
    )
    delta_available = short_put is not None and short_call is not None
    if not delta_available:
        return True, False, False, False
    assert short_put is not None
    assert short_call is not None

    wings_available = _has_exact_wing(
        snapshot,
        expiration=expiration,
        strike=short_put.strike - strategy.wing_width,
        option_type=OptionType.PUT,
        strategy=strategy,
    ) and _has_exact_wing(
        snapshot,
        expiration=expiration,
        strike=short_call.strike + strategy.wing_width,
        option_type=OptionType.CALL,
        strategy=strategy,
    )
    if not wings_available:
        return True, True, False, False

    executable = True
    try:
        build_iron_condor(
            list(snapshot.quotes),
            spot=snapshot.spot,
            as_of=snapshot.as_of,
            config=replace(strategy, min_credit_to_width=0.0),
        )
    except NoTradeError:
        executable = False
    return True, True, True, executable


def _continuity(history: tuple[OptionChainSnapshot, ...]) -> float:
    eligible = 0
    matched = 0
    for previous, current in zip(history, history[1:], strict=False):
        current_keys = {_contract_key(quote) for quote in current.quotes}
        for quote in previous.quotes:
            if quote.expiration < current.as_of:
                continue
            eligible += 1
            if _contract_key(quote) in current_keys:
                matched += 1
    return 1.0 if eligible == 0 else matched / eligible


def _stale_snapshot_count(
    history: tuple[OptionChainSnapshot, ...],
    thresholds: DiagnosticThresholds,
) -> int:
    stale = 0
    for previous, current in zip(history, history[1:], strict=False):
        previous_quotes = {_contract_key(quote): quote for quote in previous.quotes}
        current_quotes = {_contract_key(quote): quote for quote in current.quotes}
        overlap = previous_quotes.keys() & current_quotes.keys()
        if len(overlap) < thresholds.stale_minimum_overlap:
            continue
        unchanged = sum(
            math.isclose(previous_quotes[key].bid, current_quotes[key].bid, abs_tol=1e-12)
            and math.isclose(previous_quotes[key].ask, current_quotes[key].ask, abs_tol=1e-12)
            for key in overlap
        )
        if unchanged / len(overlap) >= thresholds.stale_unchanged_fraction:
            stale += 1
    return stale


def diagnose_dataset(
    snapshots: tuple[OptionChainSnapshot, ...] | list[OptionChainSnapshot],
    *,
    strategy: StrategyConfig | None = None,
    thresholds: DiagnosticThresholds | None = None,
) -> DatasetDiagnostics:
    """Measure whether normalized history can support the configured strategy faithfully."""
    history = validate_history(snapshots)
    strategy = strategy or StrategyConfig()
    thresholds = thresholds or DiagnosticThresholds()

    start_date = history[0].as_of
    end_date = history[-1].as_of
    observed_dates = {snapshot.as_of for snapshot in history}
    expected_weekdays = _weekday_dates(start_date, end_date)
    weekday_observed = observed_dates & expected_weekdays
    weekend_snapshots = sum(snapshot.as_of.weekday() >= 5 for snapshot in history)

    quotes = [quote for snapshot in history for quote in snapshot.quotes]
    quote_count = len(quotes)
    zero_bid_count = sum(quote.bid == 0 for quote in quotes)
    wide_quote_count = sum(
        quote.relative_spread > strategy.max_bid_ask_spread_fraction for quote in quotes
    )
    missing_iv_count = sum(quote.implied_volatility is None for quote in quotes)

    dte_available = 0
    delta_available = 0
    wings_available = 0
    executable = 0
    for snapshot in history:
        has_dte, has_delta, has_wings, can_execute = _strategy_availability(snapshot, strategy)
        dte_available += int(has_dte)
        delta_available += int(has_delta)
        wings_available += int(has_wings)
        executable += int(can_execute)

    snapshot_count = len(history)
    weekday_coverage = _fraction(len(weekday_observed), len(expected_weekdays))
    weekend_fraction = _fraction(weekend_snapshots, snapshot_count)
    missing_iv_fraction = _fraction(missing_iv_count, quote_count)
    wide_quote_fraction = _fraction(wide_quote_count, quote_count)
    zero_bid_fraction = _fraction(zero_bid_count, quote_count)
    continuity = _continuity(history)
    dte_coverage = _fraction(dte_available, snapshot_count)
    delta_coverage = _fraction(delta_available, snapshot_count)
    wing_coverage = _fraction(wings_available, snapshot_count)
    executable_coverage = _fraction(executable, snapshot_count)
    stale_count = _stale_snapshot_count(history, thresholds)

    issues: list[str] = []
    if weekday_coverage < thresholds.minimum_weekday_coverage:
        issues.append(
            f"weekday coverage {weekday_coverage:.1%} < {thresholds.minimum_weekday_coverage:.1%}"
        )
    if weekend_fraction > thresholds.maximum_weekend_fraction:
        issues.append(
            f"weekend snapshot fraction {weekend_fraction:.1%} > "
            f"{thresholds.maximum_weekend_fraction:.1%}"
        )
    if missing_iv_fraction > thresholds.maximum_missing_iv_fraction:
        issues.append(
            f"missing IV fraction {missing_iv_fraction:.1%} > "
            f"{thresholds.maximum_missing_iv_fraction:.1%}"
        )
    if wide_quote_fraction > thresholds.maximum_wide_quote_fraction:
        issues.append(
            f"wide quote fraction {wide_quote_fraction:.1%} > "
            f"{thresholds.maximum_wide_quote_fraction:.1%}"
        )
    if continuity < thresholds.minimum_contract_continuity:
        issues.append(
            f"contract continuity {continuity:.1%} < {thresholds.minimum_contract_continuity:.1%}"
        )
    if dte_coverage < thresholds.minimum_dte_coverage:
        issues.append(
            f"target-DTE coverage {dte_coverage:.1%} < "
            f"{thresholds.minimum_dte_coverage:.1%}"
        )
    if delta_coverage < thresholds.minimum_delta_coverage:
        issues.append(
            f"target-delta coverage {delta_coverage:.1%} < {thresholds.minimum_delta_coverage:.1%}"
        )
    if wing_coverage < thresholds.minimum_wing_coverage:
        issues.append(
            f"exact-wing coverage {wing_coverage:.1%} < "
            f"{thresholds.minimum_wing_coverage:.1%}"
        )
    if executable_coverage < thresholds.minimum_executable_coverage:
        issues.append(
            f"executable-condor coverage {executable_coverage:.1%} < "
            f"{thresholds.minimum_executable_coverage:.1%}"
        )
    if stale_count:
        issues.append(f"{stale_count} snapshot(s) look potentially stale")

    return DatasetDiagnostics(
        symbol=history[0].symbol,
        start_date=start_date,
        end_date=end_date,
        snapshot_count=snapshot_count,
        quote_count=quote_count,
        fingerprint=dataset_fingerprint(history),
        weekday_expected_count=len(expected_weekdays),
        weekday_observed_count=len(weekday_observed),
        weekday_coverage=weekday_coverage,
        weekend_snapshot_count=weekend_snapshots,
        weekend_snapshot_fraction=weekend_fraction,
        zero_bid_count=zero_bid_count,
        zero_bid_fraction=zero_bid_fraction,
        wide_quote_count=wide_quote_count,
        wide_quote_fraction=wide_quote_fraction,
        missing_iv_count=missing_iv_count,
        missing_iv_fraction=missing_iv_fraction,
        stale_snapshot_count=stale_count,
        contract_continuity=continuity,
        dte_available_snapshots=dte_available,
        dte_coverage=dte_coverage,
        delta_available_snapshots=delta_available,
        delta_coverage=delta_coverage,
        exact_wing_snapshots=wings_available,
        wing_coverage=wing_coverage,
        executable_snapshots=executable,
        executable_coverage=executable_coverage,
        research_grade="PASS" if not issues else "FAIL",
        issues=tuple(issues),
    )
