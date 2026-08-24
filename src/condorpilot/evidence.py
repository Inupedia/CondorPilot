"""Reproducible evidence runs combining data quality, OOS validation, and regime attribution."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path

from condorpilot.backtest import BacktestConfig
from condorpilot.diagnostics import DiagnosticThresholds
from condorpilot.history import OptionChainSnapshot, validate_history
from condorpilot.research import ParameterGrid
from condorpilot.validation import WalkForwardConfig, WalkForwardResult, run_walk_forward
from condorpilot.volatility import (
    RegimeThresholds,
    VixObservation,
    VolatilityRegime,
    build_volatility_regimes,
)


class ResearchVerdict(StrEnum):
    """Evidence-only verdicts. None of these authorize live trading."""

    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    RESEARCH_FAIL = "research_fail"
    RESEARCH_PASS = "research_pass"


@dataclass(frozen=True, slots=True)
class EvidenceThresholds:
    """Minimum evidence required before a strategy can earn a research PASS."""

    minimum_oos_folds: int = 4
    minimum_oos_trades: int = 20
    minimum_tested_snapshot_fraction: float = 0.25
    minimum_known_regime_trade_fraction: float = 0.80
    minimum_selection_stability: float = 0.25
    minimum_oos_total_return: float = 0.0
    maximum_oos_drawdown: float = 0.25
    minimum_profitable_fold_fraction: float = 0.50
    minimum_excess_return_vs_buy_hold: float | None = None

    def __post_init__(self) -> None:
        if self.minimum_oos_folds <= 0:
            raise ValueError("minimum_oos_folds must be positive")
        if self.minimum_oos_trades <= 0:
            raise ValueError("minimum_oos_trades must be positive")
        fractions = (
            self.minimum_tested_snapshot_fraction,
            self.minimum_known_regime_trade_fraction,
            self.minimum_selection_stability,
            self.maximum_oos_drawdown,
            self.minimum_profitable_fold_fraction,
        )
        if any(not 0 <= value <= 1 for value in fractions):
            raise ValueError("evidence fractions must be between 0 and 1")


@dataclass(frozen=True, slots=True)
class RegimeEvidence:
    regime: VolatilityRegime
    trade_count: int
    net_pnl: float
    average_trade_pnl: float
    win_rate: float


@dataclass(frozen=True, slots=True)
class EvidenceResult:
    """One traceable research experiment over one immutable normalized dataset."""

    experiment_fingerprint: str
    walk_forward: WalkForwardResult
    verdict: ResearchVerdict
    reasons: tuple[str, ...]
    regime_evidence: tuple[RegimeEvidence, ...]
    known_regime_trade_fraction: float
    profitable_fold_fraction: float
    excess_return_vs_cash: float
    excess_return_vs_buy_hold: float

    @property
    def live_trading_approved(self) -> bool:
        """Always false: execution/account safety is outside this research pipeline."""
        return False


def _experiment_fingerprint(
    *,
    dataset_fingerprint: str,
    grid: ParameterGrid,
    base_config: BacktestConfig,
    walk_forward_config: WalkForwardConfig,
    evidence_thresholds: EvidenceThresholds,
    regime_thresholds: RegimeThresholds,
    iv_lookback: int,
    target_iv_dte: int,
) -> str:
    payload = "|".join(
        (
            dataset_fingerprint,
            repr(grid),
            repr(base_config),
            repr(walk_forward_config),
            repr(evidence_thresholds),
            repr(regime_thresholds),
            str(iv_lookback),
            str(target_iv_dte),
        )
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _regime_evidence(
    result: WalkForwardResult,
    regime_by_date: dict,
) -> tuple[tuple[RegimeEvidence, ...], float]:
    pnls = {regime: [] for regime in VolatilityRegime}
    for fold in result.folds:
        for trade in fold.test_result.trades:
            regime = regime_by_date.get(trade.opened_at.date(), VolatilityRegime.UNKNOWN)
            pnls[regime].append(trade.net_pnl)

    summaries: list[RegimeEvidence] = []
    known = 0
    total = 0
    for regime in VolatilityRegime:
        values = pnls[regime]
        total += len(values)
        if regime is not VolatilityRegime.UNKNOWN:
            known += len(values)
        summaries.append(
            RegimeEvidence(
                regime=regime,
                trade_count=len(values),
                net_pnl=sum(values),
                average_trade_pnl=sum(values) / len(values) if values else 0.0,
                win_rate=(
                    sum(value > 0 for value in values) / len(values)
                    if values
                    else 0.0
                ),
            )
        )
    known_fraction = known / total if total else 0.0
    return tuple(summaries), known_fraction


def _verdict(
    result: WalkForwardResult,
    *,
    thresholds: EvidenceThresholds,
    known_regime_trade_fraction: float,
) -> tuple[ResearchVerdict, tuple[str, ...], float]:
    insufficient: list[str] = []
    if len(result.folds) < thresholds.minimum_oos_folds:
        insufficient.append(
            f"OOS folds {len(result.folds)} < {thresholds.minimum_oos_folds}"
        )
    if result.oos_trade_count < thresholds.minimum_oos_trades:
        insufficient.append(
            f"OOS trades {result.oos_trade_count} < {thresholds.minimum_oos_trades}"
        )
    if result.tested_snapshot_fraction < thresholds.minimum_tested_snapshot_fraction:
        insufficient.append(
            "tested snapshot fraction "
            f"{result.tested_snapshot_fraction:.1%} < "
            f"{thresholds.minimum_tested_snapshot_fraction:.1%}"
        )
    if known_regime_trade_fraction < thresholds.minimum_known_regime_trade_fraction:
        insufficient.append(
            "known-regime trade fraction "
            f"{known_regime_trade_fraction:.1%} < "
            f"{thresholds.minimum_known_regime_trade_fraction:.1%}"
        )
    if result.selection_stability < thresholds.minimum_selection_stability:
        insufficient.append(
            f"parameter stability {result.selection_stability:.1%} < "
            f"{thresholds.minimum_selection_stability:.1%}"
        )
    if insufficient:
        return ResearchVerdict.INSUFFICIENT_EVIDENCE, tuple(insufficient), 0.0

    profitable_fold_fraction = (
        sum(fold.test_metrics.total_return > 0 for fold in result.folds) / len(result.folds)
    )
    failed: list[str] = []
    if result.oos_total_return < thresholds.minimum_oos_total_return:
        failed.append(
            f"OOS return {result.oos_total_return:+.2%} < "
            f"{thresholds.minimum_oos_total_return:+.2%}"
        )
    if result.oos_max_drawdown > thresholds.maximum_oos_drawdown:
        failed.append(
            f"OOS drawdown {result.oos_max_drawdown:.2%} > "
            f"{thresholds.maximum_oos_drawdown:.2%}"
        )
    if profitable_fold_fraction < thresholds.minimum_profitable_fold_fraction:
        failed.append(
            "profitable-fold fraction "
            f"{profitable_fold_fraction:.1%} < "
            f"{thresholds.minimum_profitable_fold_fraction:.1%}"
        )
    if thresholds.minimum_excess_return_vs_buy_hold is not None:
        excess = result.oos_total_return - result.buy_hold_total_return
        if excess < thresholds.minimum_excess_return_vs_buy_hold:
            failed.append(
                f"excess return vs buy-and-hold {excess:+.2%} < "
                f"{thresholds.minimum_excess_return_vs_buy_hold:+.2%}"
            )
    if failed:
        return ResearchVerdict.RESEARCH_FAIL, tuple(failed), profitable_fold_fraction
    return ResearchVerdict.RESEARCH_PASS, (), profitable_fold_fraction


def run_evidence(
    snapshots: tuple[OptionChainSnapshot, ...] | list[OptionChainSnapshot],
    *,
    vix_history: tuple[VixObservation, ...] | list[VixObservation] = (),
    grid: ParameterGrid | None = None,
    base_config: BacktestConfig | None = None,
    walk_forward_config: WalkForwardConfig | None = None,
    diagnostic_thresholds: DiagnosticThresholds | None = None,
    evidence_thresholds: EvidenceThresholds | None = None,
    regime_thresholds: RegimeThresholds | None = None,
    iv_lookback: int = 252,
    target_iv_dte: int = 30,
) -> EvidenceResult:
    """Run one strict evidence experiment without using future data for parameter selection."""
    history = validate_history(snapshots)
    grid = grid or ParameterGrid()
    base_config = base_config or BacktestConfig()
    walk_forward_config = walk_forward_config or WalkForwardConfig()
    evidence_thresholds = evidence_thresholds or EvidenceThresholds()
    regime_thresholds = regime_thresholds or RegimeThresholds()

    walk_forward = run_walk_forward(
        history,
        grid=grid,
        base_config=base_config,
        config=walk_forward_config,
        diagnostic_thresholds=diagnostic_thresholds,
    )
    observations = build_volatility_regimes(
        history,
        vix_history=vix_history,
        iv_lookback=iv_lookback,
        target_iv_dte=target_iv_dte,
        thresholds=regime_thresholds,
    )
    regime_by_date = {item.observed_on: item.regime for item in observations}
    regime_evidence, known_fraction = _regime_evidence(walk_forward, regime_by_date)
    verdict, reasons, profitable_fold_fraction = _verdict(
        walk_forward,
        thresholds=evidence_thresholds,
        known_regime_trade_fraction=known_fraction,
    )

    return EvidenceResult(
        experiment_fingerprint=_experiment_fingerprint(
            dataset_fingerprint=walk_forward.dataset.fingerprint,
            grid=grid,
            base_config=base_config,
            walk_forward_config=walk_forward_config,
            evidence_thresholds=evidence_thresholds,
            regime_thresholds=regime_thresholds,
            iv_lookback=iv_lookback,
            target_iv_dte=target_iv_dte,
        ),
        walk_forward=walk_forward,
        verdict=verdict,
        reasons=reasons,
        regime_evidence=regime_evidence,
        known_regime_trade_fraction=known_fraction,
        profitable_fold_fraction=profitable_fold_fraction,
        excess_return_vs_cash=walk_forward.oos_total_return,
        excess_return_vs_buy_hold=(
            walk_forward.oos_total_return - walk_forward.buy_hold_total_return
        ),
    )


def evidence_to_dict(result: EvidenceResult) -> dict:
    """Serialize the durable evidence summary without embedding every backtest quote/equity point."""
    return {
        "experiment_fingerprint": result.experiment_fingerprint,
        "dataset_fingerprint": result.walk_forward.dataset.fingerprint,
        "symbol": result.walk_forward.dataset.symbol,
        "verdict": result.verdict.value,
        "live_trading_approved": False,
        "reasons": list(result.reasons),
        "oos": {
            "folds": len(result.walk_forward.folds),
            "trades": result.walk_forward.oos_trade_count,
            "total_return": result.walk_forward.oos_total_return,
            "max_drawdown": result.walk_forward.oos_max_drawdown,
            "win_rate": result.walk_forward.oos_win_rate,
            "tested_snapshot_fraction": result.walk_forward.tested_snapshot_fraction,
            "selection_stability": result.walk_forward.selection_stability,
            "profitable_fold_fraction": result.profitable_fold_fraction,
        },
        "benchmarks": {
            "cash_total_return": result.walk_forward.cash_total_return,
            "buy_hold_total_return": result.walk_forward.buy_hold_total_return,
            "excess_return_vs_cash": result.excess_return_vs_cash,
            "excess_return_vs_buy_hold": result.excess_return_vs_buy_hold,
        },
        "regimes": [
            {
                **asdict(item),
                "regime": item.regime.value,
            }
            for item in result.regime_evidence
        ],
        "known_regime_trade_fraction": result.known_regime_trade_fraction,
        "selected_parameters": [
            {
                "count": count,
                "parameters": asdict(parameters),
            }
            for parameters, count in result.walk_forward.selected_parameter_counts
        ],
    }


def save_evidence_json(result: EvidenceResult, path: str | Path) -> None:
    """Persist an evidence summary with stable JSON formatting for audit/review."""
    Path(path).write_text(
        json.dumps(evidence_to_dict(result), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
