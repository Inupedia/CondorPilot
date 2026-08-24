# CondorPilot

**Research-preview Iron Condor engine for auditable data validation, walk-forward testing, and strategy evidence.**

> **Status: Research Preview — No-Go for live money.** CondorPilot is not a proven profitable trading system and has no broker execution or paper-trading safety layer. Passing tests or even earning a `research_pass` verdict does **not** authorize live trading.

CondorPilot turns a discretionary Iron Condor idea into an explicit research workflow: synchronized historical option chains → integrity checks → defined-risk strategy construction → realistic execution assumptions → event-driven backtesting → walk-forward/OOS validation → volatility-regime attribution → durable evidence report.

Synthetic histories are deterministic fixtures for demos and tests only. They are not performance evidence.

## Baseline strategy

| Parameter | Default | Integrity rule |
| --- | ---: | --- |
| Target expiration | 45 DTE | must be within ±7 days |
| Short put/call delta | 0.15 absolute | must be within ±0.05 delta |
| Wing width | 5 points | exact strike required |
| Max bid/ask width | 75% of mid | otherwise no trade |
| Profit target | 50% of entry credit | close when reached |
| Stop | 2.0× entry credit close debit | `$1.00` credit stops at `$2.00` debit |
| Time exit | 21 DTE | close before expiration |
| Max account risk | 2% per position | based on defined max loss + fees |
| Minimum credit / width | 10% | otherwise no trade |

Sparse data must result in **no trade**, not a silently different strategy.

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

## Real-data workflow

### 1. Import synchronized ThetaData history

CondorPilot includes a dependency-free adapter for a locally running Theta Terminal v3.

```bash
condorpilot import-thetadata \
  --symbol SPY \
  --start-date 2020-01-02 \
  --end-date 2025-12-31 \
  --output data/spy-thetadata.csv
```

The adapter first discovers quoted expirations with the contracts endpoint, then requests `option/history/greeks/all` separately for each concrete expiration. It does **not** rely on an unsupported `expiration=*` request.

Defaults:

```text
Base URL       http://127.0.0.1:25503/v3
Interval       30m
Window         15:30:00 - 16:00:00 America/New_York
Max DTE        90
Strike range   40 strikes around spot
```

All contracts in one normalized snapshot must come from the **same ThetaData timestamp**. CondorPilot never combines different contracts' individually latest rows into a stitched chain that never existed in the market.

### 2. Dataset diagnostics

A dataset must pass research-quality gates before strict walk-forward validation can run. Diagnostics include:

- weekday/calendar coverage;
- unexpected weekend observations;
- zero bids, wide spreads, and missing IV;
- potential stale snapshots;
- live-contract continuity across adjacent snapshots;
- target-DTE coverage;
- target-delta coverage;
- exact protective-wing coverage;
- executable Iron Condor coverage;
- deterministic SHA-256 dataset fingerprint.

The fingerprint identifies the exact normalized evidence used in an experiment.

### 3. Walk-forward / out-of-sample validation

CondorPilot supports rolling and anchored walk-forward validation:

1. optimize the parameter grid only on the training window;
2. freeze the selected parameters;
3. evaluate them on the immediately following future OOS window;
4. carry OOS equity forward into the next fold;
5. repeat without overlapping OOS folds.

Fold-end entry embargoes prevent newly opened trades from being artificially closed by end-of-data. Tests also verify that changing later future data cannot alter an earlier fold's parameter selection or result.

OOS reporting includes total return, drawdown, win rate, trade count, tested-data fraction, parameter-selection stability, cash benchmark, and price-only Buy & Hold benchmark.

### 4. Evidence run

`condorpilot evidence` is the durable research entry point. It accepts **CSV-backed history only** and writes an auditable JSON result.

```bash
condorpilot evidence \
  --csv data/spy-thetadata.csv \
  --fetch-cboe-vix \
  --output-json evidence/spy-2020-2025.json \
  --dtes 30,45,60 \
  --deltas 0.10,0.15,0.20 \
  --wing-widths 5 \
  --profit-targets 0.50 \
  --stop-multiples 2 \
  --exit-dtes 14,21 \
  --risk-fractions 0.01 \
  --train-size 504 \
  --test-size 63 \
  --step-size 63 \
  --rank-by sortino
```

Every completed evidence report contains:

- dataset fingerprint;
- experiment fingerprint, including strategy grid, execution assumptions, walk-forward configuration, and evidence thresholds;
- OOS folds, trades, return, drawdown, win rate, and tested fraction;
- cash and Buy & Hold benchmarks;
- parameter-selection stability;
- OOS trade P/L grouped by **entry-date volatility regime**;
- one evidence-only verdict.

Verdicts are deliberately limited to:

| Verdict | Meaning |
| --- | --- |
| `insufficient_evidence` | sample size, coverage, regime attribution, or parameter stability is too weak |
| `research_fail` | evidence volume is adequate but configured OOS outcome gates fail |
| `research_pass` | configured research evidence gates pass; **still not permission for live trading** |

The CLI always prints `live_trading=NO-GO`, and the JSON field `live_trading_approved` is permanently `false` in this research layer.

Default evidence gates require at least four OOS folds, 20 OOS trades, 25% tested-snapshot coverage, 80% known regime attribution, 25% parameter-selection stability, non-negative OOS return, max OOS drawdown ≤25%, and at least half of OOS folds profitable. Buy & Hold outperformance is reported but is not required by default because an income strategy can have a different risk objective; it can be required explicitly with `--minimum-excess-vs-buy-hold`.

## Volatility regimes

CondorPilot aligns Cboe VIX and rolling ATM option IV without look-ahead.

| Regime | Trigger |
| --- | --- |
| `low` | VIX < 15 with non-elevated IV, or low IV percentile without VIX |
| `normal` | neither low, high, nor stress |
| `high` | VIX >= 25 or IV percentile >= 75% |
| `stress` | VIX >= 35 or IV percentile >= 90% |
| `unknown` | insufficient VIX and IV data |

```bash
condorpilot regimes \
  --csv data/spy-thetadata.csv \
  --fetch-cboe-vix \
  --iv-lookback 252
```

Research sweeps can gate new entries by regime, but the evidence pipeline does **not** re-optimize parameters separately inside each regime. Regime analysis is an attribution layer applied after OOS trades are generated, which avoids introducing another optimization channel.

## Historical data integrity

Real research deliberately fails when the supplied data cannot support a defensible mark.

Current hard rules include:

- every snapshot has a timezone-aware timestamp and one underlying spot;
- held option legs must exist exactly in subsequent snapshots;
- entry DTE and delta remain inside configured tolerances;
- excessively wide entry quotes are rejected;
- a negative modeled close debit is inconsistent data, never a free close;
- expiration settlement requires a snapshot dated exactly on expiration;
- history that jumps from before expiration to after expiration fails instead of using a later underlying price;
- one normalized vendor snapshot uses one shared quote timestamp.

## Historical CSV schema

Each row is one option quote; rows sharing `observed_at` form one snapshot.

```csv
observed_at,symbol,spot,expiration,strike,option_type,bid,ask,delta,implied_volatility
2025-01-02T16:00:00-05:00,SPY,590.20,2025-02-14,550,put,1.25,1.31,-0.102,0.248
```

Required columns:

`observed_at, symbol, spot, expiration, strike, option_type, bid, ask, delta`

`implied_volatility` remains optional for backward compatibility, although missing IV reduces volatility-regime evidence quality when VIX cannot fill the gap.

## What CondorPilot does not model yet

These limitations remain material and keep the project **No-Go for live money**:

- no partial fills or legging risk;
- no order rejection/replace state machine;
- no broker/network latency model;
- no early assignment model for American-style ETF options;
- no broker reconciliation or restart recovery;
- no account-level portfolio exposure limits;
- no kill switch;
- no validated paper-trading adapter.

## Package layout

```text
src/condorpilot/
├── backtest.py          # event loop, positions, settlement integrity
├── diagnostics.py       # research-grade dataset quality gates + fingerprint
├── evidence.py          # durable OOS evidence verdicts and regime attribution
├── execution.py         # fill assumptions and quote-consistency checks
├── history.py           # timestamped chains + synthetic fixtures
├── importers.py         # normalized CSV import/export
├── market.py            # provider boundary
├── models.py            # quotes, condor, strategy invariants
├── pricing.py           # Black-Scholes demo helper
├── research.py          # parameter grids and metrics
├── risk.py              # sizing and explicit exit semantics
├── strategy.py          # DTE/delta/liquidity-constrained selection
├── validation.py        # rolling/anchored walk-forward OOS validation
├── volatility.py        # VIX / IV regimes
├── vendors/
│   └── thetadata.py     # synchronized ThetaData v3 snapshots
└── cli.py
```

## Development

```bash
ruff check .
pytest
condorpilot evidence --help
```

CI runs linting, unit tests, and strategy/backtest/research/regime/diagnostics/walk-forward/evidence smoke coverage on Python 3.11, 3.12, and 3.13.

## Roadmap / hard gates

1. ✅ Strategy core and defined-risk payoff model.
2. ✅ Event-driven backtester with fees/slippage.
3. ✅ Historical CSV and synchronized ThetaData ingestion.
4. ✅ VIX / IV regime research.
5. ✅ Research-integrity hardening.
6. ✅ Dataset diagnostics and evidence fingerprinting.
7. ✅ Rolling/anchored walk-forward and frozen-parameter OOS validation.
8. ✅ Durable real-data evidence pipeline and research verdicts.
9. **Paper trading:** broker adapter, order state machine, partial fills, reconciliation, restart recovery, account risk and kill switch.
10. Only after paper-trading safety and operational validation: evaluate whether any live deployment is justified.

## Design principles

- **Research evidence before automation.**
- **No silent strategy drift.**
- **No stitched market snapshots.**
- **No fake settlement prices.**
- **No fabricated free fills.**
- **No look-ahead labels or parameter selection.**
- **Research PASS is not live GO.**
- **Paper before live.**

CondorPilot is research software, not financial advice.
