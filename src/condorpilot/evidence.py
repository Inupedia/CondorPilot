"""Auditable research-evidence reports built from real normalized option history."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from condorpilot.backtest import BacktestConfig, TradeRecord
from condorpilot.diagnostics import (
    DatasetDiagnostics,
    DiagnosticThresholds,
    diagnose_dataset,
)
from condorpilot.history import OptionChainSnapshot, validate_history
from condorpilot.research import ParameterGrid, ResearchParameters
from condorpilot.validation import (
    WalkForwardConfig,
    WalkForwardError,
    WalkForwardResult,
    run_walk_forward,
)
from condorpilot.volatility import (
    VixObservation,
    VolatilityObservation,
    VolatilityRegime,
    build_volatility_regimes,
)


class EvidenceVerdict(StrEnum):
    """Research-evidence gate. None of these verdicts authorizes live trading."""

    DATA_FAIL = "data_fail"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    RESEARCH_REJECT = "research_reject"
    RESEARCH_PASS = "research_pass"


@dataclass(frozen=True, slots=True)
class EvidenceThresholds:
    """Configurable minimum evidence required for a research-only PASS verdict."""

    minimum_oos_folds: int = 4
    minimum_oos_trades: int = 20
    minimum_tested_fraction: float = 0.20
    minimum_positive_fold_fraction: float = 0.50
    minimum_profit_factor: float = 1.00
    maximum_oos_drawdown: float = 0.25
    minimum_oos_total_return: float = 0.0

    def __post_init__(self) -> None:
        if self.minimum_oos_folds <= 0:
            raise ValueError("minimum_oos_folds must be positive")
        if self.minimum_oos_trades <= 0:
            raise ValueError("minimum_oos_trades must be positive")
        if not 0 < self.minimum_tested_fraction <= 1:
            raise ValueError("minimum_tested_fraction must be within (0, 1]")
        if not 0 <= self.minimum_positive_fold_fraction <= 1:
            raise ValueError("minimum_positive_fold_fraction must be within [0, 1]")
        if self.minimum_profit_factor < 0:
            raise ValueError("minimum_profit_factor must be non-negative")
        if not 0 <= self.maximum_oos_drawdown <= 1:
            raise ValueError("maximum_oos_drawdown must be within [0, 1]")
        if self.minimum_oos_total_return < -1:
            raise ValueError("minimum_oos_total_return must be at least -1")


@dataclass(frozen=True, slots=True)
class RegimePerformance:
    regime: VolatilityRegime
    trade_count: int
    win_rate: float
    net_pnl: float
    average_trade: float
    profit_factor: float


@dataclass(frozen=True, slots=True)
class EvidenceResult:
    source: str
    symbol: str
    dataset: DatasetDiagnostics
    verdict: EvidenceVerdict
    reasons: tuple[str, ...]
    walk_forward: WalkForwardResult | None
    regime_performance: tuple[RegimePerformance, ...]

    @property
    def live_ready(self) -> bool:
        """Evidence reports can never authorize live execution."""
        return False

    @property
    def oos_profit_factor(self) -> float:
        if self.walk_forward is None:
            return 0.0
        return _profit_factor(_oos_trades(self.walk_forward))

    @property
    def positive_fold_fraction(self) -> float:
        if self.walk_forward is None or not self.walk_forward.folds:
            return 0.0
        positive = sum(fold.test_metrics.total_return > 0 for fold in self.walk_forward.folds)
        return positive / len(self.walk_forward.folds)


def _oos_trades(result: WalkForwardResult) -> tuple[TradeRecord, ...]:
    return tuple(trade for fold in result.folds for trade in fold.test_result.trades)


def _profit_factor(trades: tuple[TradeRecord, ...] | list[TradeRecord]) -> float:
    profits = sum(trade.net_pnl for trade in trades if trade.net_pnl > 0)
    losses = -sum(trade.net_pnl for trade in trades if trade.net_pnl < 0)
    if losses > 0:
        return profits / losses
    if profits > 0:
        return math.inf
    return 0.0


def _regime_performance(
    walk_forward: WalkForwardResult,
    observations: tuple[VolatilityObservation, ...],
) -> tuple[RegimePerformance, ...]:
    regime_by_date = {item.observed_on: item.regime for item in observations}
    trades_by_regime: dict[VolatilityRegime, list[TradeRecord]] = {
        regime: [] for regime in VolatilityRegime
    }
    for trade in _oos_trades(walk_forward):
        regime = regime_by_date.get(trade.opened_at.date(), VolatilityRegime.UNKNOWN)
        trades_by_regime[regime].append(trade)

    rows: list[RegimePerformance] = []
    for regime in VolatilityRegime:
        trades = trades_by_regime[regime]
        count = len(trades)
        net_pnl = sum(trade.net_pnl for trade in trades)
        rows.append(
            RegimePerformance(
                regime=regime,
                trade_count=count,
                win_rate=(sum(trade.net_pnl > 0 for trade in trades) / count if count else 0.0),
                net_pnl=net_pnl,
                average_trade=net_pnl / count if count else 0.0,
                profit_factor=_profit_factor(trades),
            )
        )
    return tuple(rows)


def _research_verdict(
    result: WalkForwardResult,
    thresholds: EvidenceThresholds,
) -> tuple[EvidenceVerdict, tuple[str, ...]]:
    insufficient: list[str] = []
    if len(result.folds) < thresholds.minimum_oos_folds:
        insufficient.append(
            f"OOS folds {len(result.folds)} < {thresholds.minimum_oos_folds}"
        )
    if result.oos_trade_count < thresholds.minimum_oos_trades:
        insufficient.append(
            f"OOS trades {result.oos_trade_count} < {thresholds.minimum_oos_trades}"
        )
    if result.tested_snapshot_fraction < thresholds.minimum_tested_fraction:
        insufficient.append(
            f"tested fraction {result.tested_snapshot_fraction:.1%} < "
            f"{thresholds.minimum_tested_fraction:.1%}"
        )
    if insufficient:
        return EvidenceVerdict.INSUFFICIENT_EVIDENCE, tuple(insufficient)

    trades = _oos_trades(result)
    profit_factor = _profit_factor(trades)
    positive_folds = sum(fold.test_metrics.total_return > 0 for fold in result.folds)
    positive_fold_fraction = positive_folds / len(result.folds)
    rejected: list[str] = []
    if result.oos_total_return <= thresholds.minimum_oos_total_return:
        rejected.append(
            f"OOS return {result.oos_total_return:+.2%} <= "
            f"{thresholds.minimum_oos_total_return:+.2%}"
        )
    if profit_factor < thresholds.minimum_profit_factor:
        rejected.append(
            f"OOS profit factor {profit_factor:.2f} < {thresholds.minimum_profit_factor:.2f}"
        )
    if positive_fold_fraction < thresholds.minimum_positive_fold_fraction:
        rejected.append(
            f"positive-fold fraction {positive_fold_fraction:.1%} < "
            f"{thresholds.minimum_positive_fold_fraction:.1%}"
        )
    if result.oos_max_drawdown > thresholds.maximum_oos_drawdown:
        rejected.append(
            f"OOS max drawdown {result.oos_max_drawdown:.2%} > "
            f"{thresholds.maximum_oos_drawdown:.2%}"
        )
    if rejected:
        return EvidenceVerdict.RESEARCH_REJECT, tuple(rejected)

    reasons = (
        "dataset passed research-quality diagnostics",
        f"{len(result.folds)} non-overlapping OOS folds evaluated",
        f"{result.oos_trade_count} OOS trades evaluated",
        f"OOS return {result.oos_total_return:+.2%}",
        f"OOS profit factor {profit_factor:.2f}",
        f"OOS max drawdown {result.oos_max_drawdown:.2%}",
        "research PASS does not imply paper-trading or live-trading readiness",
    )
    return EvidenceVerdict.RESEARCH_PASS, reasons


def run_evidence(
    snapshots: tuple[OptionChainSnapshot, ...] | list[OptionChainSnapshot],
    *,
    source: str,
    expected_symbol: str = "SPY",
    vix_history: tuple[VixObservation, ...] | list[VixObservation] = (),
    grid: ParameterGrid | None = None,
    base_config: BacktestConfig | None = None,
    walk_forward_config: WalkForwardConfig | None = None,
    diagnostic_thresholds: DiagnosticThresholds | None = None,
    evidence_thresholds: EvidenceThresholds | None = None,
    iv_lookback: int = 252,
    target_iv_dte: int = 30,
) -> EvidenceResult:
    """Build one traceable real-data evidence result without manufacturing missing evidence."""
    if not source.strip():
        raise ValueError("source must identify the real dataset used for the evidence run")
    history = validate_history(snapshots)
    symbol = history[0].symbol.upper()
    if symbol != expected_symbol.upper():
        raise ValueError(f"expected {expected_symbol.upper()} history, received {symbol}")

    base_config = base_config or BacktestConfig()
    walk_forward_config = walk_forward_config or WalkForwardConfig(
        train_size=504,
        test_size=126,
        step_size=126,
        min_train_trades=5,
    )
    evidence_thresholds = evidence_thresholds or EvidenceThresholds()
    dataset = diagnose_dataset(
        history,
        strategy=base_config.strategy,
        thresholds=diagnostic_thresholds,
    )
    if not dataset.passed:
        return EvidenceResult(
            source=source,
            symbol=symbol,
            dataset=dataset,
            verdict=EvidenceVerdict.DATA_FAIL,
            reasons=dataset.issues or ("dataset failed research-quality diagnostics",),
            walk_forward=None,
            regime_performance=(),
        )

    try:
        walk_forward = run_walk_forward(
            history,
            grid=grid,
            base_config=base_config,
            config=WalkForwardConfig(
                train_size=walk_forward_config.train_size,
                test_size=walk_forward_config.test_size,
                step_size=walk_forward_config.step_size,
                anchored=walk_forward_config.anchored,
                rank_by=walk_forward_config.rank_by,
                min_train_trades=walk_forward_config.min_train_trades,
                require_dataset_pass=False,
            ),
            diagnostic_thresholds=diagnostic_thresholds,
        )
    except WalkForwardError as exc:
        return EvidenceResult(
            source=source,
            symbol=symbol,
            dataset=dataset,
            verdict=EvidenceVerdict.INSUFFICIENT_EVIDENCE,
            reasons=(str(exc),),
            walk_forward=None,
            regime_performance=(),
        )

    observations = build_volatility_regimes(
        history,
        vix_history=vix_history,
        iv_lookback=iv_lookback,
        target_iv_dte=target_iv_dte,
    )
    verdict, reasons = _research_verdict(walk_forward, evidence_thresholds)
    return EvidenceResult(
        source=source,
        symbol=symbol,
        dataset=dataset,
        verdict=verdict,
        reasons=reasons,
        walk_forward=walk_forward,
        regime_performance=_regime_performance(walk_forward, observations),
    )


def _parameters_dict(parameters: ResearchParameters) -> dict[str, int | float]:
    return {
        "target_dte": parameters.target_dte,
        "short_delta": parameters.short_delta,
        "wing_width": parameters.wing_width,
        "profit_target_fraction": parameters.profit_target_fraction,
        "stop_loss_credit_multiple": parameters.stop_loss_credit_multiple,
        "exit_dte": parameters.exit_dte,
        "max_risk_fraction": parameters.max_risk_fraction,
    }


def evidence_to_dict(result: EvidenceResult) -> dict[str, object]:
    """Return a compact, stable report schema without embedding raw option history."""
    dataset = result.dataset
    payload: dict[str, object] = {
        "schema_version": 1,
        "source": result.source,
        "symbol": result.symbol,
        "verdict": result.verdict.value,
        "live_ready": result.live_ready,
        "reasons": list(result.reasons),
        "dataset": {
            "fingerprint_sha256": dataset.fingerprint,
            "research_grade": dataset.research_grade,
            "start_date": dataset.start_date.isoformat(),
            "end_date": dataset.end_date.isoformat(),
            "snapshot_count": dataset.snapshot_count,
            "quote_count": dataset.quote_count,
            "weekday_coverage": dataset.weekday_coverage,
            "contract_continuity": dataset.contract_continuity,
            "dte_coverage": dataset.dte_coverage,
            "delta_coverage": dataset.delta_coverage,
            "wing_coverage": dataset.wing_coverage,
            "executable_coverage": dataset.executable_coverage,
            "missing_iv_fraction": dataset.missing_iv_fraction,
            "wide_quote_fraction": dataset.wide_quote_fraction,
            "issues": list(dataset.issues),
        },
        "regimes": [
            {
                "regime": item.regime.value,
                "trade_count": item.trade_count,
                "win_rate": item.win_rate,
                "net_pnl": item.net_pnl,
                "average_trade": item.average_trade,
                "profit_factor": None if math.isinf(item.profit_factor) else item.profit_factor,
            }
            for item in result.regime_performance
        ],
    }

    if result.walk_forward is None:
        payload["oos"] = None
        payload["folds"] = []
        return payload

    walk = result.walk_forward
    payload["oos"] = {
        "fold_count": len(walk.folds),
        "tested_snapshot_fraction": walk.tested_snapshot_fraction,
        "trade_count": walk.oos_trade_count,
        "win_rate": walk.oos_win_rate,
        "total_return": walk.oos_total_return,
        "max_drawdown": walk.oos_max_drawdown,
        "profit_factor": (
            None if math.isinf(result.oos_profit_factor) else result.oos_profit_factor
        ),
        "positive_fold_fraction": result.positive_fold_fraction,
        "selection_stability": walk.selection_stability,
        "cash_total_return": walk.cash_total_return,
        "buy_hold_total_return": walk.buy_hold_total_return,
    }
    payload["folds"] = [
        {
            "index": fold.index,
            "train_start": fold.train_start.isoformat(),
            "train_end": fold.train_end.isoformat(),
            "test_start": fold.test_start.isoformat(),
            "test_end": fold.test_end.isoformat(),
            "candidate_count": fold.candidate_count,
            "selected_parameters": _parameters_dict(fold.selected_parameters),
            "selection_metric": fold.selection_metric,
            "selection_score": fold.selection_score,
            "train_return": fold.train_metrics.total_return,
            "test_return": fold.test_metrics.total_return,
            "test_max_drawdown": fold.test_metrics.max_drawdown,
            "test_trade_count": fold.test_metrics.trade_count,
            "buy_hold_return": fold.buy_hold_return,
        }
        for fold in walk.folds
    ]
    return payload


def render_evidence_json(result: EvidenceResult) -> str:
    return json.dumps(evidence_to_dict(result), indent=2, sort_keys=True, allow_nan=False) + "\n"


def render_evidence_markdown(result: EvidenceResult) -> str:
    dataset = result.dataset
    lines = [
        f"# CondorPilot Evidence Report — {result.symbol}",
        "",
        f"**Research verdict:** `{result.verdict.value}`  ",
        "**Live trading ready:** `NO`  ",
        f"**Source:** `{result.source}`  ",
        f"**Dataset fingerprint:** `{dataset.fingerprint}`",
        "",
        "> This report is research evidence only. It never authorizes paper or live execution.",
        "",
        "## Dataset quality",
        "",
        f"- Research grade: **{dataset.research_grade}**",
        f"- Period: {dataset.start_date} to {dataset.end_date}",
        f"- Snapshots: {dataset.snapshot_count:,}",
        f"- Weekday coverage: {dataset.weekday_coverage:.2%}",
        f"- Contract continuity: {dataset.contract_continuity:.2%}",
        f"- Target-DTE coverage: {dataset.dte_coverage:.2%}",
        f"- Target-delta coverage: {dataset.delta_coverage:.2%}",
        f"- Exact-wing coverage: {dataset.wing_coverage:.2%}",
        f"- Executable-condor coverage: {dataset.executable_coverage:.2%}",
        "",
        "## Verdict reasons",
        "",
    ]
    lines.extend(f"- {reason}" for reason in result.reasons)

    if result.walk_forward is not None:
        walk = result.walk_forward
        pf = "inf" if math.isinf(result.oos_profit_factor) else f"{result.oos_profit_factor:.2f}"
        lines.extend(
            [
                "",
                "## Out-of-sample evidence",
                "",
                f"- Folds: {len(walk.folds)}",
                f"- OOS trades: {walk.oos_trade_count}",
                f"- OOS return: {walk.oos_total_return:+.2%}",
                f"- OOS max drawdown: {walk.oos_max_drawdown:.2%}",
                f"- OOS win rate: {walk.oos_win_rate:.2%}",
                f"- OOS profit factor: {pf}",
                f"- Positive folds: {result.positive_fold_fraction:.2%}",
                f"- Parameter selection stability: {walk.selection_stability:.2%}",
                f"- Cash benchmark: {walk.cash_total_return:+.2%}",
                f"- Price-only Buy & Hold benchmark: {walk.buy_hold_total_return:+.2%}",
                "",
                "## OOS folds",
                "",
                "| Fold | Test period | Selected parameters | OOS return | Buy & Hold | Trades |",
                "| ---: | --- | --- | ---: | ---: | ---: |",
            ]
        )
        for fold in walk.folds:
            p = fold.selected_parameters
            params = (
                f"{p.target_dte}DTE / {p.short_delta:.2f}Δ / {p.wing_width:g}w / "
                f"TP {p.profit_target_fraction:.0%} / SL {p.stop_loss_credit_multiple:g}x / "
                f"exit {p.exit_dte}DTE"
            )
            lines.append(
                f"| {fold.index} | {fold.test_start.date()} → {fold.test_end.date()} | "
                f"{params} | {fold.test_metrics.total_return:+.2%} | "
                f"{fold.buy_hold_return:+.2%} | {fold.test_metrics.trade_count} |"
            )

    if result.regime_performance:
        lines.extend(
            [
                "",
                "## OOS trade performance by entry volatility regime",
                "",
                "| Regime | Trades | Win rate | Net P/L | Avg trade | Profit factor |",
                "| --- | ---: | ---: | ---: | ---: | ---: |",
            ]
        )
        for item in result.regime_performance:
            pf = "inf" if math.isinf(item.profit_factor) else f"{item.profit_factor:.2f}"
            lines.append(
                f"| {item.regime.value} | {item.trade_count} | {item.win_rate:.1%} | "
                f"${item.net_pnl:+,.2f} | ${item.average_trade:+,.2f} | {pf} |"
            )

    return "\n".join(lines) + "\n"


def write_evidence_report(
    result: EvidenceResult,
    output_dir: str | Path,
) -> tuple[Path, Path]:
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    json_path = directory / "evidence.json"
    markdown_path = directory / "evidence.md"
    json_path.write_text(render_evidence_json(result), encoding="utf-8")
    markdown_path.write_text(render_evidence_markdown(result), encoding="utf-8")
    return json_path, markdown_path
