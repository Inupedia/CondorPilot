"""Test one post-diagnostic Bull Put position-management hypothesis on real SPY history.

Entry remains the previously studied prior-day-close-above-SMA200 rule. The only change is a
symmetric management rule: while a position is open, if the previous trading day's SPY close is
at or below its trailing SMA200, close the spread at today's EOD option snapshot.

No new lookback or percentage threshold is introduced. Existing take-profit, stop-loss, time
exit, option selection, risk sizing, slippage, and commission assumptions remain unchanged.

This hypothesis was formulated after observing all SPY periods, so the SPY stages are explicitly
model-development robustness checks, not untouched out-of-sample evidence. QQQ remains untouched
and may be opened only if the same frozen rule is positive in every SPY stage.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import duckdb

from condorpilot.execution import ExecutionDataError
from condorpilot.risk import ExitAction, contracts_for_risk_budget, evaluate_exit
from condorpilot.strategy import NoTradeError
from scripts.real_spy_baseline import _entry_chain, _max_drawdown, _prepare_database
from scripts.real_spy_put_premium import _strategy_without_premium_gate
from scripts.real_spy_put_trend import (
    DISCOVERY_END,
    DISCOVERY_START,
    PRELOAD_START,
    STAGE1_END,
    STAGE1_START,
    STAGE2_END,
    STAGE2_START,
    _prior_day_trend_flags,
)
from scripts.real_spy_side_attribution import (
    SpreadPosition,
    _build_spread,
    _execution,
    _held_spread_close,
    _marked_spread_equity,
    _spread_close_value,
    _spread_commission,
    _spread_entry_credit,
)

MIN_TRADES = 40
MAX_DRAWDOWN = 0.10
DISCOVERY_MIN_PF = 1.10
VALIDATION_MIN_PF = 1.00

BASELINE_REFERENCE = {
    "2011_2015": {"trade_count": 96, "net_pnl": 567.55, "profit_factor": 1.2573631108},
    "2016_2019": {"trade_count": 87, "net_pnl": 390.85, "profit_factor": 1.1822951890},
    "2020_2025": {"trade_count": 124, "net_pnl": -192.05, "profit_factor": 0.9545255431},
}


@dataclass(frozen=True, slots=True)
class ManagedTrade:
    opened_on: date
    closed_on: date
    reason: str
    contracts: int
    entry_credit: float
    exit_debit: float
    entry_commission: float
    exit_commission: float

    @property
    def net_pnl(self) -> float:
        gross = (self.entry_credit - self.exit_debit) * 100 * self.contracts
        return gross - self.entry_commission - self.exit_commission


def _profit_factor(trades: list[ManagedTrade]) -> float | None:
    profits = sum(trade.net_pnl for trade in trades if trade.net_pnl > 0)
    losses = -sum(trade.net_pnl for trade in trades if trade.net_pnl < 0)
    if losses > 0:
        return profits / losses
    if profits > 0:
        return None
    return 0.0


def _run_period(
    con: duckdb.DuckDBPyConnection,
    trend_flags: dict[date, bool | None],
    *,
    start: date,
    end: date,
    initial_equity: float = 50_000.0,
) -> dict[str, object]:
    strategy = _strategy_without_premium_gate()
    execution = _execution()
    realized_equity = initial_equity
    position: SpreadPosition | None = None
    trades: list[ManagedTrade] = []
    stale_events: list[dict[str, object]] = []
    no_trade_reasons: Counter[str] = Counter()
    equity_rows: list[tuple[date, float, bool]] = []
    trend_break_signals = 0
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

            trend_state = trend_flags.get(day)
            if trend_state is False:
                reason = "trend_break_exit"
                should_exit = True
                trend_break_signals += 1
            else:
                action = evaluate_exit(
                    entry_credit=position.entry_credit,
                    current_close_debit=debit,
                    dte=dte,
                    config=strategy,
                )
                should_exit = action is not ExitAction.HOLD
                reason = action.value

            if not should_exit:
                equity_rows.append(
                    (day, _marked_spread_equity(realized_equity, position, debit, execution), True)
                )
                continue

            exit_commission = _spread_commission(execution, position.contracts)
            trade = ManagedTrade(
                opened_on=position.opened_on,
                closed_on=day,
                reason=reason,
                contracts=position.contracts,
                entry_credit=position.entry_credit,
                exit_debit=debit,
                entry_commission=position.entry_commission,
                exit_commission=exit_commission,
            )
            trades.append(trade)
            realized_equity += trade.net_pnl
            position = None
            equity_rows.append((day, realized_equity, False))
            continue

        if (final_day - day).days < entry_embargo_days:
            equity_rows.append((day, realized_equity, False))
            continue

        if trend_flags.get(day) is not True:
            equity_rows.append((day, realized_equity, False))
            continue

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
        raise RuntimeError(f"managed period {start} through {end} ended with an open position")

    equities = [row[1] for row in equity_rows]
    winners = [trade for trade in trades if trade.net_pnl > 0]
    losers = [trade for trade in trades if trade.net_pnl < 0]
    final_equity = equities[-1]
    return {
        "start": rows[0][0].isoformat(),
        "end": rows[-1][0].isoformat(),
        "trade_count": len(trades),
        "total_return": final_equity / initial_equity - 1,
        "final_equity": final_equity,
        "max_drawdown": _max_drawdown(equities, initial_equity),
        "win_rate": len(winners) / len(trades) if trades else 0.0,
        "profit_factor": _profit_factor(trades),
        "average_trade": sum(item.net_pnl for item in trades) / len(trades) if trades else 0.0,
        "average_winner": (
            sum(item.net_pnl for item in winners) / len(winners) if winners else 0.0
        ),
        "average_loser": (
            sum(item.net_pnl for item in losers) / len(losers) if losers else 0.0
        ),
        "exit_reasons": dict(Counter(item.reason for item in trades)),
        "trend_break_exit_count": sum(item.reason == "trend_break_exit" for item in trades),
        "trend_break_signal_count": trend_break_signals,
        "stale_mark_leg_count": len(stale_events),
        "stale_mark_day_count": len({event["mark_date"] for event in stale_events}),
        "top_no_trade_reasons": no_trade_reasons.most_common(5),
    }


def _passes(
    result: dict[str, object],
    *,
    minimum_pf: float,
) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    trades = int(result["trade_count"])
    pf_raw = result["profit_factor"]
    pf = 0.0 if pf_raw is None else float(pf_raw)
    total_return = float(result["total_return"])
    drawdown = float(result["max_drawdown"])
    if trades < MIN_TRADES:
        reasons.append(f"trades {trades} < {MIN_TRADES}")
    if pf <= minimum_pf:
        reasons.append(f"profit factor {pf:.2f} <= {minimum_pf:.2f}")
    if total_return <= 0:
        reasons.append(f"return {total_return:+.2%} <= 0")
    if drawdown >= MAX_DRAWDOWN:
        reasons.append(f"drawdown {drawdown:.2%} >= {MAX_DRAWDOWN:.0%}")
    return not reasons, reasons


def _print_stage(label: str, result: dict[str, object], passed: bool) -> None:
    pf = result["profit_factor"]
    pf_text = "inf" if pf is None else f"{float(pf):.2f}"
    print(
        f"{label:<10} trades={result['trade_count']:>3} "
        f"ret={float(result['total_return']):>+7.2%} "
        f"dd={float(result['max_drawdown']):>6.2%} "
        f"win={float(result['win_rate']):>6.1%} pf={pf_text:>5} "
        f"trend_exits={result['trend_break_exit_count']:>2} pass={passed}"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    con = _prepare_database(args.data_dir, PRELOAD_START, STAGE2_END)
    trend_flags = _prior_day_trend_flags(con)
    definitions = (
        ("2011_2015", DISCOVERY_START, DISCOVERY_END, DISCOVERY_MIN_PF),
        ("2016_2019", STAGE1_START, STAGE1_END, VALIDATION_MIN_PF),
        ("2020_2025", STAGE2_START, STAGE2_END, VALIDATION_MIN_PF),
    )

    stages: dict[str, dict[str, object]] = {}
    all_pass = True
    for label, start, end, minimum_pf in definitions:
        result = _run_period(con, trend_flags, start=start, end=end)
        passed, reasons = _passes(result, minimum_pf=minimum_pf)
        result["pass"] = passed
        result["fail_reasons"] = reasons
        result["baseline_reference"] = BASELINE_REFERENCE[label]
        result["net_pnl_change_vs_baseline"] = (
            float(result["final_equity"]) - 50_000.0 - BASELINE_REFERENCE[label]["net_pnl"]
        )
        stages[label] = result
        all_pass = all_pass and passed
        _print_stage(label, result, passed)

    output = {
        "method": {
            "status": "SPY model-development robustness check; not untouched OOS evidence",
            "entry_rule": "previous trading day close above trailing SMA200",
            "new_management_rule": (
                "if previous trading day close is at or below trailing SMA200 while held, "
                "close at today's EOD option snapshot"
            ),
            "new_tunable_parameters": 0,
            "other_rules": (
                "45 DTE / 15-delta short put / exact 5-point wing / 50% TP / 2x stop / "
                "21 DTE exit / 2% account risk / existing slippage and commissions"
            ),
            "qqq_policy": (
                "QQQ remains untouched unless the frozen management rule passes every SPY stage"
            ),
        },
        "stages": stages,
        "qqq_external_holdout_ready": all_pass,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, indent=2, sort_keys=True), encoding="utf-8")
    print(f"QQQ_READY={all_pass}")
    print(f"RESULT_PATH={args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
