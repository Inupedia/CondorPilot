"""Test whether a no-lookahead IV percentile filter improves the frozen SPY baseline.

The strategy itself remains fixed at 45 DTE / 15 delta / 5-point wings / 50% profit target /
2x credit stop / 21 DTE exit / 2% max account risk. Only the entry eligibility rule changes.

For each trading day, the volatility proxy is the median implied volatility of the call/put at
(1) the expiration closest to 45 DTE within the strategy window and (2) the strike closest to
spot. Its percentile is computed against the *prior* 252 observations only. The current day is
never included in its own percentile and at least 126 prior observations are required.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import duckdb

from condorpilot.execution import ExecutionConfig, ExecutionDataError, close_debit, entry_credit
from condorpilot.risk import ExitAction, contracts_for_risk_budget, evaluate_exit
from condorpilot.strategy import NoTradeError, build_iron_condor
from scripts.real_spy_baseline import (
    Position,
    Trade,
    _entry_chain,
    _marked_equity,
    _max_drawdown,
    _prepare_database,
    _profit_factor,
)
from scripts.real_spy_sensitivity import Variant, _held_close_mark

DATA_START = date(2015, 1, 2)
DISCOVERY_START = date(2016, 1, 4)
DISCOVERY_END = date(2019, 12, 31)
VALIDATION_START = date(2020, 1, 2)
VALIDATION_END = date(2025, 12, 12)
LOOKBACK = 252
MIN_HISTORY = 126
THRESHOLDS = (0.50, 0.70, 0.80)
MIN_DISCOVERY_TRADES = 20
MIN_DISCOVERY_PF = 1.10
MAX_DISCOVERY_DRAWDOWN = 0.10
MIN_VALIDATION_TRADES = 25
MIN_VALIDATION_PF = 1.0


@dataclass(frozen=True, slots=True)
class RegimeCandidate:
    name: str
    min_percentile: float


def _daily_atm_iv(con: duckdb.DuckDBPyConnection) -> list[tuple[date, float]]:
    rows = con.execute(
        """
        WITH candidates AS (
            SELECT
                o.quote_date,
                o.expiration,
                o.strike,
                o.option_type,
                o.implied_volatility,
                u.close,
                abs(date_diff('day', o.quote_date, o.expiration) - 45) AS dte_gap,
                abs(o.strike - u.close) AS strike_gap
            FROM options o
            JOIN underlying u USING (quote_date)
            WHERE date_diff('day', o.quote_date, o.expiration) BETWEEN 38 AND 52
              AND o.implied_volatility IS NOT NULL
              AND isfinite(o.implied_volatility)
              AND o.implied_volatility > 0.01
              AND o.implied_volatility < 5.0
        ),
        best_expiry AS (
            SELECT *,
                   dense_rank() OVER (
                       PARTITION BY quote_date
                       ORDER BY dte_gap, expiration
                   ) AS expiry_rank
            FROM candidates
        ),
        best_strike AS (
            SELECT *,
                   dense_rank() OVER (
                       PARTITION BY quote_date
                       ORDER BY strike_gap, strike
                   ) AS strike_rank
            FROM best_expiry
            WHERE expiry_rank = 1
        )
        SELECT quote_date, median(implied_volatility) AS atm_iv
        FROM best_strike
        WHERE strike_rank = 1
        GROUP BY quote_date
        ORDER BY quote_date
        """
    ).fetchall()
    return [(row[0], float(row[1])) for row in rows]


def _rolling_percentiles(
    observations: list[tuple[date, float]],
) -> dict[date, tuple[float, float]]:
    result: dict[date, tuple[float, float]] = {}
    values = [value for _, value in observations]
    for index, (day, current) in enumerate(observations):
        start = max(0, index - LOOKBACK)
        history = values[start:index]
        if len(history) < MIN_HISTORY:
            continue
        percentile = sum(value <= current for value in history) / len(history)
        result[day] = (current, percentile)
    return result


def _run_candidate(
    con: duckdb.DuckDBPyConnection,
    candidate: RegimeCandidate,
    percentiles: dict[date, tuple[float, float]],
    *,
    start: date,
    end: date,
    initial_equity: float = 50_000.0,
) -> dict[str, object]:
    variant = Variant(candidate.name)
    strategy = variant.strategy()
    execution = variant.execution()
    realized_equity = initial_equity
    position: Position | None = None
    trades: list[Trade] = []
    stale_events: list[dict[str, object]] = []
    no_trade_reasons: Counter[str] = Counter()
    equity_rows: list[tuple[date, float, bool]] = []
    regime_eligible_days = 0
    regime_skipped_days = 0
    missing_regime_days = 0
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
            marked, debit, stale = _held_close_mark(
                con,
                day=day,
                previous_day=previous_day,
                source=position.condor,
                execution=execution,
            )
            stale_events.extend(stale)
            dte = (position.condor.expiration - day).days
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

        regime = percentiles.get(day)
        if regime is None:
            missing_regime_days += 1
            equity_rows.append((day, realized_equity, False))
            continue
        _iv, percentile = regime
        if percentile < candidate.min_percentile:
            regime_skipped_days += 1
            equity_rows.append((day, realized_equity, False))
            continue
        regime_eligible_days += 1
        entry_attempts += 1

        chain = _entry_chain(con, day, strategy)
        try:
            condor = build_iron_condor(chain, spot=spot, as_of=day, config=strategy)
            credit = entry_credit(condor, execution)
            immediate_debit = close_debit(condor, execution)
        except (NoTradeError, ExecutionDataError) as exc:
            no_trade_reasons[str(exc)] += 1
            equity_rows.append((day, realized_equity, False))
            continue

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
        equity_rows.append(
            (day, _marked_equity(realized_equity, position, immediate_debit, execution), True)
        )

    if position is not None:
        raise RuntimeError(f"{candidate.name}: period ended with an open position")

    equities = [row[1] for row in equity_rows]
    final_equity = equities[-1]
    winners = [trade for trade in trades if trade.net_pnl > 0]
    losers = [trade for trade in trades if trade.net_pnl < 0]
    return {
        "candidate": candidate.name,
        "min_iv_percentile": candidate.min_percentile,
        "start": underlying_rows[0][0].isoformat(),
        "end": underlying_rows[-1][0].isoformat(),
        "trade_count": len(trades),
        "entry_attempt_days": entry_attempts,
        "regime_eligible_days": regime_eligible_days,
        "regime_skipped_days": regime_skipped_days,
        "missing_regime_days": missing_regime_days,
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
    if trades < MIN_VALIDATION_TRADES:
        reasons.append(f"trades {trades} < {MIN_VALIDATION_TRADES}")
    if pf <= MIN_VALIDATION_PF:
        reasons.append(f"profit factor {pf:.2f} <= {MIN_VALIDATION_PF:.2f}")
    if total_return <= 0:
        reasons.append(f"return {total_return:+.2%} <= 0")
    return not reasons, reasons


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    con = _prepare_database(args.data_dir, DATA_START, VALIDATION_END)
    observations = _daily_atm_iv(con)
    percentiles = _rolling_percentiles(observations)
    candidates = tuple(
        RegimeCandidate(f"iv_pct_{int(threshold * 100)}", threshold)
        for threshold in THRESHOLDS
    )

    discovery: list[dict[str, object]] = []
    for candidate in candidates:
        result = _run_candidate(
            con,
            candidate,
            percentiles,
            start=DISCOVERY_START,
            end=DISCOVERY_END,
        )
        passed, reasons = _discovery_pass(result)
        result["discovery_pass"] = passed
        result["discovery_fail_reasons"] = reasons
        discovery.append(result)
        pf = result["profit_factor"]
        pf_text = "inf" if pf is None else f"{float(pf):.2f}"
        print(
            f"REGIME {candidate.name:>9} trades={result['trade_count']:>3} "
            f"ret={float(result['total_return']):>+7.2%} "
            f"dd={float(result['max_drawdown']):>6.2%} "
            f"win={float(result['win_rate']):>6.1%} pf={pf_text:>5} pass={passed}"
        )

    eligible = [item for item in discovery if item["discovery_pass"]]
    selected_name: str | None = None
    validation: dict[str, object] | None = None
    if eligible:
        selected = max(
            eligible,
            key=lambda item: (float(item["profit_factor"]), float(item["total_return"])),
        )
        selected_name = str(selected["candidate"])
        candidate = next(item for item in candidates if item.name == selected_name)
        validation = _run_candidate(
            con,
            candidate,
            percentiles,
            start=VALIDATION_START,
            end=VALIDATION_END,
        )
        passed, reasons = _validation_pass(validation)
        validation["validation_pass"] = passed
        validation["validation_fail_reasons"] = reasons

    output = {
        "method": {
            "strategy": "frozen baseline; only IV percentile entry filter changes",
            "iv_proxy": (
                "median call/put IV at nearest-to-45-DTE expiration and nearest-to-spot strike"
            ),
            "lookback_trading_observations": LOOKBACK,
            "minimum_prior_observations": MIN_HISTORY,
            "no_lookahead": True,
            "candidate_thresholds": list(THRESHOLDS),
            "discovery_period": [DISCOVERY_START.isoformat(), DISCOVERY_END.isoformat()],
            "validation_period": [VALIDATION_START.isoformat(), VALIDATION_END.isoformat()],
            "discovery_gates": {
                "minimum_trades": MIN_DISCOVERY_TRADES,
                "minimum_profit_factor_exclusive": MIN_DISCOVERY_PF,
                "minimum_total_return_exclusive": 0.0,
                "maximum_drawdown_exclusive": MAX_DISCOVERY_DRAWDOWN,
            },
            "selection": "highest discovery profit factor among candidates passing all gates",
            "validation_gates": {
                "minimum_trades": MIN_VALIDATION_TRADES,
                "minimum_profit_factor_exclusive": MIN_VALIDATION_PF,
                "minimum_total_return_exclusive": 0.0,
            },
            "external_holdout_rule": (
                "QQQ is evaluated only if the selected SPY regime also passes temporal validation"
            ),
        },
        "iv_observation_count": len(observations),
        "percentile_observation_count": len(percentiles),
        "discovery": discovery,
        "selected_candidate": selected_name,
        "validation": validation,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, indent=2, sort_keys=True), encoding="utf-8")

    print(f"SELECTED_REGIME={selected_name}")
    if validation is not None:
        pf = validation["profit_factor"]
        pf_text = "inf" if pf is None else f"{float(pf):.2f}"
        print(
            f"TEMPORAL_VALIDATION trades={validation['trade_count']} "
            f"ret={float(validation['total_return']):+.2%} "
            f"dd={float(validation['max_drawdown']):.2%} "
            f"win={float(validation['win_rate']):.1%} pf={pf_text} "
            f"pass={validation['validation_pass']}"
        )
    print(f"RESULT_PATH={args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
