# CondorPilot

**Systematic Iron Condor strategy engine for backtesting, research, risk management, and automated options trading.**

CondorPilot turns a discretionary Iron Condor idea into an auditable mechanical workflow. The strategy core is broker-agnostic: adapters normalize option chains, the selector builds defined-risk condors, the execution model applies explicit fill assumptions, the event-driven backtester manages positions, and the research layer compares parameters and market regimes against the same historical data.

> CondorPilot is research software, not financial advice. Synthetic histories are deterministic fixtures for demos/tests. Serious strategy evaluation should use timestamped historical option-chain data from a reliable vendor.

## Strategy defaults

| Parameter | Default |
| --- | ---: |
| Target expiration | 45 DTE |
| Short put delta | ~0.15 |
| Short call delta | ~0.15 |
| Wing width | 5 points |
| Profit target | 50% of entry credit |
| Stop loss | 2x entry-credit loss |
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
condorpilot regimes --days 90 --iv 0.25
```

## ThetaData v3 historical adapter

CondorPilot v0.4 includes a concrete adapter for a locally running [ThetaData v3](https://docs.thetadata.us/) terminal. The adapter uses the `option/history/greeks/all` endpoint because one normalized row contains the option bid/ask, delta, implied volatility, timestamp, and underlying price required by the research engine.

Start Theta Terminal v3, then import daily snapshots:

```bash
condorpilot import-thetadata \
  --symbol SPY \
  --start-date 2025-01-02 \
  --end-date 2025-03-31 \
  --output data/spy-thetadata.csv
```

Defaults:

```text
Base URL       http://127.0.0.1:25503/v3
Endpoint       option/history/greeks/all
Interval       30m
Window         15:30:00 - 16:00:00 America/New_York
Max DTE        90
Strike range   40 strikes around spot
Expiration     * (bulk expirations)
```

The ThetaData Greeks endpoint requires the appropriate ThetaData subscription/data entitlement. CondorPilot deliberately issues one market-date request at a time for bulk expirations rather than relying on multi-day bulk behavior.

For each day, the adapter:

1. keeps the latest usable row for every contract;
2. normalizes call/put, expiration, strike, bid/ask, delta, and IV;
3. computes a robust snapshot spot from ThetaData's underlying prices;
4. rejects a snapshot when underlying-price dispersion exceeds the configured tolerance;
5. skips market dates with no usable data but does not hide connection or schema errors.

The HTTP transport is injectable, so CI and unit tests never require a live Theta Terminal.

## Volatility regimes

CondorPilot can classify every option snapshot using two independent signals:

- **Cboe VIX close** from Cboe's official daily history;
- **ATM option IV** estimated from the call and put nearest 0.50 absolute delta around the expiration closest to 30 DTE.

The rolling IV percentile and IV rank use only the current observation and preceding observations. They do not use future data.

Default regimes:

| Regime | Default trigger |
| --- | --- |
| `low` | VIX < 15 with non-elevated IV, or low IV percentile when VIX is unavailable |
| `normal` | neither low, high, nor stress conditions |
| `high` | VIX >= 25 or IV percentile >= 75% |
| `stress` | VIX >= 35 or IV percentile >= 90% |
| `unknown` | neither VIX nor option IV is available |

Inspect regimes using only IV already present in the option history:

```bash
condorpilot regimes \
  --csv data/spy-thetadata.csv \
  --iv-lookback 252 \
  --tail 30
```

Add Cboe VIX history from a local file:

```bash
condorpilot regimes \
  --csv data/spy-thetadata.csv \
  --vix-csv data/VIX_History.csv
```

Or fetch the official public VIX history directly:

```bash
condorpilot regimes \
  --csv data/spy-thetadata.csv \
  --fetch-cboe-vix
```

Cboe source: `https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv`

## Regime-aware research

Regime filtering is an **entry filter**, not a data filter. A position opened on an allowed day continues to be marked and managed on every subsequent snapshot even when the current regime would reject a new entry.

Example: compare parameter combinations while opening new positions only in normal and high-volatility regimes:

```bash
condorpilot research \
  --csv data/spy-thetadata.csv \
  --fetch-cboe-vix \
  --allowed-regimes normal,high \
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

This is the path for testing questions such as:

- Does 45 DTE / 15 delta perform better when IV is elevated?
- Should low-volatility entries be skipped because premium is too small?
- Does stress-regime premium compensate for the larger tail risk?
- Does the best parameter set remain robust when slippage and commissions are included?

## Research engine

Every parameter case is evaluated against the same immutable option history and execution assumptions. The research engine reports:

- total return and CAGR;
- maximum drawdown;
- Sharpe and Sortino ratios;
- win rate and profit factor;
- average and worst trade;
- capital exposure;
- trade count.

The default ranking metric is Sortino so a high win rate does not automatically outrank a strategy with better downside-adjusted returns.

## Historical CSV schema

Real research uses one row per option quote. Rows sharing the same `observed_at` timestamp form one complete option-chain snapshot.

```csv
observed_at,symbol,spot,expiration,strike,option_type,bid,ask,delta,implied_volatility
2025-01-02T16:00:00-05:00,SPY,590.20,2025-02-14,550,put,1.25,1.31,-0.102,0.248
2025-01-02T16:00:00-05:00,SPY,590.20,2025-02-14,555,put,1.55,1.62,-0.121,0.241
```

Required columns remain:

`observed_at, symbol, spot, expiration, strike, option_type, bid, ask, delta`

`implied_volatility` is optional for backward compatibility with v0.3 data. It is required only for IV-based regime analysis when VIX is not otherwise sufficient.

`observed_at` must include a timezone. The importer rejects duplicate contracts and inconsistent symbol/spot values within a snapshot. Held option legs must exist exactly in subsequent snapshots; CondorPilot does not silently interpolate missing history.

## Backtesting model

CondorPilot does not infer historical option prices from SPY or QQQ candles.

```text
Vendor / normalized CSV
          |
          v
 OptionChainSnapshot[]
          |
          +--------------------+
          |                    |
          v                    v
 volatility regime       parameter grid
 VIX + IV pct/rank        DTE/delta/etc.
          |                    |
          v                    |
     entry filter              |
          |                    |
          +---------+----------+
                    v
             strategy selector
                    |
                    v
                IronCondor
                    |
             execution + risk
                    |
                    v
          event-driven backtester
                    |
                    v
         trades + equity + metrics
```

Important assumptions are explicit and configurable:

- **Timestamped chains.** Every snapshot has a timezone-aware timestamp, spot, and normalized option quotes.
- **Strict held-leg matching.** Missing held legs cause a data error instead of a fabricated quote.
- **Slippage.** `0.0` means midpoint fills; `1.0` means natural bid/ask fills. Default is `0.25`.
- **Commissions.** Default is `$0.65` per option contract per leg on both entry and exit.
- **Risk sizing.** Contracts are sized from modeled credit, maximum loss, fees, equity, and risk fraction.
- **Mark-to-liquidation equity.** Open equity includes modeled close cost and estimated exit commissions.
- **No orphan positions.** A still-open position at the final snapshot is liquidated with `end_of_data`.
- **Entry filters do not remove market data.** Regime/event gates are checked only before a new position opens.

## Package layout

```text
src/condorpilot/
├── backtest.py          # event loop, positions, entry filters, equity curve
├── execution.py         # fill model, slippage, commissions
├── history.py           # timestamped chains + synthetic history
├── importers.py         # normalized CSV import/export
├── market.py            # provider boundary + single-chain synthetic demo
├── models.py            # option quotes, IV, condor, strategy config
├── pricing.py           # dependency-free Black-Scholes demo helper
├── research.py          # parameter grids, metrics, sweep/ranking engine
├── risk.py              # sizing and exit policy
├── strategy.py          # mechanical strike selection
├── volatility.py        # VIX, ATM IV, IV percentile/rank, regimes
├── vendors/
│   └── thetadata.py     # ThetaData v3 historical options adapter
└── cli.py               # demo, backtest, research, regimes, imports
```

## Python API

```python
from condorpilot import (
    BacktestConfig,
    ParameterGrid,
    VolatilityRegime,
    build_regime_entry_filter,
    build_volatility_regimes,
    rank_runs,
    run_parameter_sweep,
)

regimes = build_volatility_regimes(historical_snapshots, vix_history=vix_history)
entry_filter = build_regime_entry_filter(
    regimes,
    allowed_regimes=(VolatilityRegime.NORMAL, VolatilityRegime.HIGH),
)

runs = run_parameter_sweep(
    historical_snapshots,
    grid=ParameterGrid(
        target_dte=(30, 45, 60),
        short_delta=(0.10, 0.15, 0.20),
        profit_target_fraction=(0.25, 0.50, 0.75),
        exit_dte=(14, 21),
    ),
    base_config=BacktestConfig(initial_equity=50_000),
    entry_filter=entry_filter,
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
  --exit-dtes 14 --min-credit-to-width 0 --allowed-regimes normal --top 3
condorpilot regimes --spot 100 --days 10 --iv 0.25 --tail 3
```

GitHub Actions runs linting, unit tests, strategy/backtest smoke tests, a regime-aware parameter sweep, and a volatility-regime smoke test on Python 3.11, 3.12, and 3.13.

## Roadmap

1. ✅ **Strategy core** — deterministic selection, payoff, risk, tests.
2. ✅ **Historical data contract** — timestamped option-chain snapshots and strict validation.
3. ✅ **Event-driven backtester** — fills, slippage, commissions, exits, portfolio accounting.
4. ✅ **Historical data import** — normalized CSV schema with strict validation and round-trip export.
5. ✅ **Research layer** — parameter sweeps and risk-adjusted metrics/ranking.
6. ✅ **ThetaData adapter** — concrete v3 historical option-chain ingestion.
7. ✅ **Volatility regimes** — Cboe VIX + ATM IV percentile/rank and regime-aware entries.
8. **Dataset diagnostics** — coverage, missing contracts, stale quotes, spread/liquidity reports.
9. **Paper trading adapter** — broker integration behind provider/execution boundaries.
10. **Live safeguards** — reconciliation, idempotent orders, kill switch, exposure limits, audit trail.
11. **Dashboard/API** — candidates, positions, P/L attribution, experiments, and live status.

## Design principles

- **Defined risk first.** Every supported strategy exposes bounded maximum loss before an order is considered.
- **Same data for every experiment.** Parameter comparisons reuse one validated history and one execution model.
- **No hidden broker coupling.** Strategy logic consumes normalized domain objects, not vendor payloads.
- **No fake backtests.** Real research uses timestamped option-chain data and explicit fill assumptions.
- **No look-ahead regime labels.** Rolling IV features use only observations available up to that snapshot.
- **Fail on missing held-leg data.** Silent interpolation can materially bias options results.
- **Paper before live.** Live execution waits for reconciliation and independently testable risk controls.
