"""Command-line interface for CondorPilot."""

from __future__ import annotations

import argparse
import math
from datetime import UTC, date, datetime

from condorpilot.backtest import BacktestConfig, run_backtest
from condorpilot.evidence import EvidenceThresholds, run_evidence, save_evidence_json
from condorpilot.execution import ExecutionConfig
from condorpilot.history import OptionChainSnapshot, build_synthetic_history
from condorpilot.importers import load_option_chain_csv, save_option_chain_csv
from condorpilot.market import SyntheticChainSpec, build_synthetic_chain
from condorpilot.models import StrategyConfig
from condorpilot.research import ParameterGrid, rank_runs, run_parameter_sweep
from condorpilot.risk import contracts_for_risk_budget
from condorpilot.strategy import NoTradeError, build_iron_condor
from condorpilot.validation import WalkForwardConfig, WalkForwardError
from condorpilot.vendors.thetadata import ThetaDataClient, ThetaDataConfig
from condorpilot.volatility import (
    VolatilityRegime,
    build_regime_entry_filter,
    build_volatility_regimes,
    fetch_cboe_vix_history,
    load_vix_csv,
    regime_counts,
)


def _int_list(value: str) -> tuple[int, ...]:
    try:
        parsed = tuple(int(item.strip()) for item in value.split(",") if item.strip())
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected comma-separated integers") from exc
    if not parsed:
        raise argparse.ArgumentTypeError("list must not be empty")
    return parsed


def _float_list(value: str) -> tuple[float, ...]:
    try:
        parsed = tuple(float(item.strip()) for item in value.split(",") if item.strip())
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected comma-separated numbers") from exc
    if not parsed:
        raise argparse.ArgumentTypeError("list must not be empty")
    return parsed


def _regime_list(value: str) -> tuple[VolatilityRegime, ...]:
    try:
        parsed = tuple(
            VolatilityRegime(item.strip().lower())
            for item in value.split(",")
            if item.strip()
        )
    except ValueError as exc:
        choices = ", ".join(regime.value for regime in VolatilityRegime)
        raise argparse.ArgumentTypeError(f"regimes must be selected from: {choices}") from exc
    if not parsed:
        raise argparse.ArgumentTypeError("regime list must not be empty")
    return parsed


def _date_value(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("date must use YYYY-MM-DD") from exc


def _demo(args: argparse.Namespace) -> int:
    as_of = date.today()
    spec = SyntheticChainSpec(
        symbol=args.symbol,
        spot=args.spot,
        dte=args.dte,
        volatility=args.iv,
        risk_free_rate=args.rate,
        strike_increment=args.strike_increment,
    )
    config = StrategyConfig(
        target_dte=args.dte,
        short_delta=args.delta,
        wing_width=args.wing_width,
        max_risk_fraction=args.max_risk_fraction,
        min_credit_to_width=args.min_credit_to_width,
    )
    chain = build_synthetic_chain(spec, as_of=as_of)

    try:
        condor = build_iron_condor(chain, spot=args.spot, as_of=as_of, config=config)
    except NoTradeError as exc:
        print(f"No trade: {exc}")
        return 2

    contracts = contracts_for_risk_budget(
        account_equity=args.account_equity,
        max_loss_per_contract=condor.max_loss,
        max_risk_fraction=config.max_risk_fraction,
    )

    print(f"CondorPilot demo | {condor.symbol} spot={args.spot:.2f}")
    print(f"Expiration: {condor.expiration} ({args.dte} DTE target)")
    print(
        "Legs: "
        f"+{condor.long_put.strike:g}P "
        f"-{condor.short_put.strike:g}P "
        f"-{condor.short_call.strike:g}C "
        f"+{condor.long_call.strike:g}C"
    )
    print(
        "Short deltas: "
        f"put={condor.short_put.delta:.3f}, call={condor.short_call.delta:.3f}"
    )
    print(f"Net credit: ${condor.net_credit:.2f} / share")
    print(f"Max profit: ${condor.max_profit:.2f} / contract")
    print(f"Max loss: ${condor.max_loss:.2f} / contract")
    print(f"Breakevens: {condor.lower_breakeven:.2f} - {condor.upper_breakeven:.2f}")
    print(
        f"Risk budget: {config.max_risk_fraction:.1%} of ${args.account_equity:,.2f} "
        f"=> {contracts} contract(s)"
    )
    return 0


def _synthetic_spots(args: argparse.Namespace) -> list[float]:
    return [
        args.spot
        * (1.0 + args.trend_per_day * index + args.swing * math.sin(index / 6.0))
        for index in range(args.days)
    ]


def _synthetic_history(args: argparse.Namespace) -> tuple[OptionChainSnapshot, ...]:
    target_dte = getattr(args, "dte", None)
    if target_dte is None:
        target_dte = min(getattr(args, "dtes", (45,)))
    return build_synthetic_history(
        symbol=args.symbol,
        start=datetime(2025, 1, 2, 21, 0, tzinfo=UTC),
        spots=_synthetic_spots(args),
        volatility=args.iv,
        risk_free_rate=args.rate,
        target_dte=target_dte,
        expiration_interval_days=15,
        strike_increment=args.strike_increment,
    )


def _load_history(args: argparse.Namespace) -> tuple[OptionChainSnapshot, ...]:
    if getattr(args, "csv", None):
        return load_option_chain_csv(args.csv)
    return _synthetic_history(args)


def _load_vix(args: argparse.Namespace):
    if getattr(args, "vix_csv", None):
        return load_vix_csv(args.vix_csv)
    if getattr(args, "fetch_cboe_vix", False):
        return fetch_cboe_vix_history()
    return ()


def _backtest_demo(args: argparse.Namespace) -> int:
    history = _synthetic_history(args)
    strategy = StrategyConfig(
        target_dte=args.dte,
        short_delta=args.delta,
        wing_width=args.wing_width,
        profit_target_fraction=args.profit_target,
        stop_loss_credit_multiple=args.stop_multiple,
        exit_dte=args.exit_dte,
        max_risk_fraction=args.max_risk_fraction,
        min_credit_to_width=args.min_credit_to_width,
    )
    execution = ExecutionConfig(
        slippage_fraction=args.slippage,
        commission_per_contract_per_leg=args.commission,
    )
    result = run_backtest(
        history,
        config=BacktestConfig(
            initial_equity=args.account_equity,
            strategy=strategy,
            execution=execution,
        ),
    )

    print(f"CondorPilot backtest demo | {args.symbol} | {args.days} synthetic snapshots")
    print(
        f"Equity: ${result.initial_equity:,.2f} -> ${result.final_equity:,.2f} "
        f"({result.total_return:+.2%})"
    )
    print(
        f"Trades: {len(result.trades)} | Win rate: {result.win_rate:.1%} | "
        f"Max drawdown: {result.max_drawdown:.2%}"
    )
    for trade in result.trades:
        print(
            f"{trade.opened_at.date()} -> {trade.closed_at.date()} "
            f"{trade.exit_reason.value:>11} | {trade.contracts}x | "
            f"credit {trade.entry_credit:.2f} -> debit {trade.exit_debit:.2f} | "
            f"net ${trade.net_pnl:+.2f}"
        )
    return 0


def _grid_from_args(args: argparse.Namespace) -> ParameterGrid:
    return ParameterGrid(
        target_dte=args.dtes,
        short_delta=args.deltas,
        wing_width=args.wing_widths,
        profit_target_fraction=args.profit_targets,
        stop_loss_credit_multiple=args.stop_multiples,
        exit_dte=args.exit_dtes,
        max_risk_fraction=args.risk_fractions,
    )


def _research(args: argparse.Namespace) -> int:
    grid = _grid_from_args(args)
    history = _load_history(args)
    source = args.csv if args.csv else f"synthetic:{args.symbol}:{args.days}d"
    entry_filter = None
    regime_note = "all"
    if args.allowed_regimes:
        observations = build_volatility_regimes(
            history,
            vix_history=_load_vix(args),
            iv_lookback=args.iv_lookback,
            target_iv_dte=args.target_iv_dte,
        )
        entry_filter = build_regime_entry_filter(
            observations,
            allowed_regimes=args.allowed_regimes,
        )
        regime_note = ",".join(item.value for item in args.allowed_regimes)

    base = BacktestConfig(
        initial_equity=args.account_equity,
        strategy=StrategyConfig(min_credit_to_width=args.min_credit_to_width),
        execution=ExecutionConfig(
            slippage_fraction=args.slippage,
            commission_per_contract_per_leg=args.commission,
        ),
    )
    runs = run_parameter_sweep(
        history,
        grid=grid,
        base_config=base,
        entry_filter=entry_filter,
    )
    if not runs:
        print("No valid research cases after applying strategy constraints.")
        return 2
    ranked = rank_runs(runs, metric=args.rank_by)

    print(
        f"CondorPilot research | source={source} | snapshots={len(history)} | "
        f"cases={len(runs)} | rank={args.rank_by} | entry_regimes={regime_note}"
    )
    print("rank dte delta wing tp stop exit risk return cagr maxdd sharpe sortino win pf trades")
    for index, run in enumerate(ranked[: args.top], start=1):
        p = run.parameters
        m = run.metrics
        profit_factor = "inf" if math.isinf(m.profit_factor) else f"{m.profit_factor:.2f}"
        print(
            f"{index:>4} {p.target_dte:>3} {p.short_delta:>5.2f} {p.wing_width:>4g} "
            f"{p.profit_target_fraction:>4.0%} {p.stop_loss_credit_multiple:>4g} "
            f"{p.exit_dte:>4} {p.max_risk_fraction:>4.1%} {m.total_return:>+7.2%} "
            f"{m.cagr:>+7.2%} {m.max_drawdown:>6.2%} {m.sharpe:>6.2f} "
            f"{m.sortino:>7.2f} {m.win_rate:>5.1%} {profit_factor:>5} {m.trade_count:>6}"
        )
    return 0


def _evidence(args: argparse.Namespace) -> int:
    history = load_option_chain_csv(args.csv)
    base = BacktestConfig(
        initial_equity=args.account_equity,
        strategy=StrategyConfig(min_credit_to_width=args.min_credit_to_width),
        execution=ExecutionConfig(
            slippage_fraction=args.slippage,
            commission_per_contract_per_leg=args.commission,
        ),
    )
    walk_forward = WalkForwardConfig(
        train_size=args.train_size,
        test_size=args.test_size,
        step_size=args.step_size,
        anchored=args.anchored,
        rank_by=args.rank_by,
        min_train_trades=args.min_train_trades,
        require_dataset_pass=True,
    )
    thresholds = EvidenceThresholds(
        minimum_oos_folds=args.minimum_oos_folds,
        minimum_oos_trades=args.minimum_oos_trades,
        minimum_tested_snapshot_fraction=args.minimum_tested_fraction,
        minimum_known_regime_trade_fraction=args.minimum_known_regime_fraction,
        minimum_selection_stability=args.minimum_selection_stability,
        minimum_oos_total_return=args.minimum_oos_return,
        maximum_oos_drawdown=args.maximum_oos_drawdown,
        minimum_profitable_fold_fraction=args.minimum_profitable_fold_fraction,
        minimum_excess_return_vs_buy_hold=args.minimum_excess_vs_buy_hold,
    )
    try:
        result = run_evidence(
            history,
            vix_history=_load_vix(args),
            grid=_grid_from_args(args),
            base_config=base,
            walk_forward_config=walk_forward,
            evidence_thresholds=thresholds,
            iv_lookback=args.iv_lookback,
            target_iv_dte=args.target_iv_dte,
        )
    except WalkForwardError as exc:
        print(f"Evidence run rejected: {exc}")
        return 2

    save_evidence_json(result, args.output_json)
    wf = result.walk_forward
    print(
        f"CondorPilot evidence | {wf.dataset.symbol} | verdict={result.verdict.value} | "
        f"live_trading=NO-GO"
    )
    print(f"Dataset fingerprint:    {wf.dataset.fingerprint}")
    print(f"Experiment fingerprint: {result.experiment_fingerprint}")
    print(
        f"OOS: folds={len(wf.folds)} trades={wf.oos_trade_count} "
        f"return={wf.oos_total_return:+.2%} maxdd={wf.oos_max_drawdown:.2%} "
        f"win={wf.oos_win_rate:.1%}"
    )
    print(
        f"Benchmarks: cash={wf.cash_total_return:+.2%} buy_hold={wf.buy_hold_total_return:+.2%} "
        f"excess_vs_buy_hold={result.excess_return_vs_buy_hold:+.2%}"
    )
    print(
        f"Coverage: tested={wf.tested_snapshot_fraction:.1%} "
        f"regime_known={result.known_regime_trade_fraction:.1%} "
        f"parameter_stability={wf.selection_stability:.1%}"
    )
    print("regime   trades    net_pnl    avg_pnl   win_rate")
    for item in result.regime_evidence:
        print(
            f"{item.regime.value:>7} {item.trade_count:>7} "
            f"{item.net_pnl:>+10.2f} {item.average_trade_pnl:>+10.2f} "
            f"{item.win_rate:>9.1%}"
        )
    if result.reasons:
        print("Verdict reasons:")
        for reason in result.reasons:
            print(f"- {reason}")
    print(f"Evidence JSON: {args.output_json}")
    return 0


def _regimes(args: argparse.Namespace) -> int:
    history = _load_history(args)
    observations = build_volatility_regimes(
        history,
        vix_history=_load_vix(args),
        iv_lookback=args.iv_lookback,
        target_iv_dte=args.target_iv_dte,
    )
    counts = regime_counts(observations)
    print(
        "CondorPilot regimes | "
        + " ".join(f"{regime.value}={counts[regime]}" for regime in VolatilityRegime)
    )
    print("date       atm_iv iv_pct iv_rank vix   regime")
    for item in observations[-args.tail :]:
        atm_iv = "-" if item.atm_iv is None else f"{item.atm_iv:.3f}"
        iv_pct = "-" if item.iv_percentile is None else f"{item.iv_percentile:.0%}"
        iv_rank = "-" if item.iv_rank is None else f"{item.iv_rank:.0%}"
        vix = "-" if item.vix_close is None else f"{item.vix_close:.2f}"
        print(
            f"{item.observed_on} {atm_iv:>6} {iv_pct:>6} {iv_rank:>7} "
            f"{vix:>5} {item.regime.value}"
        )
    return 0


def _import_thetadata(args: argparse.Namespace) -> int:
    config = ThetaDataConfig(
        base_url=args.base_url,
        interval=args.interval,
        start_time=args.start_time,
        end_time=args.end_time,
        max_dte=args.max_dte,
        strike_range=args.strike_range,
    )
    history = ThetaDataClient(config).fetch_history(
        args.symbol,
        start_date=args.start_date,
        end_date=args.end_date,
    )
    save_option_chain_csv(history, args.output)
    print(
        f"Saved {len(history)} ThetaData snapshots for {args.symbol.upper()} "
        f"to {args.output}"
    )
    return 0


def _add_synthetic_market_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--symbol", default="SPY")
    parser.add_argument("--spot", type=float, default=650.0)
    parser.add_argument("--days", type=int, default=90)
    parser.add_argument("--iv", type=float, default=0.25)
    parser.add_argument("--rate", type=float, default=0.04)
    parser.add_argument("--strike-increment", type=float, default=5.0)
    parser.add_argument("--trend-per-day", type=float, default=0.0002)
    parser.add_argument("--swing", type=float, default=0.012)


def _add_regime_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--vix-csv", help="Cboe-compatible daily VIX CSV")
    parser.add_argument(
        "--fetch-cboe-vix",
        action="store_true",
        help="Fetch Cboe's public VIX history directly",
    )
    parser.add_argument("--iv-lookback", type=int, default=252)
    parser.add_argument("--target-iv-dte", type=int, default=30)


def _add_grid_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--dtes", type=_int_list, default=(30, 45, 60))
    parser.add_argument("--deltas", type=_float_list, default=(0.10, 0.15, 0.20))
    parser.add_argument("--wing-widths", type=_float_list, default=(5.0,))
    parser.add_argument("--profit-targets", type=_float_list, default=(0.50,))
    parser.add_argument("--stop-multiples", type=_float_list, default=(2.0,))
    parser.add_argument("--exit-dtes", type=_int_list, default=(14, 21))
    parser.add_argument("--risk-fractions", type=_float_list, default=(0.01,))


def _add_execution_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--min-credit-to-width", type=float, default=0.10)
    parser.add_argument("--account-equity", type=float, default=50_000.0)
    parser.add_argument("--slippage", type=float, default=0.25)
    parser.add_argument("--commission", type=float, default=0.65)


def _add_rank_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--rank-by",
        choices=(
            "sortino",
            "sharpe",
            "cagr",
            "total_return",
            "win_rate",
            "profit_factor",
            "average_trade",
        ),
        default="sortino",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="condorpilot",
        description="Systematic Iron Condor strategy engine.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    demo = subparsers.add_parser("demo", help="Build a condor from a synthetic option chain")
    demo.add_argument("--symbol", default="SPY")
    demo.add_argument("--spot", type=float, default=650.0)
    demo.add_argument("--dte", type=int, default=45)
    demo.add_argument("--iv", type=float, default=0.22, help="Annualized IV as a decimal")
    demo.add_argument("--rate", type=float, default=0.04)
    demo.add_argument("--delta", type=float, default=0.15, help="Absolute target short delta")
    demo.add_argument("--wing-width", type=float, default=5.0)
    demo.add_argument("--strike-increment", type=float, default=5.0)
    demo.add_argument("--min-credit-to-width", type=float, default=0.10)
    demo.add_argument("--account-equity", type=float, default=50_000.0)
    demo.add_argument("--max-risk-fraction", type=float, default=0.02)
    demo.set_defaults(handler=_demo)

    backtest = subparsers.add_parser(
        "backtest-demo",
        help="Run the event-driven engine over deterministic synthetic option history",
    )
    _add_synthetic_market_args(backtest)
    backtest.add_argument("--dte", type=int, default=45)
    backtest.add_argument("--exit-dte", type=int, default=21)
    backtest.add_argument("--delta", type=float, default=0.15)
    backtest.add_argument("--wing-width", type=float, default=5.0)
    backtest.add_argument("--profit-target", type=float, default=0.50)
    backtest.add_argument("--stop-multiple", type=float, default=2.0)
    backtest.add_argument("--min-credit-to-width", type=float, default=0.10)
    backtest.add_argument("--account-equity", type=float, default=50_000.0)
    backtest.add_argument("--max-risk-fraction", type=float, default=0.02)
    backtest.add_argument("--slippage", type=float, default=0.25)
    backtest.add_argument("--commission", type=float, default=0.65)
    backtest.set_defaults(handler=_backtest_demo)

    research = subparsers.add_parser(
        "research",
        help="Sweep strategy parameters over CSV or synthetic option-chain history",
    )
    _add_synthetic_market_args(research)
    _add_regime_args(research)
    _add_grid_args(research)
    _add_execution_args(research)
    _add_rank_arg(research)
    research.add_argument("--csv", help="Long-form historical option-chain CSV")
    research.add_argument("--allowed-regimes", type=_regime_list)
    research.add_argument("--top", type=int, default=10)
    research.set_defaults(handler=_research)

    evidence = subparsers.add_parser(
        "evidence",
        help="Run strict CSV-backed walk-forward evidence and write an auditable JSON verdict",
    )
    _add_regime_args(evidence)
    _add_grid_args(evidence)
    _add_execution_args(evidence)
    _add_rank_arg(evidence)
    evidence.add_argument("--csv", required=True, help="Normalized historical option-chain CSV")
    evidence.add_argument("--output-json", required=True)
    evidence.add_argument("--train-size", type=int, default=504)
    evidence.add_argument("--test-size", type=int, default=63)
    evidence.add_argument("--step-size", type=int, default=63)
    evidence.add_argument("--anchored", action="store_true")
    evidence.add_argument("--min-train-trades", type=int, default=8)
    evidence.add_argument("--minimum-oos-folds", type=int, default=4)
    evidence.add_argument("--minimum-oos-trades", type=int, default=20)
    evidence.add_argument("--minimum-tested-fraction", type=float, default=0.25)
    evidence.add_argument("--minimum-known-regime-fraction", type=float, default=0.80)
    evidence.add_argument("--minimum-selection-stability", type=float, default=0.25)
    evidence.add_argument("--minimum-oos-return", type=float, default=0.0)
    evidence.add_argument("--maximum-oos-drawdown", type=float, default=0.25)
    evidence.add_argument("--minimum-profitable-fold-fraction", type=float, default=0.50)
    evidence.add_argument("--minimum-excess-vs-buy-hold", type=float)
    evidence.set_defaults(handler=_evidence)

    regimes = subparsers.add_parser(
        "regimes",
        help="Classify CSV or synthetic history into VIX/IV volatility regimes",
    )
    _add_synthetic_market_args(regimes)
    _add_regime_args(regimes)
    regimes.add_argument("--csv", help="Long-form historical option-chain CSV")
    regimes.add_argument("--tail", type=int, default=20)
    regimes.set_defaults(handler=_regimes)

    theta = subparsers.add_parser(
        "import-thetadata",
        help="Import daily historical option snapshots from a running Theta Terminal v3",
    )
    theta.add_argument("--symbol", default="SPY")
    theta.add_argument("--start-date", type=_date_value, required=True)
    theta.add_argument("--end-date", type=_date_value, required=True)
    theta.add_argument("--output", required=True)
    theta.add_argument("--base-url", default="http://127.0.0.1:25503/v3")
    theta.add_argument("--interval", default="30m")
    theta.add_argument("--start-time", default="15:30:00")
    theta.add_argument("--end-time", default="16:00:00")
    theta.add_argument("--max-dte", type=int, default=90)
    theta.add_argument("--strike-range", type=int, default=40)
    theta.set_defaults(handler=_import_thetadata)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return args.handler(args)
