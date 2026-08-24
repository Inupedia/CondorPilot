"""Predeclared real-data sensitivity study for CondorPilot.

Discovery uses SPY 2016-2019 only. A candidate must clear fixed research gates before the
highest training profit factor is frozen and evaluated on SPY 2020-2025. The candidate list is
intentionally small and one-factor-heavy to reduce optimization degrees of freedom.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import duckdb

from condorpilot.execution import ExecutionConfig, close_debit, entry_credit
from condorpilot.models import StrategyConfig
from condorpilot.risk import ExitAction, contracts_for_risk_budget, evaluate_exit
from condorpilot.strategy import NoTradeError, build_iron_condor
from scripts.real_spy_baseline import (
    Position,
    Trade,
    _entry_chain,
    _mark_condor,
    _marked_equity,
    _max_drawdown,
    _prepare_database,
    _profit_factor,
)


@dataclass(frozen=True, slots=True)
class Variant:
    name: str
    delta: float = 0.15
    wing: float = 5.0
    profit_target: float = 0.50
    stop_multiple: float = 2.0
    exit_dte: int = 21
    slippage: float = 0.25
    commission: float = 0.65

    def strategy(self) -> StrategyConfig:
        return StrategyConfig(
            target_dte=45,
            max_dte_deviation_days=7,
            short_delta=self.delta,
            max_delta_deviation=0.05,
            wing_width=self.wing,
            max_bid_ask_spread_fraction=0.75,
            profit_target_fraction=self.profit_target,
            stop_loss_credit_multiple=self.stop_multiple,
            exit_dte=self.exit_dte,
            max_risk_fraction=0.02,
            min_credit_to_width=0.10,
        )

    def execution(self) -> ExecutionConfig:
        return ExecutionConfig(
            slippage_fraction=self.slippage,
            commission_per_contract_per_leg=self.commission,
        )


CANDIDATES = (
    Variant("baseline"),
    Variant("delta_10", delta=0.10),
    Variant("delta_20", delta=0.20),
    Variant("wing_10", wing=10.0),
    Variant("tp_25", profit_target=0.25),
    Variant("tp_75", profit_target=0.75),
    Variant("stop_1_5", stop_multiple=1.5),
    Variant("stop_3", stop_multiple=3.0),
    Variant("no_stop", stop_multiple=100.0),
    Variant("exit_14", exit_dte=14),
    Variant("delta_10_wing_10", delta=0.10, wing=10.0),
    Variant("delta_20_wing_10", delta=0.20, wing=10.0),
)

DISCOVERY_START = date(2016, 1, 4)
DISCOVERY_END = date(2019, 12, 31)
VALIDATION_START = date(2020, 1, 2)
VALIDATION_END = date(2025, 12, 12)
MIN_DISCOVERY_TRADES = 40
MIN_DISCOVERY_PF = 1.0
MAX_DISCOVERY_DRAWDOWN = 0.10


def _run_variant(
    con: duckdb.DuckDBPyConnection,
    variant: Variant,
    *,
    start: date,
    end: date,
    initial_equity: float = 50_000.0,
) -> dict[str, object]:
    strategy = variant.strategy()
    execution = variant.execution()
    realized_equity = initial_equity
    position: Position | None = None
    trades: list[Trade] = []
    no_trade_reasons: Counter[str] = Counter()
    stale_events: list[dict[str, object]] = []
    equity_rows: list[tuple[date, float, bool]] = []
    entry_attempts = 0
    entry_embargo_days = (
        strategy.target_dte + strategy.max_dte_deviation_days - strategy.exit_dte
    )

    underlying_rows = con.execute(
        """
        SELECT quote_date, close, adjusted_close
        FROM underlying
        WHERE quote_date BETWEEN ? AND ?
        ORDER BY quote_date
        """,
        [start, end],
    ).fetchall()
    if not underlying_rows:
        raise RuntimeError(f"underlying history is empty for {start} through {end}")
    final_day = underlying_rows[-1][0]

    for index, (day, spot_raw, _adjusted) in enumerate(underlying_rows):
        spot = float(spot_raw)
        previous_day = underlying_rows[index - 1][0] if index > 0 else None

        if position is not None:
            marked, stale = _mark_condor(con, day, previous_day, position.condor)
            stale_events.extend(stale)
            debit = close_debit(marked, execution)
            dte = (position.condor.expiration - day).days
            if dte < 0:
                raise RuntimeError(
                    f"crossed expiration without exit: {position.condor.expiration} vs {day}"
                )
            action = evaluate_exit(
                entry_credit=position.entry_credit,
                current_close_debit=debit,
                dte=dte,
                config=strategy,
            )
            if action is ExitAction.HOLD:
                equity_rows.append(
                    (day, _marked_equity(realized_equity, position, debit, execution), True)
                )
                continue

            exit_commission = execution.commission(position.contracts)
            trade = Trade(
                opened_on=position.opened_on,
                closed_on=day,
                expiration=position.condor.expiration,
                contracts=position.contracts,
                entry_spot=position.entry_spot,
                exit_spot=spot,
                entry_credit=position.entry_credit,
                exit_debit=debit,
                entry_commission=position.entry_commission,
                exit_commission=exit_commission,
                reason=action.value,
                strikes=(
                    position.condor.long_put.strike,
                    position.condor.short_put.strike,
                    position.condor.short_call.strike,
                    position.condor.long_call.strike,
                ),
            )
            trades.append(trade)
            realized_equity += trade.net_pnl
            position = None
            equity_rows.append((day, realized_equity, False))
            continue

        if (final_day - day).days < entry_embargo_days:
            equity_rows.append((day, realized_equity, False))
            continue

        entry_attempts += 1
        chain = _entry_chain(con, day, strategy)
        try:
            condor = build_iron_condor(chain, spot=spot, as_of=day, config=strategy)
        except NoTradeError as exc:
            no_trade_reasons[str(exc)] += 1
            equity_rows.append((day, realized_equity, False))
            continue

        credit = entry_credit(condor, execution)
        if credit <= 0 or credit / condor.max_width < strategy.min_credit_to_width:
            no_trade_reasons["modeled execution credit below entry threshold"] += 1
            equity_rows.append((day, realized_equity, False))
            continue
        max_loss = (condor.max_width - credit) * 100 + execution.commission(1) * 2
        contracts = contracts_for_risk_budget(
            account_equity=realized_equity,
            max_loss_per_contract=max_loss,
            max_risk_fraction=strategy.max_risk_fraction,
        )
        if contracts <= 0:
            no_trade_reasons["risk budget cannot fund one contract"] += 1
            equity_rows.append((day, realized_equity, False))
            continue

        position = Position(
            condor=condor,
            opened_on=day,
            entry_spot=spot,
            entry_credit=credit,
            contracts=contracts,
            entry_commission=execution.commission(contracts),
        )
        immediate_debit = close_debit(condor, execution)
        equity_rows.append(
            (day, _marked_equity(realized_equity, position, immediate_debit, execution), True)
        )

    if position is not None:
        raise RuntimeError(f"{variant.name}: end-of-period entry embargo left an open position")

    equities = [item[1] for item in equity_rows]
    final_equity = equities[-1]
    winners = [trade for trade in trades if trade.net_pnl > 0]
    losers = [trade for trade in trades if trade.net_pnl < 0]
    profit_factor = _profit_factor(trades)
    return {
        "variant": variant.name,
        "parameters": {
            "delta": variant.delta,
            "wing": variant.wing,
            "profit_target": variant.profit_target,
            "stop_multiple": variant.stop_multiple,
            "exit_dte": variant.exit_dte,
            "slippage": variant.slippage,
            "commission": variant.commission,
        },
        "start": underlying_rows[0][0].isoformat(),
        "end": underlying_rows[-1][0].isoformat(),
        "trade_count": len(trades),
        "entry_attempt_days": entry_attempts,
        "total_return": final_equity / initial_equity - 1,
        "final_equity": final_equity,
        "max_drawdown": _max_drawdown(equities, initial_equity),
        "win_rate": len(winners) / len(trades) if trades else 0.0,
        "profit_factor": profit_factor,
        "average_trade": sum(item.net_pnl for item in trades) / len(trades) if trades else 0.0,
        "average_winner": (
            sum(item.net_pnl for item in winners) / len(winners) if winners else 0.0
        ),
        "average_loser": (
            sum(item.net_pnl for item in losers) / len(losers) if losers else 0.0
        ),
        "worst_trade": min((item.net_pnl for item in trades), default=0.0),
        "exit_reasons": dict(Counter(item.reason for item in trades)),
        "stale_mark_leg_count": len(stale_events),
        "stale_mark_day_count": len({item["mark_date"] for item in stale_events}),
        "top_no_trade_reasons": no_trade_reasons.most_common(5),
    }


def _discovery_pass(result: dict[str, object]) -> tuple[bool, tuple[str, ...]]:
    reasons: list[str] = []
    trades = int(result["trade_count"])
    pf = result["profit_factor"]
    total_return = float(result["total_return"])
    drawdown = float(result["max_drawdown"])
    if trades < MIN_DISCOVERY_TRADES:
        reasons.append(f"trades {trades} < {MIN_DISCOVERY_TRADES}")
    if pf is None or float(pf) <= MIN_DISCOVERY_PF:
        reasons.append(f"profit factor {pf} <= {MIN_DISCOVERY_PF}")
    if total_return <= 0:
        reasons.append(f"return {total_return:+.2%} <= 0")
    if drawdown >= MAX_DISCOVERY_DRAWDOWN:
        reasons.append(f"drawdown {drawdown:.2%} >= {MAX_DISCOVERY_DRAWDOWN:.0%}")
    return not reasons, tuple(reasons)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    con = _prepare_database(args.data_dir, DISCOVERY_START, VALIDATION_END)
    discovery: list[dict[str, object]] = []
    for variant in CANDIDATES:
        result = _run_variant(
            con,
            variant,
            start=DISCOVERY_START,
            end=DISCOVERY_END,
        )
        passed, reasons = _discovery_pass(result)
        result["discovery_pass"] = passed
        result["discovery_fail_reasons"] = list(reasons)
        discovery.append(result)
        pf = result["profit_factor"]
        pf_text = "inf" if pf is None else f"{float(pf):.2f}"
        print(
            f"TRAIN {variant.name:>18} trades={result['trade_count']:>3} "
            f"ret={float(result['total_return']):>+7.2%} "
            f"dd={float(result['max_drawdown']):>6.2%} "
            f"win={float(result['win_rate']):>6.1%} pf={pf_text:>5} "
            f"pass={passed}"
        )

    eligible = [item for item in discovery if item["discovery_pass"]]
    selected_name: str | None = None
    validation: dict[str, object] | None = None
    if eligible:
        selected = max(
            eligible,
            key=lambda item: (
                float("inf") if item["profit_factor"] is None else float(item["profit_factor"]),
                float(item["total_return"]),
            ),
        )
        selected_name = str(selected["variant"])
        selected_variant = next(item for item in CANDIDATES if item.name == selected_name)
        validation = _run_variant(
            con,
            selected_variant,
            start=VALIDATION_START,
            end=VALIDATION_END,
        )
        validation_pf = validation["profit_factor"]
        validation["validation_pass"] = (
            float(validation["total_return"]) > 0
            and validation_pf is not None
            and float(validation_pf) > 1.0
        )

    baseline_zero_cost = _run_variant(
        con,
        Variant("baseline_zero_cost", slippage=0.0, commission=0.0),
        start=VALIDATION_START,
        end=VALIDATION_END,
    )

    output = {
        "method": {
            "candidate_count": len(CANDIDATES),
            "discovery_period": [DISCOVERY_START.isoformat(), DISCOVERY_END.isoformat()],
            "validation_period": [VALIDATION_START.isoformat(), VALIDATION_END.isoformat()],
            "selection": "highest training profit factor among candidates passing fixed gates",
            "discovery_gates": {
                "minimum_trades": MIN_DISCOVERY_TRADES,
                "minimum_profit_factor_exclusive": MIN_DISCOVERY_PF,
                "minimum_total_return_exclusive": 0.0,
                "maximum_drawdown_exclusive": MAX_DISCOVERY_DRAWDOWN,
            },
            "validation_gate": "total_return > 0 and profit_factor > 1",
            "caveat": (
                "SPY 2020-2025 baseline was inspected before this study; candidate design is "
                "therefore not a pristine untouched holdout. External QQQ validation is required."
            ),
        },
        "discovery": discovery,
        "selected_variant": selected_name,
        "validation": validation,
        "cost_ablation_2020_2025": baseline_zero_cost,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, indent=2, sort_keys=True), encoding="utf-8")

    print(f"SELECTED={selected_name}")
    if validation is not None:
        pf = validation["profit_factor"]
        pf_text = "inf" if pf is None else f"{float(pf):.2f}"
        print(
            f"VALIDATION {selected_name} trades={validation['trade_count']} "
            f"ret={float(validation['total_return']):+.2%} "
            f"dd={float(validation['max_drawdown']):.2%} "
            f"win={float(validation['win_rate']):.1%} pf={pf_text} "
            f"pass={validation['validation_pass']}"
        )
    print(
        "ZERO_COST_BASELINE "
        f"ret={float(baseline_zero_cost['total_return']):+.2%} "
        f"pf={baseline_zero_cost['profit_factor']}"
    )
    print(f"RESULT_PATH={args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
