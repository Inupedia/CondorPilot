"""Run the frozen CondorPilot baseline against real SPY EOD option-chain parquet files.

Expected source schema is the Philipp D. Dubach historical-options dataset mirror:
https://github.com/anahatsingh-ui/options-dataset-hist

This script deliberately uses the existing CondorPilot strategy/execution/risk functions. It
performs no parameter optimization. The fixed baseline is 45 DTE / 15 delta / 5-point wings /
50% profit target / 2x close-debit stop / 21 DTE time exit / 2% max account risk.
"""

from __future__ import annotations

import argparse
import json
import math
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

import duckdb

from condorpilot.execution import ExecutionConfig, close_debit, entry_credit
from condorpilot.models import IronCondor, OptionQuote, OptionType, StrategyConfig
from condorpilot.risk import ExitAction, contracts_for_risk_budget, evaluate_exit
from condorpilot.strategy import NoTradeError, build_iron_condor


DATASET_URL = "https://github.com/anahatsingh-ui/options-dataset-hist"


@dataclass
class Position:
    condor: IronCondor
    opened_on: date
    entry_spot: float
    entry_credit: float
    contracts: int
    entry_commission: float


@dataclass
class Trade:
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
    strikes: tuple[float, float, float, float]

    @property
    def gross_pnl(self) -> float:
        return (self.entry_credit - self.exit_debit) * 100 * self.contracts

    @property
    def net_pnl(self) -> float:
        return self.gross_pnl - self.entry_commission - self.exit_commission

    @property
    def holding_days(self) -> int:
        return (self.closed_on - self.opened_on).days


def _option_type(value: str) -> OptionType:
    normalized = value.lower()
    if normalized in {"call", "c"}:
        return OptionType.CALL
    if normalized in {"put", "p"}:
        return OptionType.PUT
    raise ValueError(f"unknown option type {value!r}")


def _quote(row: tuple) -> OptionQuote:
    expiration, strike, kind, bid, ask, delta, iv = row
    iv_value = None if iv is None or not math.isfinite(float(iv)) else float(iv)
    return OptionQuote(
        symbol="SPY",
        expiration=expiration,
        strike=float(strike),
        option_type=_option_type(str(kind)),
        bid=float(bid),
        ask=float(ask),
        delta=float(delta),
        implied_volatility=iv_value,
    )


def _prepare_database(data_dir: Path, start: date, end: date) -> duckdb.DuckDBPyConnection:
    option_glob = str(data_dir / "options_*.parquet").replace("'", "''")
    underlying = str(data_dir / "underlying_prices.parquet").replace("'", "''")
    db_path = data_dir / "real_spy.duckdb"
    if db_path.exists():
        db_path.unlink()
    con = duckdb.connect(str(db_path))
    con.execute("PRAGMA threads=4")
    con.execute(
        f"""
        CREATE TABLE options AS
        SELECT
            CAST(date AS DATE) AS quote_date,
            CAST(expiration AS DATE) AS expiration,
            CAST(strike AS DOUBLE) AS strike,
            lower(CAST(type AS VARCHAR)) AS option_type,
            CAST(bid AS DOUBLE) AS bid,
            CAST(ask AS DOUBLE) AS ask,
            CAST(delta AS DOUBLE) AS delta,
            CAST(implied_volatility AS DOUBLE) AS implied_volatility
        FROM read_parquet('{option_glob}')
        WHERE CAST(date AS DATE) BETWEEN ? AND ?
          AND date_diff('day', CAST(date AS DATE), CAST(expiration AS DATE)) BETWEEN 14 AND 55
          AND bid IS NOT NULL AND ask IS NOT NULL AND delta IS NOT NULL
          AND CAST(bid AS DOUBLE) >= 0
          AND CAST(ask AS DOUBLE) >= CAST(bid AS DOUBLE)
          AND CAST(delta AS DOUBLE) BETWEEN -1 AND 1
          AND CAST(strike AS DOUBLE) > 0
        """,
        [start, end],
    )
    con.execute(
        f"""
        CREATE TABLE underlying AS
        SELECT
            CAST(date AS DATE) AS quote_date,
            CAST(close AS DOUBLE) AS close,
            CAST(adjusted_close AS DOUBLE) AS adjusted_close
        FROM read_parquet('{underlying}')
        WHERE upper(CAST(symbol AS VARCHAR)) = 'SPY'
          AND CAST(date AS DATE) BETWEEN ? AND ?
          AND close IS NOT NULL
        ORDER BY quote_date
        """,
        [start, end],
    )
    con.execute("CREATE INDEX options_date_expiry ON options(quote_date, expiration)")
    return con


def _entry_chain(con: duckdb.DuckDBPyConnection, day: date, config: StrategyConfig) -> list[OptionQuote]:
    min_dte = config.target_dte - config.max_dte_deviation_days
    max_dte = config.target_dte + config.max_dte_deviation_days
    rows = con.execute(
        """
        SELECT expiration, strike, option_type, bid, ask, delta, implied_volatility
        FROM options
        WHERE quote_date = ?
          AND date_diff('day', quote_date, expiration) BETWEEN ? AND ?
        ORDER BY expiration, strike, option_type
        """,
        [day, min_dte, max_dte],
    ).fetchall()
    return [_quote(row) for row in rows]


def _mark_condor(
    con: duckdb.DuckDBPyConnection,
    day: date,
    source: IronCondor,
) -> IronCondor:
    rows = con.execute(
        """
        SELECT expiration, strike, option_type, bid, ask, delta, implied_volatility
        FROM options
        WHERE quote_date = ? AND expiration = ?
        ORDER BY strike, option_type
        """,
        [day, source.expiration],
    ).fetchall()
    quotes = {_quote(row).option_type.value + f"|{float(row[1]):g}": _quote(row) for row in rows}

    def find(option_type: OptionType, strike: float) -> OptionQuote:
        key = option_type.value + f"|{strike:g}"
        if key not in quotes:
            raise RuntimeError(
                f"missing held-leg quote on {day}: {source.symbol} {source.expiration} "
                f"{strike:g} {option_type.value}"
            )
        return quotes[key]

    return IronCondor(
        long_put=find(OptionType.PUT, source.long_put.strike),
        short_put=find(OptionType.PUT, source.short_put.strike),
        short_call=find(OptionType.CALL, source.short_call.strike),
        long_call=find(OptionType.CALL, source.long_call.strike),
    )


def _marked_equity(
    realized_equity: float,
    position: Position,
    current_close_debit: float,
    execution: ExecutionConfig,
) -> float:
    gross = (position.entry_credit - current_close_debit) * 100 * position.contracts
    return (
        realized_equity
        + gross
        - position.entry_commission
        - execution.commission(position.contracts)
    )


def _max_drawdown(equities: list[float], initial: float) -> float:
    peak = initial
    maximum = 0.0
    for equity in equities:
        peak = max(peak, equity)
        maximum = max(maximum, (peak - equity) / peak)
    return maximum


def _profit_factor(trades: list[Trade]) -> float | None:
    profits = sum(trade.net_pnl for trade in trades if trade.net_pnl > 0)
    losses = -sum(trade.net_pnl for trade in trades if trade.net_pnl < 0)
    if losses > 0:
        return profits / losses
    if profits > 0:
        return None
    return 0.0


def _run(con: duckdb.DuckDBPyConnection, start: date, end: date) -> dict:
    strategy = StrategyConfig(
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
    execution = ExecutionConfig(slippage_fraction=0.25, commission_per_contract_per_leg=0.65)
    initial_equity = 50_000.0
    realized_equity = initial_equity
    position: Position | None = None
    trades: list[Trade] = []
    no_trade_reasons: Counter[str] = Counter()
    equity_rows: list[tuple[date, float, bool]] = []
    entry_attempts = 0
    entry_embargo_days = (
        strategy.target_dte + strategy.max_dte_deviation_days - strategy.exit_dte
    )

    underlying_rows = con.execute(
        "SELECT quote_date, close, adjusted_close FROM underlying ORDER BY quote_date"
    ).fetchall()
    if not underlying_rows:
        raise RuntimeError("underlying history is empty")
    final_day = underlying_rows[-1][0]

    for day, spot_raw, _adjusted in underlying_rows:
        spot = float(spot_raw)
        if position is not None:
            marked = _mark_condor(con, day, position.condor)
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
        max_loss = (
            (condor.max_width - credit) * 100
            + execution.commission(1) * 2
        )
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
        raise RuntimeError("entry embargo failed: position remained open at end of data")

    equities = [row[1] for row in equity_rows]
    final_equity = equities[-1]
    total_return = final_equity / initial_equity - 1
    elapsed_years = max((underlying_rows[-1][0] - underlying_rows[0][0]).days / 365.25, 1e-9)
    cagr = (final_equity / initial_equity) ** (1 / elapsed_years) - 1
    winners = [trade for trade in trades if trade.net_pnl > 0]
    losses = [trade for trade in trades if trade.net_pnl < 0]
    pf = _profit_factor(trades)

    year_end: dict[int, float] = {}
    for day, equity, _ in equity_rows:
        year_end[day.year] = equity
    annual_returns: dict[str, float] = {}
    prior = initial_equity
    for year in sorted(year_end):
        annual_returns[str(year)] = year_end[year] / prior - 1
        prior = year_end[year]

    entry_year_pnl: dict[str, float] = {}
    for trade in trades:
        key = str(trade.opened_on.year)
        entry_year_pnl[key] = entry_year_pnl.get(key, 0.0) + trade.net_pnl

    first_close = float(underlying_rows[0][1])
    last_close = float(underlying_rows[-1][1])
    first_adjusted = float(underlying_rows[0][2])
    last_adjusted = float(underlying_rows[-1][2])
    exposure = sum(1 for _, _, open_position in equity_rows if open_position) / len(equity_rows)

    return {
        "dataset": {
            "source": DATASET_URL,
            "symbol": "SPY",
            "start": underlying_rows[0][0].isoformat(),
            "end": underlying_rows[-1][0].isoformat(),
            "trading_days": len(underlying_rows),
            "filtered_option_rows": con.execute("SELECT count(*) FROM options").fetchone()[0],
            "snapshot": "end-of-day",
        },
        "strategy": {
            "target_dte": strategy.target_dte,
            "max_dte_deviation_days": strategy.max_dte_deviation_days,
            "short_delta": strategy.short_delta,
            "max_delta_deviation": strategy.max_delta_deviation,
            "wing_width": strategy.wing_width,
            "profit_target_fraction": strategy.profit_target_fraction,
            "stop_loss_credit_multiple": strategy.stop_loss_credit_multiple,
            "exit_dte": strategy.exit_dte,
            "max_risk_fraction": strategy.max_risk_fraction,
            "min_credit_to_width": strategy.min_credit_to_width,
            "slippage_fraction": execution.slippage_fraction,
            "commission_per_contract_per_leg": execution.commission_per_contract_per_leg,
        },
        "performance": {
            "initial_equity": initial_equity,
            "final_equity": final_equity,
            "net_profit": final_equity - initial_equity,
            "total_return": total_return,
            "cagr": cagr,
            "max_drawdown": _max_drawdown(equities, initial_equity),
            "trade_count": len(trades),
            "win_rate": len(winners) / len(trades) if trades else 0.0,
            "profit_factor": pf,
            "average_trade_pnl": sum(t.net_pnl for t in trades) / len(trades) if trades else 0.0,
            "average_winner": sum(t.net_pnl for t in winners) / len(winners) if winners else 0.0,
            "average_loser": sum(t.net_pnl for t in losses) / len(losses) if losses else 0.0,
            "worst_trade": min((t.net_pnl for t in trades), default=0.0),
            "best_trade": max((t.net_pnl for t in trades), default=0.0),
            "average_holding_days": (
                sum(t.holding_days for t in trades) / len(trades) if trades else 0.0
            ),
            "market_exposure_fraction": exposure,
            "annual_returns": annual_returns,
            "entry_year_net_pnl": entry_year_pnl,
            "exit_reasons": dict(Counter(t.reason for t in trades)),
        },
        "benchmarks": {
            "spy_price_return": last_close / first_close - 1,
            "spy_adjusted_total_return": last_adjusted / first_adjusted - 1,
            "cash_return": 0.0,
        },
        "entry_diagnostics": {
            "entry_attempt_days": entry_attempts,
            "entries": len(trades),
            "top_no_trade_reasons": no_trade_reasons.most_common(10),
        },
        "trades": [
            {
                "opened_on": t.opened_on.isoformat(),
                "closed_on": t.closed_on.isoformat(),
                "expiration": t.expiration.isoformat(),
                "contracts": t.contracts,
                "entry_spot": t.entry_spot,
                "exit_spot": t.exit_spot,
                "entry_credit": t.entry_credit,
                "exit_debit": t.exit_debit,
                "net_pnl": t.net_pnl,
                "reason": t.reason,
                "holding_days": t.holding_days,
                "strikes": list(t.strikes),
            }
            for t in trades
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--start", type=date.fromisoformat, default=date(2020, 1, 2))
    parser.add_argument("--end", type=date.fromisoformat, default=date(2025, 12, 16))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    con = _prepare_database(args.data_dir, args.start, args.end)
    result = _run(con, args.start, args.end)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")

    perf = result["performance"]
    benchmarks = result["benchmarks"]
    print("=== REAL SPY 2020-2025 CONDORPILOT BASELINE ===")
    print(f"Data: {result['dataset']['start']} -> {result['dataset']['end']}")
    print(f"Trading days: {result['dataset']['trading_days']}")
    print(f"Filtered option rows: {result['dataset']['filtered_option_rows']:,}")
    print(f"Trades: {perf['trade_count']}")
    print(f"Final equity: ${perf['final_equity']:,.2f}")
    print(f"Net profit: ${perf['net_profit']:+,.2f}")
    print(f"Total return: {perf['total_return']:+.2%}")
    print(f"CAGR: {perf['cagr']:+.2%}")
    print(f"Max drawdown: {perf['max_drawdown']:.2%}")
    print(f"Win rate: {perf['win_rate']:.2%}")
    pf_text = "inf" if perf["profit_factor"] is None else f"{perf['profit_factor']:.2f}"
    print(f"Profit factor: {pf_text}")
    print(f"Avg trade: ${perf['average_trade_pnl']:+,.2f}")
    print(f"Worst trade: ${perf['worst_trade']:+,.2f}")
    print(f"Exposure: {perf['market_exposure_fraction']:.2%}")
    print(f"SPY price return: {benchmarks['spy_price_return']:+.2%}")
    print(f"SPY adjusted total return: {benchmarks['spy_adjusted_total_return']:+.2%}")
    print(f"Annual returns: {perf['annual_returns']}")
    print(f"Exit reasons: {perf['exit_reasons']}")
    print(f"Top no-trade reasons: {result['entry_diagnostics']['top_no_trade_reasons'][:5]}")
    print(f"RESULT_PATH={args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
