# CondorPilot

**Systematic Iron Condor strategy engine for backtesting, screening, risk management, and automated options trading.**

CondorPilot turns a discretionary Iron Condor idea into an auditable, mechanical workflow. The strategy core is broker-agnostic: market-data adapters normalize option chains, the selector builds defined-risk condors, the execution model applies explicit fill assumptions, the backtester manages the position lifecycle, and the research layer compares strategy parameters against the same historical dataset.

> CondorPilot is research software, not financial advice. Synthetic histories are deterministic fixtures for demos/tests. Serious strategy evaluation should use timestamped historical option-chain data from a reliable vendor.

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

## Quick start

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'

condorpilot demo
condorpilot backtest-demo
condorpilot research --days 90 --dtes 30,45,60 --deltas 0.10,0.15,0.20
```

## Research engine

CondorPilot can sweep the same historical option dataset across combinations of DTE, short delta, wing width, profit target, stop multiple, time exit, and account-risk fraction.

```bash
condorpilot research \
  --csv data/spy-options.csv \
  --dtes 30,45,60 \
  --deltas 0.10,0.15,0.20 \
  --wing-widths 5,10 \
  --profit-targets 0.25,0.50,0.75 \
  --stop-multiples 1,2,3 \
  --exit-dtes 7,14,21 \
  --risk-fractions 0.005,0.01,0.02 \
  --rank-by sortino \
  --top 20
```

Each run reports metrics including:

- total return and CAGR
- maximum drawdown
- Sharpe and Sortino ratios
- win rate and profit factor
- average and worst trade
- capital exposure
- trade count

The default ranking metric is Sortino so a high win rate does not automatically outrank a strategy with better downside-adjusted returns.

## Historical CSV schema

Real research uses one row per option quote. Rows sharing the same `observed_at` timestamp form one complete option-chain snapshot.

```csv
observed_at,symbol,spot,expiration,strike,option_type,bid,ask,delta
2025-01-02T21:00:00+00:00,SPY,590.20,2025-02-14,550,put,1.25,1.31,-0.102
2025-01-02T21:00:00+00:00,SPY,590.20,2025-02-14,555,put,1.55,1.62,-0.121
```

Required columns are:

`observed_at, symbol, spot, expiration, strike, option_type, bid, ask, delta`

`observed_at` must include a timezone. The importer rejects duplicate contracts and inconsistent symbol/spot values within a snapshot. Held option legs must exist exactly in subsequent snapshots; CondorPilot does not silently interpolate missing history.

Python API:

```python
from condorpilot import load_option_chain_csv, save_option_chain_csv

history = load_option_chain_csv("data/spy-options.csv")
save_option_chain_csv(history, "data/normalized.csv")
```

## Backtesting model

CondorPilot does not infer historical option prices from SPY or QQQ candles. A real run consumes timestamped option-chain snapshots:

```text
OptionChainSnapshot(t0)
        |
        v
DTE / delta / wings / credit filter
        |
        v
modeled entry fill + commissions
        |
        v
risk-budgeted Iron Condor
        |
        +------------------------------+
        |                              |
        v                              |
OptionChainSnapshot(t1..n)             |
        |                              |
exact held-contract lookup             |
        |                              |
modeled close debit                    |
        |                              |
TP / SL / DTE / expiration             |
        |                              |
        v                              |
TradeRecord + EquityPoint <------------+
```

Important assumptions are explicit and configurable:

- **Timestamped chains.** Each snapshot carries a timezone-aware timestamp, underlying spot, and normalized option quotes.
- **Strict contract matching.** Missing held legs cause a data error instead of a fabricated quote.
- **Slippage.** `0.0` means midpoint fills; `1.0` means natural bid/ask fills. Default is `0.25`.
- **Commissions.** Default is `$0.65` per option contract per leg on both entry and exit.
- **Risk sizing.** Contracts are sized from modeled entry credit, defined maximum loss, estimated round-trip commissions, equity, and configured risk fraction.
- **Mark-to-liquidation equity.** Open-position equity includes modeled close cost and estimated exit commissions.
- **No orphan positions.** A still-open position at the final snapshot is liquidated with `end_of_data` as the exit reason.

## Architecture

```text
Vendor CSV / broker adapter / synthetic fixture
                    |
                    v
          OptionChainSnapshot[]
                    |
          +---------+----------+
          |                    |
          v                    v
   strategy selector      research grid
 DTE/delta/wings/etc.    parameter expansion
          |                    |
          v                    |
       IronCondor               |
          |                    |
     execution + risk           |
          |                    |
          v                    |
 event-driven backtester <------+
          |
          v
 trades + equity + research metrics
```

Current package layout:

```text
src/condorpilot/
├── backtest.py    # event loop, positions, trades, equity curve
├── execution.py   # entry/exit fill model, slippage, commissions
├── history.py     # timestamped chains + deterministic synthetic history
├── importers.py   # normalized long-form CSV import/export
├── market.py      # provider boundary + single-chain synthetic demo
├── models.py      # option quotes, condor, strategy config
├── pricing.py     # dependency-free Black-Scholes demo helper
├── research.py    # parameter grids, metrics, sweep/ranking engine
├── risk.py        # sizing and exit policy
├── strategy.py    # mechanical strike selection
└── cli.py         # candidate, backtest, and research commands
```

## Python research API

```python
from condorpilot import BacktestConfig, ParameterGrid, rank_runs, run_parameter_sweep

runs = run_parameter_sweep(
    historical_snapshots,
    grid=ParameterGrid(
        target_dte=(30, 45, 60),
        short_delta=(0.10, 0.15, 0.20),
        profit_target_fraction=(0.25, 0.50, 0.75),
        exit_dte=(14, 21),
    ),
    base_config=BacktestConfig(initial_equity=50_000),
)

for run in rank_runs(runs, metric="sortino")[:10]:
    print(run.parameters, run.metrics)
```

## Development

```bash
ruff check .
pytest
condorpilot demo --spot 100 --iv 0.25 --min-credit-to-width 0
condorpilot backtest-demo --spot 100 --days 30 --iv 0.25 --min-credit-to-width 0
condorpilot research --spot 100 --days 30 --dtes 30,45 --deltas 0.10,0.15 \
  --exit-dtes 14 --min-credit-to-width 0 --top 3
```

GitHub Actions runs linting, unit tests, strategy/backtest smoke tests, and a parameter-sweep smoke test on Python 3.11, 3.12, and 3.13.

## Roadmap

1. ✅ **Strategy core** — deterministic selection, payoff, risk, tests.
2. ✅ **Historical data contract** — timestamped option-chain snapshots and strict validation.
3. ✅ **Event-driven backtester** — fills, slippage, commissions, exits, portfolio accounting.
4. ✅ **Historical data import** — normalized CSV schema with strict validation and round-trip export.
5. ✅ **Research layer** — parameter sweeps and risk-adjusted metrics/ranking.
6. **Vendor adapters** — normalize concrete historical options datasets into the CSV/domain contract.
7. **Volatility regimes** — VIX/IV percentile tagging and regime-aware research.
8. **Paper trading adapter** — broker integration behind provider/execution boundaries.
9. **Live safeguards** — reconciliation, idempotent orders, kill switch, exposure limits, audit trail.
10. **Dashboard/API** — candidates, positions, P/L attribution, experiments, and live status.

## Design principles

- **Defined risk first.** Every supported strategy exposes bounded maximum loss before an order is considered.
- **Same data for every experiment.** Parameter comparisons reuse one validated history and one execution model.
- **No hidden broker coupling.** Strategy logic consumes normalized domain objects, not vendor payloads.
- **No fake backtests.** Real research uses timestamped option-chain data and explicit fill assumptions.
- **Fail on missing held-leg data.** Silent interpolation can materially bias options results.
- **Mechanical rules are configuration.** DTE, delta, wings, entry filters, exits, and risk budgets remain testable parameters.
- **Paper before live.** Live execution waits for reconciliation and independently testable risk controls.
