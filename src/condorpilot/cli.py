"""Command-line interface for CondorPilot."""

from __future__ import annotations

import argparse
import math
from datetime import UTC, date, datetime

from condorpilot.backtest import BacktestConfig, run_backtest
from condorpilot.execution import ExecutionConfig
from condorpilot.history import build_synthetic_history
from condorpilot.importers import load_option_chain_csv
from condorpilot.market import SyntheticChainSpec, build_synthetic_chain
from condorpilot.models import StrategyConfig
from condorpilot.research import ParameterGrid, rank_runs, run_parameter_sweep
from condorpilot.risk import contracts_for_risk_budget
from condorpilot.strategy import NoTradeError, build_iron_condor


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


def _backtest_demo(args: argparse.Namespace) -> int:
    start = datetime(2025, 1, 2, 21, 0, tzinfo=UTC)
    history = build_synthetic_history(
        symbol=args.symbol,
        start=start,
        spots=_synthetic_spots(args),
        volatility=args.iv,
        risk_free_rate=args.rate,
        target_dte=args.dte,
        strike_increment=args.strike_increment,
    )
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

    print(f"CondorPilot backtest demo | {args.symbol} | {args.days} synthetic daily snapshots")
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


def _research(args: argparse.Namespace) -> int:
    grid = ParameterGrid(
        target_dte=args.dtes,
        short_delta=args.deltas,
        wing_width=args.wing_widths,
        profit_target_fraction=args.profit_targets,
        stop_loss_credit_multiple=args.stop_multiples,
        exit_dte=args.exit_dtes,
        max_risk_fraction=args.risk_fractions,
    )
    if args.csv:
        history = load_option_chain_csv(args.csv)
        source = args.csv
    else:
        history = build_synthetic_history(
            symbol=args.symbol,
            start=datetime(2025, 1, 2, 21, 0, tzinfo=UTC),
            spots=_synthetic_spots(args),
            volatility=args.iv,
            risk_free_rate=args.rate,
            target_dte=min(args.dtes),
            expiration_interval_days=15,
            strike_increment=args.strike_increment,
        )
        source = f"synthetic:{args.symbol}:{args.days}d"

    base = BacktestConfig(
        initial_equity=args.account_equity,
        strategy=StrategyConfig(min_credit_to_width=args.min_credit_to_width),
        execution=ExecutionConfig(
            slippage_fraction=args.slippage,
            commission_per_contract_per_leg=args.commission,
        ),
    )
    runs = run_parameter_sweep(history, grid=grid, base_config=base)
    if not runs:
        print("No valid research cases after applying strategy constraints.")
        return 2
    ranked = rank_runs(runs, metric=args.rank_by)

    print(
        f"CondorPilot research | source={source} | snapshots={len(history)} | "
        f"cases={len(runs)} | rank={args.rank_by}"
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


def _add_synthetic_market_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--symbol", default="SPY")
    parser.add_argument("--spot", type=float, default=650.0)
    parser.add_argument("--days", type=int, default=90)
    parser.add_argument("--iv", type=float, default=0.25)
    parser.add_argument("--rate", type=float, default=0.04)
    parser.add_argument("--strike-increment", type=float, default=5.0)
    parser.add_argument("--trend-per-day", type=float, default=0.0002)
    parser.add_argument("--swing", type=float, default=0.012)


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
    research.add_argument("--csv", help="Long-form historical option-chain CSV")
    research.add_argument("--dtes", type=_int_list, default=(30, 45, 60))
    research.add_argument("--deltas", type=_float_list, default=(0.10, 0.15, 0.20))
    research.add_argument("--wing-widths", type=_float_list, default=(5.0,))
    research.add_argument("--profit-targets", type=_float_list, default=(0.50,))
    research.add_argument("--stop-multiples", type=_float_list, default=(2.0,))
    research.add_argument("--exit-dtes", type=_int_list, default=(14, 21))
    research.add_argument("--risk-fractions", type=_float_list, default=(0.01,))
    research.add_argument("--min-credit-to-width", type=float, default=0.10)
    research.add_argument("--account-equity", type=float, default=50_000.0)
    research.add_argument("--slippage", type=float, default=0.25)
    research.add_argument("--commission", type=float, default=0.65)
    research.add_argument(
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
    research.add_argument("--top", type=int, default=10)
    research.set_defaults(handler=_research)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return args.handler(args)
