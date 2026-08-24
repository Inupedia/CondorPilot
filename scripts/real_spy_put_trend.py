"""Test one predeclared Bull Put Spread trend-regime hypothesis on real SPY history.

The option structure remains fixed at 45 DTE / 15-delta short put / exact 5-point wing /
50% take profit / 2x-credit stop / 21 DTE exit / 2% account risk with existing execution costs.
There is no minimum premium threshold beyond a positive executable credit; the only new entry
condition is that the *previous trading day's* SPY close is above its trailing 200-session SMA.

Using the previous day's trend state avoids same-close signal/execution ambiguity. The study has
one hypothesis and no parameter selection: 2011-2015 discovery, then 2016-2019 temporal stage 1,
then 2020-2025 temporal stage 2. A later stage is never evaluated unless the prior stage passes.
QQQ remains untouched unless every SPY stage passes.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import date
from pathlib import Path

import duckdb

from condorpilot.execution import ExecutionDataError
from condorpilot.risk import ExitAction, contracts_for_risk_budget, evaluate_exit
from condorpilot.strategy import NoTradeError
from scripts.real_spy_baseline import _entry_chain, _max_drawdown, _prepare_database
from scripts.real_spy_put_premium import _strategy_without_premium_gate
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

PRELOAD_START = date(2010, 1, 4)
DISCOVERY_START = date(2011, 1, 3)
DISCOVERY_END = date(2015, 12, 31)
STAGE1_START = date(2016, 1, 4)
STAGE1_END = date(2019, 12, 31)
STAGE2_START = date(2020, 1, 2)
STAGE2_END = date(2025, 12, 12)
SMA_SESSIONS = 200

MIN_DISCOVERY_TRADES = 40
MIN_DISCOVERY_PF = 1.10
MAX_DISCOVERY_DRAWDOWN = 0.10
MIN_VALIDATION_TRADES = 40
MIN_VALIDATION_PF = 1.00
MAX_VALIDATION_DRAWDOWN = 0.10


def _prior_day_trend_flags(
    con: duckdb.DuckDBPyConnection,
) -> dict[date, bool | None]:
    rows = con.execute(
        """
        SELECT
            quote_date,
            close,
            avg(close) OVER (
                ORDER BY quote_date
                ROWS BETWEEN 199 PRECEDING AND CURRENT ROW
            ) AS sma_200,
            count(*) OVER (
                ORDER BY quote_date
                ROWS BETWEEN 199 PRECEDING AND CURRENT ROW
            ) AS sample_count
        FROM underlying
        ORDER BY quote_date
        """
    ).fetchall()

    flags: dict[date, bool | None] = {}
    previous: tuple[float, float, int] | None = None
    for day, close_raw, sma_raw, count_raw in rows:
        if previous is None or previous[2] < SMA_SESSIONS:
            flags[day] = None
        else:
            prior_close, prior_sma, _prior_count = previous
            flags[day] = prior_close > prior_sma
        previous = (float(close_raw), float(sma_raw), int(count_raw))
    return flags


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
    trades: list[SpreadTrade] = []
    stale_events: list[dict[str, object]] = []
    no_trade_reasons: Counter[str] = Counter()
    equity_rows: list[tuple[date, float, bool]] = []
    entry_attempts = 0
    trend_eligible_days = 0
    trend_skipped_days = 0
    trend_unavailable_days = 0
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

        trend = trend_flags.get(day)
        if trend is None:
            trend_unavailable_days += 1
            equity_rows.append((day, realized_equity, False))
            continue
        if not trend:
            trend_skipped_days += 1
            equity_rows.append((day, realized_equity, False))
            continue
        trend_eligible_days += 1
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
        raise RuntimeError(f"trend study period {start} through {end} ended with an open position")

    equities = [row[1] for row in equity_rows]
    winners = [trade for trade in trades if trade.net_pnl > 0]
    losers = [trade for trade in trades if trade.net_pnl < 0]
    final_equity = equities[-1]
    return {
        "start": rows[0][0].isoformat(),
        "end": rows[-1][0].isoformat(),
        "trade_count": len(trades),
        "entry_attempt_days": entry_attempts,
        "trend_eligible_flat_days": trend_eligible_days,
        "trend_skipped_flat_days": trend_skipped_days,
        "trend_unavailable_flat_days": trend_unavailable_days,
        "total_return": final_equity / initial_equity - 1,
        "final_equity": final_equity,
        "max_drawdown": _max_drawdown(equities, initial_equity),
        "win_rate": len(winners) / len(trades) if trades else 0.0,
        "profit_factor": _profit_factor(trades),
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


def _passes(
    result: dict[str, object],
    *,
    min_trades: int,
    min_pf: float,
    max_drawdown: float,
) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    trades = int(result["trade_count"])
    pf_raw = result["profit_factor"]
    pf = 0.0 if pf_raw is None else float(pf_raw)
    total_return = float(result["total_return"])
    drawdown = float(result["max_drawdown"])
    if trades < min_trades:
        reasons.append(f"trades {trades} < {min_trades}")
    if pf <= min_pf:
        reasons.append(f"profit factor {pf:.2f} <= {min_pf:.2f}")
    if total_return <= 0:
        reasons.append(f"return {total_return:+.2%} <= 0")
    if drawdown >= max_drawdown:
        reasons.append(f"drawdown {drawdown:.2%} >= {max_drawdown:.0%}")
    return not reasons, reasons


def _print_stage(label: str, result: dict[str, object], passed: bool) -> None:
    pf = result["profit_factor"]
    pf_text = "inf" if pf is None else f"{float(pf):.2f}"
    print(
        f"{label:<10} trades={result['trade_count']:>3} "
        f"ret={float(result['total_return']):>+7.2%} "
        f"dd={float(result['max_drawdown']):>6.2%} "
        f"win={float(result['win_rate']):>6.1%} pf={pf_text:>5} pass={passed}"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    con = _prepare_database(args.data_dir, PRELOAD_START, STAGE2_END)
    trend_flags = _prior_day_trend_flags(con)

    discovery = _run_period(
        con,
        trend_flags,
        start=DISCOVERY_START,
        end=DISCOVERY_END,
    )
    discovery_pass, discovery_reasons = _passes(
        discovery,
        min_trades=MIN_DISCOVERY_TRADES,
        min_pf=MIN_DISCOVERY_PF,
        max_drawdown=MAX_DISCOVERY_DRAWDOWN,
    )
    discovery["pass"] = discovery_pass
    discovery["fail_reasons"] = discovery_reasons
    _print_stage("DISCOVERY", discovery, discovery_pass)

    stage1: dict[str, object] | None = None
    stage2: dict[str, object] | None = None
    qqq_ready = False
    if discovery_pass:
        stage1 = _run_period(con, trend_flags, start=STAGE1_START, end=STAGE1_END)
        stage1_pass, stage1_reasons = _passes(
            stage1,
            min_trades=MIN_VALIDATION_TRADES,
            min_pf=MIN_VALIDATION_PF,
            max_drawdown=MAX_VALIDATION_DRAWDOWN,
        )
        stage1["pass"] = stage1_pass
        stage1["fail_reasons"] = stage1_reasons
        _print_stage("STAGE1", stage1, stage1_pass)

        if stage1_pass:
            stage2 = _run_period(con, trend_flags, start=STAGE2_START, end=STAGE2_END)
            stage2_pass, stage2_reasons = _passes(
                stage2,
                min_trades=MIN_VALIDATION_TRADES,
                min_pf=MIN_VALIDATION_PF,
                max_drawdown=MAX_VALIDATION_DRAWDOWN,
            )
            stage2["pass"] = stage2_pass
            stage2["fail_reasons"] = stage2_reasons
            _print_stage("STAGE2", stage2, stage2_pass)
            qqq_ready = stage2_pass

    output = {
        "method": {
            "hypothesis": "sell Bull Put Spreads only when prior-day SPY close > prior-day SMA200",
            "signal_lag": "trend uses previous trading day only",
            "sma_sessions": SMA_SESSIONS,
            "premium_gate": "none beyond positive executable credit and defined risk",
            "frozen_strategy": (
                "45 DTE / 15-delta short put / exact 5-point wing / 50% TP / 2x stop / "
                "21 DTE exit / 2% account risk / existing slippage and commissions"
            ),
            "discovery_period": [DISCOVERY_START.isoformat(), DISCOVERY_END.isoformat()],
            "stage1_period": [STAGE1_START.isoformat(), STAGE1_END.isoformat()],
            "stage2_period": [STAGE2_START.isoformat(), STAGE2_END.isoformat()],
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
            "qqq_policy": "QQQ remains untouched unless all three SPY stages pass",
        },
        "discovery_2011_2015": discovery,
        "temporal_stage1_2016_2019": stage1,
        "temporal_stage2_2020_2025": stage2,
        "qqq_external_holdout_ready": qqq_ready,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, indent=2, sort_keys=True), encoding="utf-8")

    print(f"QQQ_READY={qqq_ready}")
    print(f"RESULT_PATH={args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
