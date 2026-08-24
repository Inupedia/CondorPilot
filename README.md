# CondorPilot

**Research-preview Iron Condor engine for auditable backtesting, data validation, and strategy evidence.**

> **Status: Research Preview — No-Go for live money.** CondorPilot is not a proven profitable trading system and currently has no broker execution or paper-trading safety layer. Passing tests means the software follows its rules; it does not prove the strategy has statistical edge.

CondorPilot turns a discretionary Iron Condor idea into an explicit workflow: historical option data is normalized into synchronized snapshots, strategy rules select defined-risk spreads, execution assumptions model fills and fees, an event-driven backtester manages positions, dataset diagnostics gate bad evidence, walk-forward validation freezes parameters before future tests, and the evidence layer produces traceable research reports.

Synthetic histories are deterministic fixtures for demos and tests only. They are not performance evidence. **This repository currently contains no committed multi-year real SPY evidence result.**

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

Sparse data must result in **no trade**, not a silently different strategy. A 45-DTE experiment is not allowed to become a 100-DTE trade, and a 15-delta experiment is not allowed to become a 45-delta trade simply because the chain is incomplete.

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

## Historical data integrity

Real research uses timestamped option-chain snapshots. CondorPilot deliberately fails when the supplied data cannot support a defensible mark.

Current rules:

- every snapshot has a timezone-aware timestamp and one underlying spot;
- held option legs must exist exactly in subsequent snapshots;
- entry DTE and delta must remain inside configured tolerances;
- excessively wide entry quotes are rejected;
- a negative modeled close debit is treated as inconsistent data, never clamped into a free close;
- expiration settlement requires a snapshot dated exactly on the option expiration date;
- if history jumps from before expiration to after expiration, the backtest fails instead of using a later SPY/QQQ price as the settlement price.

## ThetaData v3 adapter

CondorPilot includes a dependency-free adapter for a locally running Theta Terminal v3. It normalizes bid/ask, delta, implied volatility, expiration, strike, timestamp, and underlying price.

```bash
condorpilot import-thetadata \
  --symbol SPY \
  --start-date 2020-01-02 \
  --end-date 2025-12-31 \
  --output data/spy-thetadata.csv
```

Defaults:

```text
Base URL       http://127.0.0.1:25503/v3
Interval       30m
Window         15:30:00 - 16:00:00 America/New_York
Max DTE        90
Strike range   40 strikes around spot
```

The adapter first discovers quoted contracts/expirations for the market date, then requests `option/history/greeks/all` with each concrete expiration. It does **not** rely on an unsupported `expiration=*` shortcut.

### No stitched chains

All contracts in one normalized snapshot must come from the **same ThetaData timestamp**. CondorPilot selects one common usable timestamp for the chain. It never takes each contract's individual latest row and combines 15:30 and 16:00 quotes into a synthetic chain that never existed in the market.

The adapter also checks dispersion in ThetaData's underlying price across rows at that timestamp. Market dates with no usable synchronized data can be skipped; connectivity and schema failures are not hidden.

## Dataset diagnostics

Before evidence is accepted, `diagnose_dataset()` measures whether the normalized history can faithfully support the configured strategy:

- weekday/calendar coverage and unexpected weekend snapshots;
- zero bids, wide quotes, missing IV, and potentially stale snapshots;
- continuity of still-live contracts from one snapshot to the next;
- target-DTE coverage;
- target-delta coverage;
- exact protective-wing coverage;
- complete executable-condor coverage;
- a deterministic SHA-256 fingerprint over normalized research inputs.

A dataset can be syntactically valid yet still receive `research_grade=FAIL`. Downstream evidence must not repair bad history by interpolation or by silently changing strategy identity.

## Volatility regimes

CondorPilot can classify snapshots using Cboe VIX and ATM option IV.

| Regime | Trigger |
| --- | --- |
| `low` | VIX < 15 with non-elevated IV, or low IV percentile without VIX |
| `normal` | neither low, high, nor stress |
| `high` | VIX >= 25 or IV percentile >= 75% |
| `stress` | VIX >= 35 or IV percentile >= 90% |
| `unknown` | insufficient VIX and IV data |

Rolling IV percentile/rank uses only information available at or before the snapshot; it does not use future observations.

```bash
condorpilot regimes \
  --csv data/spy-thetadata.csv \
  --fetch-cboe-vix \
  --iv-lookback 252
```

Regimes can gate **new entries** without removing marks for existing positions:

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
  --rank-by sortino
```

`--stop-multiples` means **modeled close debit divided by original entry credit**. For example, `2` means a position sold for `$1.20` stops at approximately `$2.40`, before commissions and slippage effects.

## Walk-forward / out-of-sample validation

Walk-forward validation separates parameter selection from future evaluation:

1. evaluate candidates only inside the training window;
2. select the training winner by the configured metric;
3. freeze those parameters;
4. run them on the immediately following, non-overlapping OOS window;
5. carry ending OOS equity into the next OOS fold;
6. compare OOS results with Cash and a price-only Buy & Hold benchmark.

Rolling fixed-size and anchored expanding training windows are supported. An entry embargo near each fold boundary prevents positions from being opened when they cannot reach the configured time exit before the fold ends. Candidates or OOS folds contaminated by forced `end_of_data` liquidation are rejected.

Regression tests deliberately mutate later future data and verify that earlier fold selection/results do not change.

## Real-data evidence gate

`condorpilot evidence` is intentionally different from `research`: **`--csv` is required and there is no synthetic fallback.** It writes a compact machine-readable JSON report and a human-readable Markdown report.

```bash
condorpilot evidence \
  --csv data/spy-thetadata.csv \
  --symbol SPY \
  --fetch-cboe-vix \
  --output-dir evidence/spy-2020-2025 \
  --train-size 504 \
  --test-size 126 \
  --step-size 126 \
  --dtes 30,45,60 \
  --deltas 0.10,0.15,0.20 \
  --wing-widths 5 \
  --profit-targets 0.50 \
  --stop-multiples 2 \
  --exit-dtes 14,21 \
  --risk-fractions 0.01
```

Outputs:

```text
evidence/spy-2020-2025/
├── evidence.json
└── evidence.md
```

The report records:

- dataset fingerprint, period, coverage, continuity, and quality issues;
- every OOS fold and its frozen selected parameters;
- OOS return, drawdown, win rate, trade count, and profit factor;
- tested-history fraction and parameter-selection stability;
- Cash and Buy & Hold benchmarks;
- OOS trade performance grouped by entry volatility regime.

The research gate has four outcomes:

| Verdict | Meaning |
| --- | --- |
| `data_fail` | dataset quality is not adequate for the experiment |
| `insufficient_evidence` | valid data exists, but OOS folds/trades/coverage are insufficient |
| `research_reject` | adequate OOS evidence exists but fails configured performance/risk gates |
| `research_pass` | configured research-only evidence gates pass |

**`research_pass` never means live-ready.** Every evidence report hard-codes `live_ready=false`. Paper trading, order lifecycle safety, reconciliation, assignment handling, and account-level risk remain separate gates.

Default research-only evidence thresholds require at least 4 OOS folds, 20 OOS trades, 20% of source snapshots tested OOS, at least half of folds positive, profit factor >= 1.0, max OOS drawdown <= 25%, and positive OOS total return. These are explicit heuristic gates, not claims of statistical significance or future profitability.

## Research metrics

Every parameter case reuses the same validated history and execution assumptions. Results include:

- total return and CAGR;
- maximum drawdown;
- Sharpe and Sortino ratios;
- win rate and profit factor;
- average and worst trade;
- capital exposure;
- trade count.

A high win rate is not treated as proof of safety. Iron Condor is a short-volatility strategy: many small wins can still be dominated by infrequent large losses.

## Historical CSV schema

Each row is one option quote; rows sharing `observed_at` form one snapshot.

```csv
observed_at,symbol,spot,expiration,strike,option_type,bid,ask,delta,implied_volatility
2025-01-02T16:00:00-05:00,SPY,590.20,2025-02-14,550,put,1.25,1.31,-0.102,0.248
```

Required columns:

`observed_at, symbol, spot, expiration, strike, option_type, bid, ask, delta`

`implied_volatility` is optional for backward compatibility.

## What CondorPilot does not model yet

These limitations are material and are why the project remains **No-Go for live money**:

- no committed multi-year real SPY evidence result yet;
- no partial fills or legging risk;
- no order rejection/replace state machine;
- no network or broker latency model;
- no early assignment model for American-style ETF options;
- no broker reconciliation or restart recovery;
- no account-level portfolio exposure limits;
- no kill switch;
- no validated paper-trading adapter.

## Package layout

```text
src/condorpilot/
├── backtest.py          # event loop, positions, settlement integrity
├── diagnostics.py       # data-quality and strategy-availability gates
├── evidence.py          # real-data OOS evidence verdicts and reports
├── execution.py         # fill assumptions and quote-consistency checks
├── history.py           # timestamped chains + synthetic fixtures
├── importers.py         # normalized CSV import/export
├── market.py            # provider boundary
├── models.py            # quotes, condor, strategy invariants
├── pricing.py           # Black-Scholes demo helper
├── research.py          # parameter grids and metrics
├── risk.py              # sizing and explicit exit semantics
├── strategy.py          # DTE/delta/liquidity-constrained selection
├── validation.py        # rolling/anchored walk-forward OOS
├── volatility.py        # VIX / IV regimes
├── vendors/
│   └── thetadata.py     # synchronized ThetaData v3 snapshots
└── cli.py
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

CI runs linting, unit tests, and smoke tests on Python 3.11, 3.12, and 3.13. The evidence CLI smoke test generates a temporary CSV fixture only to validate software behavior; its output is not retained or presented as market evidence.

## Roadmap / hard gates

1. ✅ Strategy core and defined-risk payoff model.
2. ✅ Event-driven backtester with fees/slippage.
3. ✅ Historical CSV and synchronized ThetaData ingestion.
4. ✅ VIX / IV regime research.
5. ✅ Research-integrity hardening: strict DTE/delta identity, synchronized vendor snapshots, explicit stop semantics, exact expiration settlement, inconsistent-quote failure.
6. ✅ Dataset diagnostics: stale quotes, liquidity/spreads, missing contracts, calendar/expiration coverage, fingerprinted provenance.
7. ✅ Walk-forward / out-of-sample: frozen parameters, rolling/anchored train-test windows, leakage regression tests, benchmark comparisons.
8. ✅ Real-data evidence pipeline and research-only verdict/report schema. **Real multi-year SPY evidence still needs to be supplied and run.**
9. **Paper trading:** broker adapter, order state machine, partial fills, reconciliation, restart recovery, account risk and kill switch.
10. Only after those gates: evaluate whether any live deployment is justified.

## Design principles

- **Research evidence before automation.**
- **No silent strategy drift.**
- **No stitched market snapshots.**
- **No fake settlement prices.**
- **No fabricated free fills.**
- **No look-ahead regime labels.**
- **No synthetic performance claims.**
- **Paper before live.**

CondorPilot is research software, not financial advice.
