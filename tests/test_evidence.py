from datetime import UTC, datetime

import pytest

from condorpilot.backtest import BacktestConfig
from condorpilot.diagnostics import DiagnosticThresholds
from condorpilot.evidence import (
    EvidenceThresholds,
    EvidenceVerdict,
    evidence_to_dict,
    render_evidence_json,
    render_evidence_markdown,
    run_evidence,
    write_evidence_report,
)
from condorpilot.execution import ExecutionConfig
from condorpilot.history import build_synthetic_history
from condorpilot.models import StrategyConfig
from condorpilot.research import ParameterGrid
from condorpilot.validation import WalkForwardConfig

START = datetime(2026, 1, 5, 21, 0, tzinfo=UTC)


def _history(*, symbol: str = "SPY", days: int = 80, target_dte: int = 10):
    spots = [100.0 + index * 0.06 + (0.30 if index % 11 < 5 else -0.20) for index in range(days)]
    return build_synthetic_history(
        symbol=symbol,
        start=START,
        spots=spots,
        volatility=0.25,
        target_dte=target_dte,
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
        short_delta=(0.15,),
        wing_width=(5.0,),
        profit_target_fraction=(0.50,),
        stop_loss_credit_multiple=(2.0,),
        exit_dte=(3,),
        max_risk_fraction=(0.01,),
    )


def _walk() -> WalkForwardConfig:
    return WalkForwardConfig(
        train_size=30,
        test_size=15,
        step_size=15,
        min_train_trades=1,
    )


def _diagnostics(**overrides) -> DiagnosticThresholds:
    values = dict(
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
    values.update(overrides)
    return DiagnosticThresholds(**values)


def _permissive_evidence() -> EvidenceThresholds:
    return EvidenceThresholds(
        minimum_oos_folds=2,
        minimum_oos_trades=1,
        minimum_tested_fraction=0.10,
        minimum_positive_fold_fraction=0.0,
        minimum_profit_factor=0.0,
        maximum_oos_drawdown=1.0,
        minimum_oos_total_return=-1.0,
    )


def test_evidence_can_pass_research_gate_but_never_live_gate() -> None:
    result = run_evidence(
        _history(),
        source="test-real-like.csv",
        grid=_grid(),
        base_config=_base(),
        walk_forward_config=_walk(),
        diagnostic_thresholds=_diagnostics(),
        evidence_thresholds=_permissive_evidence(),
        iv_lookback=20,
        target_iv_dte=10,
    )

    assert result.verdict is EvidenceVerdict.RESEARCH_PASS
    assert result.live_ready is False
    assert result.walk_forward is not None
    assert len(result.dataset.fingerprint) == 64
    assert result.regime_performance


def test_evidence_rejects_when_oos_gate_is_intentionally_unreachable() -> None:
    thresholds = EvidenceThresholds(
        minimum_oos_folds=2,
        minimum_oos_trades=1,
        minimum_tested_fraction=0.10,
        minimum_positive_fold_fraction=0.0,
        minimum_profit_factor=0.0,
        maximum_oos_drawdown=1.0,
        minimum_oos_total_return=0.99,
    )
    result = run_evidence(
        _history(),
        source="test-real-like.csv",
        grid=_grid(),
        base_config=_base(),
        walk_forward_config=_walk(),
        diagnostic_thresholds=_diagnostics(),
        evidence_thresholds=thresholds,
    )

    assert result.verdict is EvidenceVerdict.RESEARCH_REJECT
    assert any("OOS return" in reason for reason in result.reasons)


def test_evidence_stops_before_backtest_when_dataset_fails() -> None:
    result = run_evidence(
        _history(target_dte=100),
        source="bad.csv",
        grid=_grid(),
        base_config=_base(),
        walk_forward_config=_walk(),
        diagnostic_thresholds=_diagnostics(minimum_dte_coverage=0.80),
    )

    assert result.verdict is EvidenceVerdict.DATA_FAIL
    assert result.walk_forward is None
    assert result.dataset.research_grade == "FAIL"


def test_evidence_requires_expected_symbol_and_named_source() -> None:
    with pytest.raises(ValueError, match="source must identify"):
        run_evidence(_history(), source="")
    with pytest.raises(ValueError, match="expected SPY"):
        run_evidence(_history(symbol="QQQ"), source="qqq.csv")


def test_evidence_reports_are_compact_traceable_and_writable(tmp_path) -> None:
    result = run_evidence(
        _history(),
        source="spy-history.csv",
        grid=_grid(),
        base_config=_base(),
        walk_forward_config=_walk(),
        diagnostic_thresholds=_diagnostics(),
        evidence_thresholds=_permissive_evidence(),
    )
    payload = evidence_to_dict(result)
    json_text = render_evidence_json(result)
    markdown = render_evidence_markdown(result)
    json_path, markdown_path = write_evidence_report(result, tmp_path)

    assert payload["dataset"]["fingerprint_sha256"] == result.dataset.fingerprint
    assert payload["live_ready"] is False
    assert "quotes" not in json_text
    assert result.dataset.fingerprint in json_text
    assert "Live trading ready:** `NO`" in markdown
    assert json_path.read_text() == json_text
    assert markdown_path.read_text() == markdown
