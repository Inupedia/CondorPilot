from datetime import UTC, datetime, timedelta

from condorpilot.diagnostics import (
    DiagnosticThresholds,
    dataset_fingerprint,
    diagnose_dataset,
)
from condorpilot.history import OptionChainSnapshot, build_synthetic_history
from condorpilot.models import StrategyConfig

START = datetime(2026, 1, 5, 21, 0, tzinfo=UTC)


def _history(*, days: int = 10, target_dte: int = 45):
    return build_synthetic_history(
        symbol="SPY",
        start=START,
        spots=[100.0 + index * 0.05 for index in range(days)],
        volatility=0.25,
        target_dte=target_dte,
        expiration_interval_days=7,
        strike_increment=1.0,
        spread=0.02,
    )


def _permissive_thresholds() -> DiagnosticThresholds:
    return DiagnosticThresholds(
        minimum_weekday_coverage=0.0,
        maximum_weekend_fraction=1.0,
        maximum_missing_iv_fraction=1.0,
        maximum_wide_quote_fraction=1.0,
        minimum_contract_continuity=0.0,
        minimum_dte_coverage=0.0,
        minimum_delta_coverage=0.0,
        minimum_wing_coverage=0.0,
        minimum_executable_coverage=0.0,
    )


def test_fingerprint_is_stable_and_content_sensitive() -> None:
    history = _history(days=3)
    same_history = _history(days=3)
    changed_history = build_synthetic_history(
        symbol="SPY",
        start=START,
        spots=[100.0, 100.05, 101.0],
        volatility=0.25,
        target_dte=45,
        expiration_interval_days=7,
        strike_increment=1.0,
        spread=0.02,
    )

    assert dataset_fingerprint(history) == dataset_fingerprint(same_history)
    assert dataset_fingerprint(history) != dataset_fingerprint(changed_history)


def test_diagnostics_measure_strategy_specific_availability() -> None:
    history = _history()
    report = diagnose_dataset(
        history,
        strategy=StrategyConfig(
            target_dte=45,
            max_dte_deviation_days=7,
            short_delta=0.15,
            max_delta_deviation=0.05,
            wing_width=5.0,
            min_credit_to_width=0.0,
        ),
        thresholds=_permissive_thresholds(),
    )

    assert report.symbol == "SPY"
    assert report.snapshot_count == len(history)
    assert report.quote_count > report.snapshot_count
    assert report.weekday_coverage == 1.0
    assert report.dte_coverage == 1.0
    assert report.delta_coverage == 1.0
    assert report.wing_coverage == 1.0
    assert report.executable_coverage == 1.0
    assert len(report.fingerprint) == 64
    assert report.passed


def test_diagnostics_detect_missing_contract_continuity() -> None:
    history = _history(days=3)
    second = history[1]
    broken_second = OptionChainSnapshot(
        observed_at=second.observed_at,
        spot=second.spot,
        quotes=second.quotes[1:],
    )
    broken = (history[0], broken_second, history[2])

    report = diagnose_dataset(broken, thresholds=_permissive_thresholds())

    assert report.contract_continuity < 1.0


def test_diagnostics_reject_dataset_without_target_dte() -> None:
    history = build_synthetic_history(
        symbol="SPY",
        start=START,
        spots=[100.0] * 5,
        volatility=0.25,
        target_dte=100,
        expiration_interval_days=30,
        strike_increment=1.0,
        spread=0.02,
    )
    thresholds = DiagnosticThresholds(
        minimum_weekday_coverage=0.0,
        maximum_weekend_fraction=1.0,
        maximum_missing_iv_fraction=1.0,
        maximum_wide_quote_fraction=1.0,
        minimum_contract_continuity=0.0,
        minimum_dte_coverage=0.80,
        minimum_delta_coverage=0.0,
        minimum_wing_coverage=0.0,
        minimum_executable_coverage=0.0,
    )

    report = diagnose_dataset(history, thresholds=thresholds)

    assert report.dte_coverage == 0.0
    assert not report.passed
    assert any("target-DTE coverage" in issue for issue in report.issues)


def test_diagnostics_flags_potentially_stale_snapshots() -> None:
    first = _history(days=1)[0]
    stale_second = OptionChainSnapshot(
        observed_at=first.observed_at + timedelta(days=1),
        spot=first.spot,
        quotes=first.quotes,
    )
    thresholds = DiagnosticThresholds(
        minimum_weekday_coverage=0.0,
        maximum_weekend_fraction=1.0,
        maximum_missing_iv_fraction=1.0,
        maximum_wide_quote_fraction=1.0,
        minimum_contract_continuity=0.0,
        minimum_dte_coverage=0.0,
        minimum_delta_coverage=0.0,
        minimum_wing_coverage=0.0,
        minimum_executable_coverage=0.0,
        stale_unchanged_fraction=0.95,
        stale_minimum_overlap=4,
    )

    report = diagnose_dataset((first, stale_second), thresholds=thresholds)

    assert report.stale_snapshot_count == 1
    assert not report.passed
    assert any("potentially stale" in issue for issue in report.issues)
