"""Command-line interface for CondorPilot."""

from __future__ import annotations

import argparse
from datetime import date

from condorpilot.market import SyntheticChainSpec, build_synthetic_chain
from condorpilot.models import StrategyConfig
from condorpilot.risk import contracts_for_risk_budget
from condorpilot.strategy import NoTradeError, build_iron_condor


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
    print(
        f"Breakevens: {condor.lower_breakeven:.2f} - {condor.upper_breakeven:.2f}"
    )
    print(
        f"Risk budget: {config.max_risk_fraction:.1%} of ${args.account_equity:,.2f} "
        f"=> {contracts} contract(s)"
    )
    return 0


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
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return args.handler(args)
