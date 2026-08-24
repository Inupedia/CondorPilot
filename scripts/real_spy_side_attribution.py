"""Decompose the rejected SPY Iron Condor and test each credit-spread side independently.

This study answers two separate questions without changing the frozen strategy parameters:

1. Exact attribution: replay the 2020-2025 Iron Condor baseline and algebraically split every
   trade's net P/L into its put-credit-spread and call-credit-spread legs. Entries, exits,
   contract counts, and total commissions remain identical to the original Condor replay.
2. Standalone falsification: run a 15-delta / 5-point Bull Put Spread and Bear Call Spread as
   independent strategies. Discovery is SPY 2016-2019. A side is evaluated on 2020-2025 only
   when it clears fixed discovery gates.

The standalone spread selector is independent: a missing opposite-side option never prevents
an otherwise valid spread entry. Entries always require same-day quotes. Held contracts may use
only the immediately previous trading observation as a disclosed fallback, matching the prior
real-data research policy.
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

from condorpilot.execution import (
    ExecutionConfig,
    ExecutionDataError,
    close_debit as condor_close_debit,
    entry_credit as condor_entry_credit,
)
from condorpilot.models import IronCondor, OptionQuote, OptionType, StrategyConfig
from condorpilot.risk import ExitAction, contracts_for_risk_budget, evaluate_exit
from condorpilot.strategy import (
    NoTradeError,
    _closest_expiration,
    _quote_at_strike,
    _short_by_delta,
    build_iron_condor,
)
from scripts.real_spy_baseline import (
    _entry_chain,
    _held_leg_row,
    _max_drawdown,
    _prepare_database,
    _quote,
)
from scripts.real_spy_sensitivity import _held_close_mark

Side = Literal["put", "call"]

DISCOVERY_START = date(2016, 1, 4)
DISCOVERY_END = date(2019, 12, 31)
VALIDATION_START = date(2020, 1, 2)
VALIDATION_END = date(2025, 12, 12)
EXPECTED_BASELINE_TRADES = 98
EXPECTED_BASELINE_NET_PNL = -3091.95
MIN_DISCOVERY_TRADES = 40
MIN_DISCOVERY_PF = 1.10
MAX_DISCOVERY_DRAWDOWN = 0.10
MIN_VALIDATION_TRADES = 40
MIN_VALIDATION_PF = 1.0


@dataclass(frozen=True, slots=True)
class CreditSpread:
    side: Side
    long: OptionQuote
    short: OptionQuote

    def __post_init__(self) -> None:
        expected = OptionType.PUT if self.side == "put" else OptionType.CALL
        if self.long.option_type is not expected or self.short.option_type is not expected:
            raise ValueError("credit-spread legs do not match side")
        if self.long.expiration != self.short.expiration:
            raise ValueError("credit-spread legs must share expiration")
        if self.long.symbol != self.short.symbol:
            raise ValueError("credit-spread legs must share symbol")
        if self.side == "put" and not self.long.strike < self.short.strike:
            raise ValueError("put spread requires long strike below short strike")
        if self.side == "call" and not self.short.strike < self.long.strike:
            raise ValueError("call spread requires short strike below long strike")

    @property
    def expiration(self) -> date:
        return self.short.expiration

    @property
    def width(self) -> float:
        return abs(self.long.strike - self.short.strike)

    @property
    def mid_credit(self) -> float:
        return self.short.mid - self.long.mid


@dataclass(slots=True)
class SpreadPosition:
    spread: CreditSpread
    opened_on: date
    entry_spot: float
    entry_credit: float
    contracts: int
    entry_commission: float


@dataclass(frozen=True, slots=True)
class SpreadTrade:
    opened_on: date
    closed_on: date
    expiration: date
    contracts: int
    entry_spot: float
    exit_spot: float
    entry_credit: float
    exit_debit: float
    entry_commission: float
    exit_commission: float
    reason: str
    strikes: tuple[float, float]

    @property
    def net_pnl(self) -> float:
        gross = (self.entry_credit - self.exit_debit) * 100 * self.contracts
        return gross - self.entry_commission - self.exit_commission


@dataclass(slots=True)
class CondorAttributionPosition:
    condor: IronCondor
    opened_on: date
    entry_spot: float
    entry_credit: float
    contracts: int
    entry_commission: float
    put_entry_credit: float
    call_entry_credit: float


def _strategy() -> StrategyConfig:
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
        min_credit_to_width=0.10,
    )


def _execution() -> ExecutionConfig:
    return ExecutionConfig(slippage_fraction=0.25, commission_per_contract_per_leg=0.65)


def _sell_fill(quote: OptionQuote, slippage_fraction: float) -> float:
    return quote.mid - slippage_fraction * (quote.mid - quote.bid)


def _buy_fill(quote: OptionQuote, slippage_fraction: float) -> float:
    return quote.mid + slippage_fraction * (quote.ask - quote.mid)


def _spread_entry_credit(spread: CreditSpread, execution: ExecutionConfig) -> float:
    slip = execution.slippage_fraction
    return _sell_fill(spread.short, slip) - _buy_fill(spread.long, slip)


def _spread_close_value(
    spread: CreditSpread,
    execution: ExecutionConfig,
    *,
    require_non_negative: bool,
) -> float:
    slip = execution.slippage_fraction
    debit = _buy_fill(spread.short, slip) - _sell_fill(spread.long, slip)
    if require_non_negative and debit < -1e-9:
        raise ExecutionDataError(
            f"modeled {spread.side} spread close debit is negative ({debit:.4f})"
        )
    return max(0.0, debit) if require_non_negative else debit


def _spread_commission(execution: ExecutionConfig, contracts: int) -> float:
    if contracts < 0:
        raise ValueError("contracts must be non-negative")
    return contracts * 2 * execution.commission_per_contract_per_leg


def _spread_from_condor(condor: IronCondor, side: Side) -> CreditSpread:
    if side == "put":
        return CreditSpread(side="put", long=condor.long_put, short=condor.short_put)
    return CreditSpread(side="call", long=condor.long_call, short=condor.short_call)


def _build_spread(
    quotes: list[OptionQuote],
    *,
    spot: float,
    as_of: date,
    side: Side,
    config: StrategyConfig,
) -> CreditSpread:
    if spot <= 0:
        raise ValueError("spot must be positive")
    if not quotes:
        raise NoTradeError("option chain is empty")

    expiration = _closest_expiration(
        quotes,
        as_of=as_of,
        target_dte=config.target_dte,
        minimum_dte=config.exit_dte,
        max_deviation_days=config.max_dte_deviation_days,
    )
    chain = [quote for quote in quotes if quote.expiration == expiration]
    option_type = OptionType.PUT if side == "put" else OptionType.CALL
    short = _short_by_delta(
        chain,
        option_type=option_type,
        spot=spot,
        target_delta=config.short_delta,
        max_delta_deviation=config.max_delta_deviation,
        max_relative_spread=config.max_bid_ask_spread_fraction,
    )
    wing_strike = (
        short.strike - config.wing_width if side == "put" else short.strike + config.wing_width
    )
    long = _quote_at_strike(
        chain,
        option_type=option_type,
        strike=wing_strike,
        max_relative_spread=config.max_bid_ask_spread_fraction,
    )
    spread = CreditSpread(side=side, long=long, short=short)
    if spread.mid_credit <= 0:
        raise NoTradeError(f"selected {side} spread does not produce a net credit")
    if spread.mid_credit / spread.width < config.min_credit_to_width:
        raise NoTradeError(
            f"{side} spread entry credit is below the configured credit-to-width threshold"
        )
    return spread


def _mark_spread(
    con: duckdb.DuckDBPyConnection,
    *,
    day: date,
    previous_day: date | None,
    source: CreditSpread,
) -> tuple[CreditSpread, tuple[dict[str, object], ...]]:
    stale_events: list[dict[str, object]] = []

    def find(source_leg: OptionQuote) -> OptionQuote:
        row = _held_leg_row(
            con,
            day=day,
            expiration=source.expiration,
            strike=source_leg.strike,
            option_type=source_leg.option_type,
        )
        if row is not None:
            return _quote(row, fallback_delta=source_leg.delta)
        if previous_day is None:
            raise RuntimeError(
                f"missing held {source.side} spread quote on {day}: "
                f"{source.expiration} {source_leg.strike:g} {source_leg.option_type.value}"
            )
        previous = _held_leg_row(
            con,
            day=previous_day,
            expiration=source.expiration,
            strike=source_leg.strike,
            option_type=source_leg.option_type,
        )
        if previous is None:
            raise RuntimeError(
                "held spread quote is missing for at least two consecutive observations: "
                f"side={source.side}, expiration={source.expiration}, strike={source_leg.strike:g}, "
                f"current={day}, prior={previous_day}"
            )
        stale_events.append(
            {
                "reason": "missing_leg",
                "mark_date": day.isoformat(),
                "source_date": previous_day.isoformat(),
                "side": source.side,
                "expiration": source.expiration.isoformat(),
                "strike": source_leg.strike,
                "option_type": source_leg.option_type.value,
            }
        )
        return _quote(previous, fallback_delta=source_leg.delta)

    return CreditSpread(side=source.side, long=find(source.long), short=find(source.short)), tuple(
        stale_events
    )


def _held_spread_close(
    con: duckdb.DuckDBPyConnection,
    *,
    day: date,
    previous_day: date | None,
    source: CreditSpread,
    execution: ExecutionConfig,
) -> tuple[CreditSpread, float, tuple[dict[str, object], ...]]:
    marked, stale = _mark_spread(con, day=day, previous_day=previous_day, source=source)
    try:
        return marked, _spread_close_value(marked, execution, require_non_negative=True), stale
    except ExecutionDataError as current_error:
        if previous_day is None:
            raise RuntimeError(
                f"inconsistent held {source.side} spread quote on {day} with no prior day"
            ) from current_error
        try:
            prior, _ = _mark_spread(con, day=previous_day, previous_day=None, source=source)
            prior_debit = _spread_close_value(prior, execution, require_non_negative=True)
        except (ExecutionDataError, RuntimeError) as prior_error:
            raise RuntimeError(
                f"{source.side} spread is unmarkable on current and previous trading day"
            ) from prior_error
        fallback = tuple(
            {
                "reason": "inconsistent_same_day_spread_quote",
                "mark_date": day.isoformat(),
                "source_date": previous_day.isoformat(),
                "side": source.side,
                "expiration": source.expiration.isoformat(),
                "strike": leg.strike,
                "option_type": leg.option_type.value,
            }
            for leg in (source.long, source.short)
        )
        return prior, prior_debit, fallback


def _profit_factor(trades: list[SpreadTrade]) -> float | None:
    profits = sum(trade.net_pnl for trade in trades if trade.net_pnl > 0)
    losses = -sum(trade.net_pnl for trade in trades if trade.net_pnl < 0)
    if losses > 0:
        return profits / losses
    if profits > 0:
        return None
    return 0.0


def _marked_spread_equity(
    realized_equity: float,
    position: SpreadPosition,
    current_close_debit: float,
    execution: ExecutionConfig,
) -> float:
    gross = (position.entry_credit - current_close_debit) * 100 * position.contracts
    return (
        realized_equity
        + gross
        - position.entry_commission
        - _spread_commission(execution, position.contracts)
    )


def _run_spread(
    con: duckdb.DuckDBPyConnection,
    *,
    side: Side,
    start: date,
    end: date,
    initial_equity: float = 50_000.0,
) -> dict[str, object]:
    strategy = _strategy()
    execution = _execution()
    realized_equity = initial_equity
    position: SpreadPosition | None = None
    trades: list[SpreadTrade] = []
    stale_events: list[dict[str, object]] = []
    no_trade_reasons: Counter[str] = Counter()
    equity_rows: list[tuple[date, float, bool]] = []
    entry_attempts = 0
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
                    f"crossed spread expiration without exit: {position.spread.expiration} vs {day}"
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
            spread = _build_spread(chain, spot=spot, as_of=day, side=side, config=strategy)
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

        if credit <= 0 or credit / spread.width < strategy.min_credit_to_width:
            no_trade_reasons["modeled execution credit below entry threshold"] += 1
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
        raise RuntimeError(f"{side} spread period ended with an open position")

    equities = [row[1] for row in equity_rows]
    winners = [trade for trade in trades if trade.net_pnl > 0]
    losers = [trade for trade in trades if trade.net_pnl < 0]
    final_equity = equities[-1]
    return {
        "side": side,
        "start": rows[0][0].isoformat(),
        "end": rows[-1][0].isoformat(),
        "trade_count": len(trades),
        "entry_attempt_days": entry_attempts,
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
        "stale_mark_reason_counts": dict(
            Counter(str(event.get("reason", "missing_leg")) for event in stale_events)
        ),
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


def _run_condor_attribution(
    con: duckdb.DuckDBPyConnection,
    *,
    start: date = VALIDATION_START,
    end: date = VALIDATION_END,
    initial_equity: float = 50_000.0,
) -> dict[str, object]:
    strategy = _strategy()
    execution = _execution()
    realized_equity = initial_equity
    position: CondorAttributionPosition | None = None
    full_net_values: list[float] = []
    put_net_values: list[float] = []
    call_net_values: list[float] = []
    exit_reasons: Counter[str] = Counter()
    stale_events: list[dict[str, object]] = []
    no_trade_reasons: Counter[str] = Counter()
    negative_side_marks: Counter[str] = Counter()
    max_reconciliation_error = 0.0
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
        raise RuntimeError("attribution underlying history is empty")
    final_day = rows[-1][0]

    for index, (day, spot_raw) in enumerate(rows):
        spot = float(spot_raw)
        previous_day = rows[index - 1][0] if index > 0 else None

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
                continue

            put_marked = _spread_from_condor(marked, "put")
            call_marked = _spread_from_condor(marked, "call")
            put_debit = _spread_close_value(
                put_marked,
                execution,
                require_non_negative=False,
            )
            call_debit = _spread_close_value(
                call_marked,
                execution,
                require_non_negative=False,
            )
            if put_debit < 0:
                negative_side_marks["put"] += 1
            if call_debit < 0:
                negative_side_marks["call"] += 1

            full_exit_commission = execution.commission(position.contracts)
            side_exit_commission = _spread_commission(execution, position.contracts)
            full_net = (
                (position.entry_credit - debit) * 100 * position.contracts
                - position.entry_commission
                - full_exit_commission
            )
            put_net = (
                (position.put_entry_credit - put_debit) * 100 * position.contracts
                - side_exit_commission
                - side_exit_commission
            )
            call_net = (
                (position.call_entry_credit - call_debit) * 100 * position.contracts
                - side_exit_commission
                - side_exit_commission
            )
            reconciliation_error = abs(full_net - (put_net + call_net))
            max_reconciliation_error = max(max_reconciliation_error, reconciliation_error)
            if reconciliation_error > 1e-6:
                raise RuntimeError(
                    f"side attribution does not reconcile on {day}: error={reconciliation_error}"
                )

            full_net_values.append(full_net)
            put_net_values.append(put_net)
            call_net_values.append(call_net)
            exit_reasons[action.value] += 1
            realized_equity += full_net
            position = None
            continue

        if (final_day - day).days < entry_embargo_days:
            continue

        chain = _entry_chain(con, day, strategy)
        try:
            condor = build_iron_condor(chain, spot=spot, as_of=day, config=strategy)
            credit = condor_entry_credit(condor, execution)
            condor_close_debit(condor, execution)
        except (NoTradeError, ExecutionDataError) as exc:
            no_trade_reasons[str(exc)] += 1
            continue
        if credit <= 0 or credit / condor.max_width < strategy.min_credit_to_width:
            no_trade_reasons["modeled execution credit below entry threshold"] += 1
            continue

        max_loss = (condor.max_width - credit) * 100 + execution.commission(1) * 2
        contracts = contracts_for_risk_budget(
            account_equity=realized_equity,
            max_loss_per_contract=max_loss,
            max_risk_fraction=strategy.max_risk_fraction,
        )
        if contracts <= 0:
            no_trade_reasons["risk budget cannot fund one contract"] += 1
            continue

        put_credit = _spread_entry_credit(_spread_from_condor(condor, "put"), execution)
        call_credit = _spread_entry_credit(_spread_from_condor(condor, "call"), execution)
        if abs(credit - (put_credit + call_credit)) > 1e-9:
            raise RuntimeError("entry side credits do not reconcile to condor credit")
        position = CondorAttributionPosition(
            condor=condor,
            opened_on=day,
            entry_spot=spot,
            entry_credit=credit,
            contracts=contracts,
            entry_commission=execution.commission(contracts),
            put_entry_credit=put_credit,
            call_entry_credit=call_credit,
        )

    if position is not None:
        raise RuntimeError("attribution period ended with an open Condor")

    full_net = sum(full_net_values)
    if len(full_net_values) != EXPECTED_BASELINE_TRADES:
        raise RuntimeError(
            f"baseline replay changed: trades={len(full_net_values)}, expected={EXPECTED_BASELINE_TRADES}"
        )
    if abs(full_net - EXPECTED_BASELINE_NET_PNL) > 0.05:
        raise RuntimeError(
            f"baseline replay changed: net={full_net:.2f}, expected={EXPECTED_BASELINE_NET_PNL:.2f}"
        )

    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "trade_count": len(full_net_values),
        "full_condor_net_pnl": full_net,
        "put_side_net_pnl": sum(put_net_values),
        "call_side_net_pnl": sum(call_net_values),
        "put_side_average_trade": sum(put_net_values) / len(put_net_values),
        "call_side_average_trade": sum(call_net_values) / len(call_net_values),
        "put_side_profitable_trade_fraction": sum(value > 0 for value in put_net_values)
        / len(put_net_values),
        "call_side_profitable_trade_fraction": sum(value > 0 for value in call_net_values)
        / len(call_net_values),
        "put_side_best_trade": max(put_net_values),
        "put_side_worst_trade": min(put_net_values),
        "call_side_best_trade": max(call_net_values),
        "call_side_worst_trade": min(call_net_values),
        "exit_reasons": dict(exit_reasons),
        "stale_mark_leg_count": len(stale_events),
        "stale_mark_day_count": len({event["mark_date"] for event in stale_events}),
        "negative_algebraic_side_close_marks": dict(negative_side_marks),
        "maximum_reconciliation_error": max_reconciliation_error,
        "reconciled_net_pnl": sum(put_net_values) + sum(call_net_values),
        "top_no_trade_reasons": no_trade_reasons.most_common(5),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    con = _prepare_database(args.data_dir, DISCOVERY_START, VALIDATION_END)
    attribution = _run_condor_attribution(con)

    discovery: list[dict[str, object]] = []
    validation: list[dict[str, object]] = []
    for side in ("put", "call"):
        result = _run_spread(
            con,
            side=side,
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
            f"DISCOVERY {side:>4} trades={result['trade_count']:>3} "
            f"ret={float(result['total_return']):>+7.2%} "
            f"dd={float(result['max_drawdown']):>6.2%} "
            f"win={float(result['win_rate']):>6.1%} pf={pf_text:>5} pass={passed}"
        )
        if not passed:
            continue

        holdout = _run_spread(
            con,
            side=side,
            start=VALIDATION_START,
            end=VALIDATION_END,
        )
        valid, validation_reasons = _validation_pass(holdout)
        holdout["validation_pass"] = valid
        holdout["validation_fail_reasons"] = validation_reasons
        validation.append(holdout)
        holdout_pf = holdout["profit_factor"]
        holdout_pf_text = "inf" if holdout_pf is None else f"{float(holdout_pf):.2f}"
        print(
            f"VALIDATE  {side:>4} trades={holdout['trade_count']:>3} "
            f"ret={float(holdout['total_return']):>+7.2%} "
            f"dd={float(holdout['max_drawdown']):>6.2%} "
            f"win={float(holdout['win_rate']):>6.1%} pf={holdout_pf_text:>5} pass={valid}"
        )

    external_candidates = [
        str(item["side"]) for item in validation if bool(item["validation_pass"])
    ]
    output = {
        "method": {
            "strategy_parameters": {
                "target_dte": 45,
                "short_delta": 0.15,
                "wing_width": 5.0,
                "profit_target_fraction": 0.50,
                "stop_loss_credit_multiple": 2.0,
                "exit_dte": 21,
                "max_risk_fraction": 0.02,
                "min_credit_to_width": 0.10,
                "slippage_fraction": 0.25,
                "commission_per_contract_per_leg": 0.65,
            },
            "attribution": (
                "same Condor entries/exits/contracts; split leg-level executable fill P/L and "
                "split four-leg commissions evenly into two two-leg sides"
            ),
            "standalone_selection": (
                "each side independently selects the closest eligible expiration, closest "
                "15-delta OTM short, and exact 5-point protective wing"
            ),
            "discovery_period": [DISCOVERY_START.isoformat(), DISCOVERY_END.isoformat()],
            "validation_period": [VALIDATION_START.isoformat(), VALIDATION_END.isoformat()],
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
            },
            "qqq_rule": (
                "QQQ external holdout is allowed only for a side that passes both SPY discovery "
                "and SPY temporal validation"
            ),
        },
        "condor_attribution_2020_2025": attribution,
        "standalone_discovery": discovery,
        "standalone_validation": validation,
        "external_holdout_candidates": external_candidates,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, indent=2, sort_keys=True), encoding="utf-8")

    print(
        "ATTRIBUTION "
        f"trades={attribution['trade_count']} "
        f"condor=${float(attribution['full_condor_net_pnl']):+.2f} "
        f"put=${float(attribution['put_side_net_pnl']):+.2f} "
        f"call=${float(attribution['call_side_net_pnl']):+.2f} "
        f"recon_error={float(attribution['maximum_reconciliation_error']):.8f}"
    )
    print(f"EXTERNAL_CANDIDATES={external_candidates}")
    print(f"RESULT_PATH={args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
