"""Diagnose why the fixed SMA200 Bull Put strategy deteriorates in 2020-2025.

This script does not change entry or exit rules. It exactly replays the trades from the
predeclared trend-regime study and attaches explanatory features to each completed trade:
entry credit, return on risk, short-put IV, prior 20-session realized volatility, prior-day
distance above SMA200, next-session close return, maximum adverse close move while held, and
worst one-session close move while held.

The goal is attribution, not strategy selection. No diagnostic bucket becomes a new trading
rule in this study, and QQQ remains untouched.
"""

from __future__ import annotations

import argparse
import json
import math
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from statistics import median

import duckdb

from condorpilot.execution import ExecutionDataError
from condorpilot.risk import ExitAction, contracts_for_risk_budget, evaluate_exit
from condorpilot.strategy import NoTradeError
from scripts.real_spy_baseline import _entry_chain, _prepare_database
from scripts.real_spy_put_premium import _strategy_without_premium_gate
from scripts.real_spy_put_trend import (
    DISCOVERY_END,
    DISCOVERY_START,
    PRELOAD_START,
    SMA_SESSIONS,
    STAGE1_END,
    STAGE1_START,
    STAGE2_END,
    STAGE2_START,
)
from scripts.real_spy_side_attribution import (
    SpreadPosition,
    _build_spread,
    _execution,
    _held_spread_close,
    _spread_close_value,
    _spread_commission,
    _spread_entry_credit,
)

EXPECTED_COUNTS = {
    "2011_2015": 96,
    "2016_2019": 87,
    "2020_2025": 124,
}


@dataclass(frozen=True, slots=True)
class EntryContext:
    short_iv: float | None
    rv20: float | None
    trend_distance: float
    next_close_return: float | None
    credit_to_width: float
    credit_to_max_loss: float
    entry_dte: int


@dataclass(frozen=True, slots=True)
class DiagnosticTrade:
    stage: str
    opened_on: str
    closed_on: str
    reason: str
    contracts: int
    holding_days: int
    entry_spot: float
    exit_spot: float
    entry_credit: float
    exit_debit: float
    net_pnl: float
    risk_dollars: float
    pnl_to_risk: float
    short_iv: float | None
    rv20: float | None
    iv_minus_rv20: float | None
    trend_distance: float
    next_close_return: float | None
    max_adverse_close_return: float
    worst_one_day_close_return: float
    final_underlying_return: float
    credit_to_width: float
    credit_to_max_loss: float
    entry_dte: int


def _underlying_series(
    con: duckdb.DuckDBPyConnection,
) -> tuple[list[date], dict[date, float]]:
    rows = con.execute(
        "SELECT quote_date, close FROM underlying ORDER BY quote_date"
    ).fetchall()
    dates = [row[0] for row in rows]
    closes = {row[0]: float(row[1]) for row in rows}
    return dates, closes


def _entry_contexts(
    dates: list[date],
    closes: dict[date, float],
) -> dict[date, tuple[float, float | None, float | None]]:
    """Return prior-day SMA distance, prior-20-session RV, and next-close return."""
    contexts: dict[date, tuple[float, float | None, float | None]] = {}
    values = [closes[day] for day in dates]
    log_returns: list[float | None] = [None]
    for index in range(1, len(values)):
        log_returns.append(math.log(values[index] / values[index - 1]))

    for index, day in enumerate(dates):
        if index == 0:
            continue
        prior_index = index - 1
        if prior_index + 1 < SMA_SESSIONS:
            continue
        sma_start = prior_index - SMA_SESSIONS + 1
        prior_sma = sum(values[sma_start : prior_index + 1]) / SMA_SESSIONS
        trend_distance = values[prior_index] / prior_sma - 1.0

        rv20: float | None = None
        if prior_index >= 20:
            window = [
                item
                for item in log_returns[prior_index - 19 : prior_index + 1]
                if item is not None
            ]
            if len(window) == 20:
                mean = sum(window) / len(window)
                variance = sum((item - mean) ** 2 for item in window) / (len(window) - 1)
                rv20 = math.sqrt(variance * 252.0)

        next_close_return = None
        if index + 1 < len(values):
            next_close_return = values[index + 1] / values[index] - 1.0
        contexts[day] = (trend_distance, rv20, next_close_return)
    return contexts


def _holding_path_metrics(
    dates: list[date],
    closes: dict[date, float],
    *,
    opened_on: date,
    closed_on: date,
    entry_spot: float,
) -> tuple[float, float]:
    path_dates = [day for day in dates if opened_on <= day <= closed_on]
    if not path_dates:
        raise RuntimeError(f"no underlying path for {opened_on} through {closed_on}")
    path = [closes[day] for day in path_dates]
    max_adverse = min(price / entry_spot - 1.0 for price in path)
    one_day = [path[index] / path[index - 1] - 1.0 for index in range(1, len(path))]
    worst_one_day = min(one_day, default=0.0)
    return max_adverse, worst_one_day


def _run_stage(
    con: duckdb.DuckDBPyConnection,
    dates: list[date],
    closes: dict[date, float],
    contexts: dict[date, tuple[float, float | None, float | None]],
    *,
    stage: str,
    start: date,
    end: date,
) -> list[DiagnosticTrade]:
    strategy = _strategy_without_premium_gate()
    execution = _execution()
    realized_equity = 50_000.0
    position: SpreadPosition | None = None
    entry_context: EntryContext | None = None
    trades: list[DiagnosticTrade] = []
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
            _marked, debit, _stale = _held_spread_close(
                con,
                day=day,
                previous_day=previous_day,
                source=position.spread,
                execution=execution,
            )
            dte = (position.spread.expiration - day).days
            action = evaluate_exit(
                entry_credit=position.entry_credit,
                current_close_debit=debit,
                dte=dte,
                config=strategy,
            )
            if action is ExitAction.HOLD:
                continue
            if entry_context is None:
                raise RuntimeError("open position is missing diagnostic entry context")

            exit_commission = _spread_commission(execution, position.contracts)
            gross = (position.entry_credit - debit) * 100 * position.contracts
            net_pnl = gross - position.entry_commission - exit_commission
            max_loss_one = (
                (position.spread.width - position.entry_credit) * 100
                + _spread_commission(execution, 1) * 2
            )
            risk_dollars = max_loss_one * position.contracts
            max_adverse, worst_one_day = _holding_path_metrics(
                dates,
                closes,
                opened_on=position.opened_on,
                closed_on=day,
                entry_spot=position.entry_spot,
            )
            iv_minus_rv = None
            if entry_context.short_iv is not None and entry_context.rv20 is not None:
                iv_minus_rv = entry_context.short_iv - entry_context.rv20

            trades.append(
                DiagnosticTrade(
                    stage=stage,
                    opened_on=position.opened_on.isoformat(),
                    closed_on=day.isoformat(),
                    reason=action.value,
                    contracts=position.contracts,
                    holding_days=(day - position.opened_on).days,
                    entry_spot=position.entry_spot,
                    exit_spot=spot,
                    entry_credit=position.entry_credit,
                    exit_debit=debit,
                    net_pnl=net_pnl,
                    risk_dollars=risk_dollars,
                    pnl_to_risk=net_pnl / risk_dollars,
                    short_iv=entry_context.short_iv,
                    rv20=entry_context.rv20,
                    iv_minus_rv20=iv_minus_rv,
                    trend_distance=entry_context.trend_distance,
                    next_close_return=entry_context.next_close_return,
                    max_adverse_close_return=max_adverse,
                    worst_one_day_close_return=worst_one_day,
                    final_underlying_return=spot / position.entry_spot - 1.0,
                    credit_to_width=entry_context.credit_to_width,
                    credit_to_max_loss=entry_context.credit_to_max_loss,
                    entry_dte=entry_context.entry_dte,
                )
            )
            realized_equity += net_pnl
            position = None
            entry_context = None
            continue

        if (final_day - day).days < entry_embargo_days:
            continue
        raw_context = contexts.get(day)
        if raw_context is None:
            continue
        trend_distance, rv20, next_close_return = raw_context
        if trend_distance <= 0:
            continue

        chain = _entry_chain(con, day, strategy)
        try:
            spread = _build_spread(chain, spot=spot, as_of=day, side="put", config=strategy)
            credit = _spread_entry_credit(spread, execution)
            _spread_close_value(spread, execution, require_non_negative=True)
        except (NoTradeError, ExecutionDataError):
            continue
        if credit <= 0 or spread.width <= credit:
            continue

        max_loss_one = (
            (spread.width - credit) * 100 + _spread_commission(execution, 1) * 2
        )
        contracts = contracts_for_risk_budget(
            account_equity=realized_equity,
            max_loss_per_contract=max_loss_one,
            max_risk_fraction=strategy.max_risk_fraction,
        )
        if contracts <= 0:
            continue

        short_iv = spread.short.implied_volatility
        position = SpreadPosition(
            spread=spread,
            opened_on=day,
            entry_spot=spot,
            entry_credit=credit,
            contracts=contracts,
            entry_commission=_spread_commission(execution, contracts),
        )
        entry_context = EntryContext(
            short_iv=short_iv,
            rv20=rv20,
            trend_distance=trend_distance,
            next_close_return=next_close_return,
            credit_to_width=credit / spread.width,
            credit_to_max_loss=credit / (spread.width - credit),
            entry_dte=(spread.expiration - day).days,
        )

    if position is not None:
        raise RuntimeError(f"diagnostic stage {stage} ended with an open position")
    expected = EXPECTED_COUNTS[stage]
    if len(trades) != expected:
        raise RuntimeError(
            f"diagnostic replay changed {stage}: trades={len(trades)}, expected={expected}"
        )
    return trades


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _feature_summary(
    trades: list[DiagnosticTrade],
    attribute: str,
) -> dict[str, float | None]:
    values = [getattr(trade, attribute) for trade in trades]
    cleaned = [float(value) for value in values if value is not None]
    if not cleaned:
        return {"mean": None, "median": None, "p10": None, "p90": None}
    ordered = sorted(cleaned)

    def percentile(fraction: float) -> float:
        index = round((len(ordered) - 1) * fraction)
        return ordered[index]

    return {
        "mean": _mean(cleaned),
        "median": median(cleaned),
        "p10": percentile(0.10),
        "p90": percentile(0.90),
    }


def _aggregate(trades: list[DiagnosticTrade]) -> dict[str, object]:
    winners = [trade for trade in trades if trade.net_pnl > 0]
    losers = [trade for trade in trades if trade.net_pnl < 0]
    stops = [trade for trade in trades if trade.reason == ExitAction.STOP_LOSS.value]
    gross_profit = sum(trade.net_pnl for trade in winners)
    gross_loss = -sum(trade.net_pnl for trade in losers)
    pf = gross_profit / gross_loss if gross_loss > 0 else None

    worst = sorted(trades, key=lambda trade: trade.net_pnl)
    total_loss = -sum(min(trade.net_pnl, 0.0) for trade in trades)

    def worst_loss_share(count: int) -> float | None:
        if total_loss <= 0:
            return None
        selected_loss = -sum(min(trade.net_pnl, 0.0) for trade in worst[:count])
        return selected_loss / total_loss

    features = (
        "entry_credit",
        "credit_to_width",
        "credit_to_max_loss",
        "short_iv",
        "rv20",
        "iv_minus_rv20",
        "trend_distance",
        "next_close_return",
        "max_adverse_close_return",
        "worst_one_day_close_return",
        "final_underlying_return",
        "holding_days",
        "pnl_to_risk",
    )
    return {
        "trade_count": len(trades),
        "net_pnl": sum(trade.net_pnl for trade in trades),
        "average_trade": _mean([trade.net_pnl for trade in trades]),
        "win_rate": len(winners) / len(trades) if trades else 0.0,
        "profit_factor": pf,
        "average_winner": _mean([trade.net_pnl for trade in winners]),
        "average_loser": _mean([trade.net_pnl for trade in losers]),
        "stop_count": len(stops),
        "stop_rate": len(stops) / len(trades) if trades else 0.0,
        "stop_net_pnl": sum(trade.net_pnl for trade in stops),
        "exit_reasons": dict(Counter(trade.reason for trade in trades)),
        "worst_5_loss_share": worst_loss_share(5),
        "worst_10_loss_share": worst_loss_share(10),
        "features": {name: _feature_summary(trades, name) for name in features},
    }


def _yearly(trades: list[DiagnosticTrade]) -> dict[str, object]:
    grouped: dict[int, list[DiagnosticTrade]] = defaultdict(list)
    for trade in trades:
        grouped[date.fromisoformat(trade.opened_on).year].append(trade)
    return {str(year): _aggregate(items) for year, items in sorted(grouped.items())}


def _loss_vs_winner_features(trades: list[DiagnosticTrade]) -> dict[str, object]:
    winners = [trade for trade in trades if trade.net_pnl > 0]
    losers = [trade for trade in trades if trade.net_pnl < 0]
    names = (
        "credit_to_max_loss",
        "short_iv",
        "rv20",
        "iv_minus_rv20",
        "trend_distance",
        "next_close_return",
        "max_adverse_close_return",
        "worst_one_day_close_return",
    )
    return {
        name: {
            "winners": _feature_summary(winners, name),
            "losers": _feature_summary(losers, name),
        }
        for name in names
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    con = _prepare_database(args.data_dir, PRELOAD_START, STAGE2_END)
    dates, closes = _underlying_series(con)
    contexts = _entry_contexts(dates, closes)

    stages = {
        "2011_2015": (DISCOVERY_START, DISCOVERY_END),
        "2016_2019": (STAGE1_START, STAGE1_END),
        "2020_2025": (STAGE2_START, STAGE2_END),
    }
    trades_by_stage = {
        stage: _run_stage(
            con,
            dates,
            closes,
            contexts,
            stage=stage,
            start=start,
            end=end,
        )
        for stage, (start, end) in stages.items()
    }

    output = {
        "method": {
            "strategy": "exact replay of the frozen prior-day-close-above-SMA200 Bull Put rule",
            "no_strategy_changes": True,
            "rv20": "annualized sample standard deviation of prior 20 close-to-close log returns",
            "max_adverse_move": "minimum SPY close / entry close - 1 during the holding period",
            "worst_daily_move": "minimum one-session close-to-close return while held",
            "purpose": "diagnose temporal deterioration; no feature bucket is an entry rule",
            "qqq_touched": False,
        },
        "stage_summaries": {
            stage: _aggregate(trades) for stage, trades in trades_by_stage.items()
        },
        "stage_yearly": {
            stage: _yearly(trades) for stage, trades in trades_by_stage.items()
        },
        "winner_loser_feature_comparison": {
            stage: _loss_vs_winner_features(trades)
            for stage, trades in trades_by_stage.items()
        },
        "worst_2020_2025_trades": [
            asdict(trade)
            for trade in sorted(
                trades_by_stage["2020_2025"],
                key=lambda item: item.net_pnl,
            )[:15]
        ],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, indent=2, sort_keys=True), encoding="utf-8")

    for stage, trades in trades_by_stage.items():
        summary = _aggregate(trades)
        print(
            f"{stage} trades={summary['trade_count']} net=${float(summary['net_pnl']):+.2f} "
            f"pf={float(summary['profit_factor']):.2f} "
            f"stop_rate={float(summary['stop_rate']):.1%} "
            f"avg_loss=${float(summary['average_loser']):+.2f} "
            f"worst5_loss_share={float(summary['worst_5_loss_share']):.1%}"
        )
    print(f"RESULT_PATH={args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
