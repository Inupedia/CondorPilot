"""Event-driven backtesting for the mechanical Iron Condor strategy."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum

from condorpilot.execution import ExecutionConfig, close_debit, entry_credit
from condorpilot.history import HistoricalDataError, OptionChainSnapshot, validate_history
from condorpilot.models import IronCondor, StrategyConfig
from condorpilot.risk import ExitAction, contracts_for_risk_budget, evaluate_exit
from condorpilot.strategy import NoTradeError, build_iron_condor

EntryFilter = Callable[[OptionChainSnapshot], bool]


class BacktestDataError(HistoricalDataError):
    """Raised when a held position cannot be valued from the supplied history."""


class ExitReason(StrEnum):
    TAKE_PROFIT = "take_profit"
    STOP_LOSS = "stop_loss"
    TIME_EXIT = "time_exit"
    EXPIRATION = "expiration"
    END_OF_DATA = "end_of_data"


@dataclass(frozen=True, slots=True)
class BacktestConfig:
    initial_equity: float = 50_000.0
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)

    def __post_init__(self) -> None:
        if self.initial_equity <= 0:
            raise ValueError("initial_equity must be positive")


@dataclass(frozen=True, slots=True)
class TradeRecord:
    symbol: str
    opened_at: datetime
    closed_at: datetime
    expiration: date
    contracts: int
    long_put_strike: float
    short_put_strike: float
    short_call_strike: float
    long_call_strike: float
    entry_spot: float
    exit_spot: float
    entry_credit: float
    exit_debit: float
    entry_commission: float
    exit_commission: float
    exit_reason: ExitReason

    @property
    def gross_pnl(self) -> float:
        return (self.entry_credit - self.exit_debit) * 100 * self.contracts

    @property
    def commissions(self) -> float:
        return self.entry_commission + self.exit_commission

    @property
    def net_pnl(self) -> float:
        return self.gross_pnl - self.commissions

    @property
    def holding_days(self) -> int:
        return (self.closed_at.date() - self.opened_at.date()).days


@dataclass(frozen=True, slots=True)
class EquityPoint:
    observed_at: datetime
    equity: float
    realized_equity: float
    has_open_position: bool


@dataclass(frozen=True, slots=True)
class BacktestResult:
    initial_equity: float
    trades: tuple[TradeRecord, ...]
    equity_curve: tuple[EquityPoint, ...]

    @property
    def final_equity(self) -> float:
        if not self.equity_curve:
            return self.initial_equity
        return self.equity_curve[-1].equity

    @property
    def net_profit(self) -> float:
        return self.final_equity - self.initial_equity

    @property
    def total_return(self) -> float:
        return self.net_profit / self.initial_equity

    @property
    def win_rate(self) -> float:
        if not self.trades:
            return 0.0
        winners = sum(trade.net_pnl > 0 for trade in self.trades)
        return winners / len(self.trades)

    @property
    def max_drawdown(self) -> float:
        peak = self.initial_equity
        maximum = 0.0
        for point in self.equity_curve:
            peak = max(peak, point.equity)
            drawdown = (peak - point.equity) / peak
            maximum = max(maximum, drawdown)
        return maximum


@dataclass(frozen=True, slots=True)
class _OpenPosition:
    condor: IronCondor
    opened_at: datetime
    entry_spot: float
    entry_credit: float
    contracts: int
    entry_commission: float


def _reprice(position: _OpenPosition, snapshot: OptionChainSnapshot) -> IronCondor:
    source = position.condor
    try:
        return IronCondor(
            long_put=snapshot.quote_for(
                expiration=source.expiration,
                strike=source.long_put.strike,
                option_type=source.long_put.option_type,
            ),
            short_put=snapshot.quote_for(
                expiration=source.expiration,
                strike=source.short_put.strike,
                option_type=source.short_put.option_type,
            ),
            short_call=snapshot.quote_for(
                expiration=source.expiration,
                strike=source.short_call.strike,
                option_type=source.short_call.option_type,
            ),
            long_call=snapshot.quote_for(
                expiration=source.expiration,
                strike=source.long_call.strike,
                option_type=source.long_call.option_type,
            ),
            multiplier=source.multiplier,
        )
    except HistoricalDataError as exc:
        raise BacktestDataError(str(exc)) from exc


def _expiration_close_debit(condor: IronCondor, spot: float) -> float:
    long_put = max(condor.long_put.strike - spot, 0.0)
    short_put = max(condor.short_put.strike - spot, 0.0)
    short_call = max(spot - condor.short_call.strike, 0.0)
    long_call = max(spot - condor.long_call.strike, 0.0)
    return max(0.0, short_put + short_call - long_put - long_call)


def _reason_from_action(action: ExitAction) -> ExitReason:
    mapping = {
        ExitAction.TAKE_PROFIT: ExitReason.TAKE_PROFIT,
        ExitAction.STOP_LOSS: ExitReason.STOP_LOSS,
        ExitAction.TIME_EXIT: ExitReason.TIME_EXIT,
    }
    return mapping[action]


def _marked_equity(
    *,
    realized_equity: float,
    position: _OpenPosition,
    current_close_debit: float,
    execution: ExecutionConfig,
) -> float:
    gross_unrealized = (
        position.entry_credit - current_close_debit
    ) * position.condor.multiplier * position.contracts
    estimated_exit_commission = execution.commission(position.contracts)
    return (
        realized_equity
        + gross_unrealized
        - position.entry_commission
        - estimated_exit_commission
    )


def _append_flat_equity(
    equity_curve: list[EquityPoint],
    *,
    snapshot: OptionChainSnapshot,
    realized_equity: float,
) -> None:
    equity_curve.append(
        EquityPoint(
            observed_at=snapshot.observed_at,
            equity=realized_equity,
            realized_equity=realized_equity,
            has_open_position=False,
        )
    )


def run_backtest(
    snapshots: tuple[OptionChainSnapshot, ...] | list[OptionChainSnapshot],
    *,
    config: BacktestConfig | None = None,
    entry_filter: EntryFilter | None = None,
) -> BacktestResult:
    """Run a single-position event-driven backtest over timestamped option chains.

    ``entry_filter`` is evaluated only while flat. Held positions continue to be marked and
    managed on every snapshot, even when that snapshot would reject a new entry. This makes
    volatility/event regime filters safe to compose without corrupting position valuation.
    """
    config = config or BacktestConfig()
    history = validate_history(snapshots)
    realized_equity = config.initial_equity
    position: _OpenPosition | None = None
    trades: list[TradeRecord] = []
    equity_curve: list[EquityPoint] = []

    for index, snapshot in enumerate(history):
        is_last = index == len(history) - 1

        if position is not None:
            dte = (position.condor.expiration - snapshot.as_of).days
            if dte <= 0:
                exit_debit_value = _expiration_close_debit(position.condor, snapshot.spot)
                exit_reason = ExitReason.EXPIRATION
            else:
                marked = _reprice(position, snapshot)
                exit_debit_value = close_debit(marked, config.execution)
                action = evaluate_exit(
                    entry_credit=position.entry_credit,
                    current_close_debit=exit_debit_value,
                    dte=dte,
                    config=config.strategy,
                )
                if action is ExitAction.HOLD and not is_last:
                    equity_curve.append(
                        EquityPoint(
                            observed_at=snapshot.observed_at,
                            equity=_marked_equity(
                                realized_equity=realized_equity,
                                position=position,
                                current_close_debit=exit_debit_value,
                                execution=config.execution,
                            ),
                            realized_equity=realized_equity,
                            has_open_position=True,
                        )
                    )
                    continue
                exit_reason = (
                    ExitReason.END_OF_DATA
                    if action is ExitAction.HOLD
                    else _reason_from_action(action)
                )

            exit_commission = config.execution.commission(position.contracts)
            trade = TradeRecord(
                symbol=position.condor.symbol,
                opened_at=position.opened_at,
                closed_at=snapshot.observed_at,
                expiration=position.condor.expiration,
                contracts=position.contracts,
                long_put_strike=position.condor.long_put.strike,
                short_put_strike=position.condor.short_put.strike,
                short_call_strike=position.condor.short_call.strike,
                long_call_strike=position.condor.long_call.strike,
                entry_spot=position.entry_spot,
                exit_spot=snapshot.spot,
                entry_credit=position.entry_credit,
                exit_debit=exit_debit_value,
                entry_commission=position.entry_commission,
                exit_commission=exit_commission,
                exit_reason=exit_reason,
            )
            trades.append(trade)
            realized_equity += trade.net_pnl
            position = None
            _append_flat_equity(
                equity_curve,
                snapshot=snapshot,
                realized_equity=realized_equity,
            )
            continue

        if is_last:
            _append_flat_equity(
                equity_curve,
                snapshot=snapshot,
                realized_equity=realized_equity,
            )
            continue

        if entry_filter is not None and not entry_filter(snapshot):
            _append_flat_equity(
                equity_curve,
                snapshot=snapshot,
                realized_equity=realized_equity,
            )
            continue

        try:
            condor = build_iron_condor(
                list(snapshot.quotes),
                spot=snapshot.spot,
                as_of=snapshot.as_of,
                config=config.strategy,
            )
        except NoTradeError:
            _append_flat_equity(
                equity_curve,
                snapshot=snapshot,
                realized_equity=realized_equity,
            )
            continue

        modeled_entry_credit = entry_credit(condor, config.execution)
        if modeled_entry_credit <= 0:
            _append_flat_equity(
                equity_curve,
                snapshot=snapshot,
                realized_equity=realized_equity,
            )
            continue
        if modeled_entry_credit / condor.max_width < config.strategy.min_credit_to_width:
            _append_flat_equity(
                equity_curve,
                snapshot=snapshot,
                realized_equity=realized_equity,
            )
            continue

        round_trip_commission_per_contract = config.execution.commission(1) * 2
        max_loss_per_contract = (
            (condor.max_width - modeled_entry_credit) * condor.multiplier
            + round_trip_commission_per_contract
        )
        if max_loss_per_contract <= 0:
            raise BacktestDataError("modeled entry implies non-positive maximum risk")

        contracts = contracts_for_risk_budget(
            account_equity=realized_equity,
            max_loss_per_contract=max_loss_per_contract,
            max_risk_fraction=config.strategy.max_risk_fraction,
        )
        if contracts == 0:
            _append_flat_equity(
                equity_curve,
                snapshot=snapshot,
                realized_equity=realized_equity,
            )
            continue

        position = _OpenPosition(
            condor=condor,
            opened_at=snapshot.observed_at,
            entry_spot=snapshot.spot,
            entry_credit=modeled_entry_credit,
            contracts=contracts,
            entry_commission=config.execution.commission(contracts),
        )
        immediate_close_debit = close_debit(condor, config.execution)
        equity_curve.append(
            EquityPoint(
                observed_at=snapshot.observed_at,
                equity=_marked_equity(
                    realized_equity=realized_equity,
                    position=position,
                    current_close_debit=immediate_close_debit,
                    execution=config.execution,
                ),
                realized_equity=realized_equity,
                has_open_position=True,
            )
        )

    return BacktestResult(
        initial_equity=config.initial_equity,
        trades=tuple(trades),
        equity_curve=tuple(equity_curve),
    )
