# CondorPilot

**Systematic Iron Condor strategy engine for backtesting, screening, risk management, and automated options trading.**

CondorPilot turns a discretionary Iron Condor idea into an auditable, mechanical workflow. The strategy core is broker-agnostic: market-data adapters normalize option chains, the selector builds defined-risk condors, the execution model applies explicit fill assumptions, and the backtester manages the full position lifecycle.

> CondorPilot is research software, not financial advice. Synthetic option histories are included for deterministic demos and tests. Serious strategy evaluation should use timestamped historical option-chain data from a reliable vendor.

## Strategy defaults

| Parameter | Default |
| --- | ---: |
| Target expiration | 45 DTE |
| Short put delta | ~0.15 |
| Short call delta | ~0.15 |
| Wing width | 5 points |
| Profit target | 50% of entry credit |
| Stop loss | 2x entry credit loss |
| Time exit | 21 DTE |
| Max account risk | 2% per position |
| Minimum credit / width | 10% |

The selector chooses the expiration nearest the target DTE, picks OTM short strikes nearest the configured absolute delta, adds exact-width protective wings, and rejects trades that do not meet the credit-to-width threshold.

## Quick start

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'

condorpilot demo
condorpilot backtest-demo
```

Inspect a single candidate:

```bash
condorpilot demo \
  --symbol SPY \
  --spot 650 \
  --dte 45 \
  --iv 0.22 \
  --delta 0.15 \
  --wing-width 5 \
  --account-equity 50000
```

Run the event-driven engine against deterministic synthetic history:

```bash
condorpilot backtest-demo \
  --symbol SPY \
  --spot 650 \
  --days 90 \
  --dte 45 \
  --exit-dte 21 \
  --delta 0.15 \
  --wing-width 5 \
  --profit-target 0.50 \
  --stop-multiple 2 \
  --slippage 0.25 \
  --commission 0.65
```

The backtest demo reports final equity, total return, trade count, win rate, maximum drawdown, and each trade's realized P/L and exit reason.

## Backtesting model

CondorPilot does not infer historical option prices from SPY or QQQ candles. A real historical run consumes timestamped option-chain snapshots:

```text
OptionChainSnapshot(t0)
        |
        v
45 DTE / 15 delta selector
        |
        v
modeled entry fill + commissions
        |
        v
risk-budgeted Iron Condor position
        |
        +-----------------------------+
        |                             |
        v                             |
OptionChainSnapshot(t1..n)            |
        |                             |
exact held-contract lookup            |
        |                             |
modeled close debit                   |
        |                             |
TP / SL / 21 DTE / expiration         |
        |                             |
        v                             |
TradeRecord + EquityPoint <-----------+
```

Important backtest assumptions are explicit and configurable:

- **Timestamped chains.** Each snapshot carries an aware timestamp, underlying spot, and normalized option quotes.
- **Strict contract matching.** If a held leg is missing from a historical snapshot, the engine raises a data error instead of interpolating a fake quote.
- **Slippage.** `0.0` means midpoint fills; `1.0` means natural bid/ask fills. The default is `0.25`, one quarter of the distance from mid toward natural.
- **Commissions.** Default is `$0.65` per option contract per leg on both entry and exit.
- **Risk sizing.** Contracts are sized from modeled entry credit, defined maximum loss, estimated round-trip commissions, account equity, and the configured risk fraction.
- **Mark-to-liquidation equity.** Open-position equity includes modeled close cost and estimated exit commissions.
- **No orphan positions.** A still-open position at the final snapshot is liquidated with `end_of_data` as the exit reason.

## Architecture

```text
Broker / historical data adapter
              |
              v
      normalized OptionQuote[]
              |
              v
       strategy selector
 DTE -> delta -> wings -> credit
              |
              v
          IronCondor
              |
      +-------+--------+
      |                |
      v                v
 risk policy      execution model
 sizing/exits     fills/slippage/fees
      |                |
      +-------+--------+
              |
              v
      event-driven backtester
              |
              v
     trades + equity curve
```

Current package layout:

```text
src/condorpilot/
├── backtest.py    # event loop, positions, trades, equity curve, metrics
├── execution.py   # entry/exit fill model, slippage, commissions
├── history.py     # timestamped chains + deterministic synthetic history
├── market.py      # provider boundary + single-chain synthetic demo
├── models.py      # option quotes, condor, strategy config
├── pricing.py     # dependency-free Black-Scholes demo helper
├── risk.py        # sizing and exit policy
├── strategy.py    # mechanical strike selection
└── cli.py         # candidate and backtest demos
```

## Python API

```python
from condorpilot import BacktestConfig, ExecutionConfig, StrategyConfig, run_backtest

strategy = StrategyConfig(
    target_dte=45,
    short_delta=0.15,
    wing_width=5,
    profit_target_fraction=0.50,
    stop_loss_credit_multiple=2,
    exit_dte=21,
)

config = BacktestConfig(
    initial_equity=50_000,
    strategy=strategy,
    execution=ExecutionConfig(
        slippage_fraction=0.25,
        commission_per_contract_per_leg=0.65,
    ),
)

result = run_backtest(historical_snapshots, config=config)
print(result.total_return, result.max_drawdown, result.win_rate)
```

## Development

```bash
ruff check .
pytest
condorpilot demo --spot 100 --iv 0.25 --min-credit-to-width 0
condorpilot backtest-demo --spot 100 --days 30 --iv 0.25 --min-credit-to-width 0
```

GitHub Actions runs linting, unit tests, the strategy CLI smoke test, and the backtester smoke test on Python 3.11, 3.12, and 3.13.

## Roadmap

1. ✅ **Strategy core** — deterministic selection, payoff, risk, tests.
2. ✅ **Historical data contract** — timestamped option-chain snapshots and strict validation.
3. ✅ **Event-driven backtester** — fills, slippage, commissions, exits, portfolio accounting.
4. **Historical data adapters** — import vendor datasets with provenance and normalization.
5. **Research layer** — parameter sweeps across DTE, delta, wing width, exits, volatility regimes, and sizing.
6. **Paper trading adapter** — broker integration behind provider/execution boundaries.
7. **Live safeguards** — idempotent orders, reconciliation, kill switch, exposure limits, audit trail, alerts.
8. **Dashboard/API** — candidates, positions, P/L attribution, experiments, and live status.

## Design principles

- **Defined risk first.** Every supported strategy must expose bounded maximum loss before an order can be considered.
- **No hidden broker coupling.** Strategy logic consumes normalized domain objects, not vendor payloads.
- **No fake backtests.** Real research uses timestamped option-chain data and explicit fill assumptions.
- **Fail on missing held-leg data.** Silent interpolation can materially bias options results.
- **Mechanical rules are configuration.** DTE, delta, wings, entry filters, exits, and risk budgets remain testable parameters.
- **Paper before live.** Live execution should not be enabled until reconciliation and risk controls are independently testable.
