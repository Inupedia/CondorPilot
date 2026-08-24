from datetime import UTC, datetime

from condorpilot.backtest import BacktestConfig
from condorpilot.diagnostics import DiagnosticThresholds
from condorpilot.evidence import (
    EvidenceThresholds,
    ResearchVerdict,
    evidence_to_dict,
    run_evidence,
)
from condorpilot.execution import ExecutionConfig
from condorpilot.history import build_synthetic_history
from condorpilot.models import StrategyConfig
from condorpilot.research import ParameterGrid
from condorpilot.validation import WalkForwardConfig

START = datetime(2026, 1, 5, 21, 0, tzinfo=UTC)


def _history():
    spots = [
        100.0 + index * 0.08 + (0.35 if index % 9 < 4 else -0.20)
        for index in range(75)
    ]
    return build_synthetic_history(
        symbol="SPY",
        start=START,
        spots=spots,
        volatility=0.25,
        target_dte=10,
        expiration_interval_days=7,
        strike_increment=1.0,
        spread=0.02,
    )


def _base() -> BacktestConfig:
    return BacktestConfig(
        initial_equity=50_000,
        strategy=StrategyConfig(
            target_dte=10,
            max_dte_deviation_days=4,
            short_delta=0.15,
            max_delta_deviation=0.08,
            wing_width=5.0,
            max_bid_ask_spread_fraction=1.0,
            profit_target_fraction=0.50,
            stop_loss_credit_multiple=2.0,
            exit_dte=3,
            max_risk_fraction=0.01,
            min_credit_to_width=0.0,
        ),
        execution=ExecutionConfig(
            slippage_fraction=0,
            commission_per_contract_per_leg=0,
        ),
    )


def _grid() -> ParameterGrid:
    return ParameterGrid(
        target_dte=(10,),
        short_delta=(0.10, 0.15),
        wing_width=(5.0,),
        profit_target_fraction=(0.50,),
        stop_loss_credit_multiple=(2.0,),
        exit_dte=(3,),
        max_risk_fraction=(0.01,),
    )


def _walk_forward() -> WalkForwardConfig:
    return WalkForwardConfig(
        train_size=30,
        test_size=15,
        step_size=15,
        rank_by="sortino",
        min_train_trades=1,
        require_dataset_pass=True,
    )


def _diagnostics() -> DiagnosticThresholds:
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


def _lenient_evidence(**overrides) -> EvidenceThresholds:
    values = dict(
        minimum_oos_folds=1,
        minimum_oos_trades=1,
        minimum_tested_snapshot_fraction=0.0,
        minimum_known_regime_trade_fraction=0.0,
        minimum_selection_stability=0.0,
        minimum_oos_total_return=-10.0,
        maximum_oos_drawdown=1.0,
        minimum_profitable_fold_fraction=0.0,
    )
    values.update(overrides)
    return EvidenceThresholds(**values)


def test_evidence_run_is_traceable_and_never_approves_live_trading() -> None:
    result = run_evidence(
        _history(),
        grid=_grid(),
        base_config=_base(),
        walk_forward_config=_walk_forward(),
        diagnostic_thresholds=_diagnostics(),
        evidence_thresholds=_lenient_evidence(),
        iv_lookback=20,
        target_iv_dte=10,
    )

    assert result.verdict is ResearchVerdict.RESEARCH_PASS
    assert not result.live_trading_approved
    assert len(result.experiment_fingerprint) == 64
    assert len(result.walk_forward.dataset.fingerprint) == 64
    assert result.known_regime_trade_fraction == 1.0
    assert sum(item.trade_count for item in result.regime_evidence) == result.walk_forward.oos_trade_count


def test_evidence_reports_insufficient_sample_before_outcome_quality() -> None:
    result = run_evidence(
        _history(),
        grid=_grid(),
        base_config=_base(),
        walk_forward_config=_walk_forward(),
        diagnostic_thresholds=_diagnostics(),
        evidence_thresholds=_lenient_evidence(minimum_oos_trades=10_000),
        iv_lookback=20,
        target_iv_dte=10,
    )

    assert result.verdict is ResearchVerdict.INSUFFICIENT_EVIDENCE
    assert any("OOS trades" in reason for reason in result.reasons)


def test_evidence_can_fail_an_outcome_threshold_without_calling_it_live_go() -> None:
    result = run_evidence(
        _history(),
        grid=_grid(),
        base_config=_base(),
        walk_forward_config=_walk_forward(),
        diagnostic_thresholds=_diagnostics(),
        evidence_thresholds=_lenient_evidence(minimum_oos_total_return=10.0),
        iv_lookback=20,
        target_iv_dte=10,
    )

    assert result.verdict is ResearchVerdict.RESEARCH_FAIL
    assert not result.live_trading_approved
    assert any("OOS return" in reason for reason in result.reasons)


def test_evidence_json_summary_keeps_provenance_benchmarks_and_regimes() -> None:
    result = run_evidence(
        _history(),
        grid=_grid(),
        base_config=_base(),
        walk_forward_config=_walk_forward(),
        diagnostic_thresholds=_diagnostics(),
        evidence_thresholds=_lenient_evidence(),
        iv_lookback=20,
        target_iv_dte=10,
    )
    payload = evidence_to_dict(result)

    assert payload["experiment_fingerprint"] == result.experiment_fingerprint
    assert payload["dataset_fingerprint"] == result.walk_forward.dataset.fingerprint
    assert payload["symbol"] == "SPY"
    assert payload["live_trading_approved"] is False
    assert payload["oos"]["folds"] == len(result.walk_forward.folds)
    assert "buy_hold_total_return" in payload["benchmarks"]
    assert len(payload["regimes"]) == 5
    assert payload["selected_parameters"]
