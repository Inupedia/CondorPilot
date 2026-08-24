"""Test predeclared Bull Put Spread entry-premium rules on real SPY history.

The directional structure remains frozen: 45 DTE target, 15-delta short put, exact 5-point
protective wing, 50% take profit, 2x-credit stop, 21 DTE exit, 2% account risk, existing
slippage and commissions. Only the entry-premium eligibility definition changes.

Discovery is SPY 2016-2019. The 2020-2025 temporal validation period is evaluated only for the
single discovery winner, and only when at least one candidate clears every predeclared gate.
No QQQ data is touched in this study.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Literal

import duckdb

from condorpilot.execution import ExecutionDataError
from condorpilot.models import StrategyConfig
from condorpilot.risk import ExitAction, contracts_for_risk_budget, evaluate_exit
from condorpilot.strategy import NoTradeError
from scripts.real_spy_baseline import _entry_chain, _max_drawdown, _prepare_database
from scripts.real_spy_side_attribution import (
    SpreadPosition,
    SpreadTrade,
    _build_spread,
    _execution,
    _held_spread_close,
    _marked_spread_equity,
    _profit_factor,
    _spread_close_value,
    _spread_commission,
    _spread_entry_credit,
)

DISCOVERY_START = date(2016, 1, 4)
DISCOVERY_END = date(2019, 12, 31)
VALIDATION_START = date(2020, 1, 2)
VALIDATION_END = date(2025, 12, 12)

MIN_DISCOVERY_TRADES = 40
MIN_DISCOVERY_PF = 1.10
MAX_DISCOVERY_DRAWDOWN = 0.10
MIN_VALIDATION_TRADES = 40
MIN_VALIDATION_PF = 1.00
MAX_VALIDATION_DRAWDOWN = 0.10

Metric = Literal["credit_width", "credit_max_loss", "annualized_return_on_risk"]


@dataclass(frozen=True, slots=True)
class PremiumRule:
    name: str
    metric: Metric
    threshold: float


RULES = (
    PremiumRule("credit_width_5", "credit_width", 0.05),
    PremiumRule("credit_max_loss_10", "credit_max_loss", 0.10),
    PremiumRule("annualized_ror_30", "annualized_return_on_risk", 0.30),
)


def _strategy_without_premium_gate() -> StrategyConfig:
    """Return the frozen strategy while disabling only the legacy credit/width gate."""
    return StrategyConfig(
        target_dte=45,
        max_dte_deviation_days=7,
        short_delta=0.15,
        max_delta_deviation=0.05,
        wing_width=5.0,
        max_bid_ask_spread_fraction=0.75,
        profit_target_fraction=0.50,
        stop_loss_credit_multiple=2.0,
        exit_dte=21,
        max_risk_fraction=0.02,
        min_credit_to_width=0.0,
    )


def _premium_metrics(*, credit: float, width: float, dte: int) -> dict[str, float]:
    if credit <= 0:
        raise ValueError("credit must be positive")
    if width <= credit:
        raise ValueError("defined-risk spread requires width greater than credit")
    if dte <= 0:
        raise ValueError("dte must be positive")
    max_loss_per_share = width - credit
    credit_width = credit / width
    credit_max_loss = credit / max_loss_per_share
    annualized = credit_max_loss * 365.0 / dte
    return {
        "credit_width": credit_width,
        "credit_max_loss": credit_max_loss,
        "annualized_return_on_risk": annualized,
    }


def _rule_accepts(rule: PremiumRule, metrics: dict[str, float]) -> bool:
    return metrics[rule.metric] >= rule.threshold


def _run_rule(
    con: duckdb.DuckDBPyConnection,
    rule: PremiumRule,
    *,
    start: date,
    end: date,
    initial_equity: float = 50_000.0,
) -> dict[str, object]:
    strategy = _strategy_without_premium_gate()
    execution = _execution()
    realized_equity = initial_equity
    position: SpreadPosition | None = None
    trades: list[SpreadTrade] = []
    stale_events: list[dict[str, object]] = []
    no_trade_reasons: Counter[str] = Counter()
    premium_rejections = 0
    premium_eligible_days = 0
    entry_attempts = 0
    accepted_metric_values: list[float] = []
    equity_rows: list[tuple[date, float, bool]] = []
    entry_embargo_days = (
        strategy.target_dte + strategy.max_dte_deviation_days - strategy.exit_dte
    )

    rows = con.execute(
        """
        SELECT quote_date, close
        FROM underlying
        WHERE quote_date BETWEEN ? AND ?
        ORDER BY quote_date
        """,
        [start, end],
    ).fetchall()
    if not rows:
        raise RuntimeError(f"underlying history is empty for {start} through {end}")
    final_day = rows[-1][0]

    for index, (day, spot_raw) in enumerate(rows):
        spot = float(spot_raw)
        previous_day = rows[index - 1][0] if index > 0 else None

        if position is not None:
            _marked, debit, stale = _held_spread_close(
                con,
                day=day,
                previous_day=previous_day,
                source=position.spread,
                execution=execution,
            )
            stale_events.extend(stale)
            dte = (position.spread.expiration - day).days
            if dte < 0:
                raise RuntimeError(
                    f"crossed put-spread expiration without exit: "
                    f"{position.spread.expiration} vs {day}"
                )
            action = evaluate_exit(
                entry_credit=position.entry_credit,
                current_close_debit=debit,
                dte=dte,
                config=strategy,
            )
            if action is ExitAction.HOLD:
                equity_rows.append(
                    (day, _marked_spread_equity(realized_equity, position, debit, execution), True)
                )
                continue

            exit_commission = _spread_commission(execution, position.contracts)
            trade = SpreadTrade(
                opened_on=position.opened_on,
                closed_on=day,
                expiration=position.spread.expiration,
                contracts=position.contracts,
                entry_spot=position.entry_spot,
                exit_spot=spot,
                entry_credit=position.entry_credit,
                exit_debit=debit,
                entry_commission=position.entry_commission,
                exit_commission=exit_commission,
                reason=action.value,
                strikes=(position.spread.long.strike, position.spread.short.strike),
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
            spread = _build_spread(chain, spot=spot, as_of=day, side="put", config=strategy)
            credit = _spread_entry_credit(spread, execution)
            immediate_debit = _spread_close_value(
                spread,
                execution,
                require_non_negative=True,
            )
        except (NoTradeError, ExecutionDataError) as exc:
            no_trade_reasons[str(exc)] += 1
            equity_rows.append((day, realized_equity, False))
            continue

        if credit <= 0 or spread.width <= credit:
            no_trade_reasons["modeled execution does not produce a valid defined-risk credit"] += 1
            equity_rows.append((day, realized_equity, False))
            continue

        dte = (spread.expiration - day).days
        metrics = _premium_metrics(credit=credit, width=spread.width, dte=dte)
        if not _rule_accepts(rule, metrics):
            premium_rejections += 1
            equity_rows.append((day, realized_equity, False))
            continue

        premium_eligible_days += 1
        accepted_metric_values.append(metrics[rule.metric])
        max_loss = (spread.width - credit) * 100 + _spread_commission(execution, 1) * 2
        contracts = contracts_for_risk_budget(
            account_equity=realized_equity,
            max_loss_per_contract=max_loss,
            max_risk_fraction=strategy.max_risk_fraction,
        )
        if contracts <= 0:
            no_trade_reasons["risk budget cannot fund one contract"] += 1
            equity_rows.append((day, realized_equity, False))
            continue

        position = SpreadPosition(
            spread=spread,
            opened_on=day,
            entry_spot=spot,
            entry_credit=credit,
            contracts=contracts,
            entry_commission=_spread_commission(execution, contracts),
        )
        equity_rows.append(
            (
                day,
                _marked_spread_equity(realized_equity, position, immediate_debit, execution),
                True,
            )
        )

    if position is not None:
        raise RuntimeError(f"{rule.name}: period ended with an open position")

    equities = [row[1] for row in equity_rows]
    winners = [trade for trade in trades if trade.net_pnl > 0]
    losers = [trade for trade in trades if trade.net_pnl < 0]
    final_equity = equities[-1]
    pf = _profit_factor(trades)
    return {
        "rule": rule.name,
        "metric": rule.metric,
        "threshold": rule.threshold,
        "start": rows[0][0].isoformat(),
        "end": rows[-1][0].isoformat(),
        "trade_count": len(trades),
        "entry_attempt_days": entry_attempts,
        "premium_eligible_days": premium_eligible_days,
        "premium_rejected_days": premium_rejections,
        "accepted_metric_min": min(accepted_metric_values, default=None),
        "accepted_metric_max": max(accepted_metric_values, default=None),
        "total_return": final_equity / initial_equity - 1,
        "final_equity": final_equity,
        "max_drawdown": _max_drawdown(equities, initial_equity),
        "win_rate": len(winners) / len(trades) if trades else 0.0,
        "profit_factor": pf,
        "average_trade": sum(trade.net_pnl for trade in trades) / len(trades) if trades else 0.0,
        "average_winner": (
            sum(trade.net_pnl for trade in winners) / len(winners) if winners else 0.0
        ),
        "average_loser": (
            sum(trade.net_pnl for trade in losers) / len(losers) if losers else 0.0
        ),
        "best_trade": max((trade.net_pnl for trade in trades), default=0.0),
        "worst_trade": min((trade.net_pnl for trade in trades), default=0.0),
        "exit_reasons": dict(Counter(trade.reason for trade in trades)),
        "stale_mark_leg_count": len(stale_events),
        "stale_mark_day_count": len({event["mark_date"] for event in stale_events}),
        "top_no_trade_reasons": no_trade_reasons.most_common(5),
    }


def _discovery_pass(result: dict[str, object]) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    trades = int(result["trade_count"])
    pf_raw = result["profit_factor"]
    pf = 0.0 if pf_raw is None else float(pf_raw)
    total_return = float(result["total_return"])
    drawdown = float(result["max_drawdown"])
    if trades < MIN_DISCOVERY_TRADES:
        reasons.append(f"trades {trades} < {MIN_DISCOVERY_TRADES}")
    if pf <= MIN_DISCOVERY_PF:
        reasons.append(f"profit factor {pf:.2f} <= {MIN_DISCOVERY_PF:.2f}")
    if total_return <= 0:
        reasons.append(f"return {total_return:+.2%} <= 0")
    if drawdown >= MAX_DISCOVERY_DRAWDOWN:
        reasons.append(f"drawdown {drawdown:.2%} >= {MAX_DISCOVERY_DRAWDOWN:.0%}")
    return not reasons, reasons


def _validation_pass(result: dict[str, object]) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    trades = int(result["trade_count"])
    pf_raw = result["profit_factor"]
    pf = 0.0 if pf_raw is None else float(pf_raw)
    total_return = float(result["total_return"])
    drawdown = float(result["max_drawdown"])
    if trades < MIN_VALIDATION_TRADES:
        reasons.append(f"trades {trades} < {MIN_VALIDATION_TRADES}")
    if pf <= MIN_VALIDATION_PF:
        reasons.append(f"profit factor {pf:.2f} <= {MIN_VALIDATION_PF:.2f}")
    if total_return <= 0:
        reasons.append(f"return {total_return:+.2%} <= 0")
    if drawdown >= MAX_VALIDATION_DRAWDOWN:
        reasons.append(f"drawdown {drawdown:.2%} >= {MAX_VALIDATION_DRAWDOWN:.0%}")
    return not reasons, reasons


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    con = _prepare_database(args.data_dir, DISCOVERY_START, VALIDATION_END)
    discovery: list[dict[str, object]] = []
    for rule in RULES:
        result = _run_rule(con, rule, start=DISCOVERY_START, end=DISCOVERY_END)
        passed, reasons = _discovery_pass(result)
        result["discovery_pass"] = passed
        result["discovery_fail_reasons"] = reasons
        discovery.append(result)
        pf = result["profit_factor"]
        pf_text = "inf" if pf is None else f"{float(pf):.2f}"
        print(
            f"DISCOVERY {rule.name:>22} trades={result['trade_count']:>3} "
            f"ret={float(result['total_return']):>+7.2%} "
            f"dd={float(result['max_drawdown']):>6.2%} "
            f"win={float(result['win_rate']):>6.1%} pf={pf_text:>5} pass={passed}"
        )

    eligible = [item for item in discovery if bool(item["discovery_pass"])]
    selected_name: str | None = None
    validation: dict[str, object] | None = None
    qqq_ready = False
    if eligible:
        selected = max(
            eligible,
            key=lambda item: (
                float("inf") if item["profit_factor"] is None else float(item["profit_factor"]),
                int(item["trade_count"]),
            ),
        )
        selected_name = str(selected["rule"])
        selected_rule = next(rule for rule in RULES if rule.name == selected_name)
        validation = _run_rule(
            con,
            selected_rule,
            start=VALIDATION_START,
            end=VALIDATION_END,
        )
        passed, reasons = _validation_pass(validation)
        validation["validation_pass"] = passed
        validation["validation_fail_reasons"] = reasons
        qqq_ready = passed
        pf = validation["profit_factor"]
        pf_text = "inf" if pf is None else f"{float(pf):.2f}"
        print(
            f"VALIDATE  {selected_name:>22} trades={validation['trade_count']:>3} "
            f"ret={float(validation['total_return']):>+7.2%} "
            f"dd={float(validation['max_drawdown']):>6.2%} "
            f"win={float(validation['win_rate']):>6.1%} pf={pf_text:>5} pass={passed}"
        )

    output = {
        "method": {
            "frozen_strategy": (
                "45 DTE / 15-delta short put / exact 5-point wing / 50% TP / 2x stop / "
                "21 DTE exit / 2% account risk / existing slippage and commissions"
            ),
            "candidate_rules": [
                {"name": rule.name, "metric": rule.metric, "threshold": rule.threshold}
                for rule in RULES
            ],
            "premium_measurement": "execution-adjusted opening credit",
            "annualization": "simple credit/max-loss * 365 / actual entry DTE",
            "discovery_period": [DISCOVERY_START.isoformat(), DISCOVERY_END.isoformat()],
            "validation_period": [VALIDATION_START.isoformat(), VALIDATION_END.isoformat()],
            "selection": "highest discovery PF among candidates passing every fixed gate",
            "discovery_gates": {
                "minimum_trades": MIN_DISCOVERY_TRADES,
                "minimum_profit_factor_exclusive": MIN_DISCOVERY_PF,
                "minimum_total_return_exclusive": 0.0,
                "maximum_drawdown_exclusive": MAX_DISCOVERY_DRAWDOWN,
            },
            "validation_gates": {
                "minimum_trades": MIN_VALIDATION_TRADES,
                "minimum_profit_factor_exclusive": MIN_VALIDATION_PF,
                "minimum_total_return_exclusive": 0.0,
                "maximum_drawdown_exclusive": MAX_VALIDATION_DRAWDOWN,
            },
            "qqq_policy": "QQQ remains untouched unless SPY temporal validation passes",
        },
        "discovery": discovery,
        "selected_rule": selected_name,
        "validation": validation,
        "qqq_external_holdout_ready": qqq_ready,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, indent=2, sort_keys=True), encoding="utf-8")

    print(f"SELECTED_RULE={selected_name}")
    print(f"QQQ_READY={qqq_ready}")
    print(f"RESULT_PATH={args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
