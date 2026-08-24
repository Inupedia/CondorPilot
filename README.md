# CondorPilot

**Systematic Iron Condor strategy engine for backtesting, screening, risk management, and automated options trading.**

CondorPilot turns a discretionary Iron Condor idea into an auditable, mechanical workflow. The strategy core is deliberately broker-agnostic: market-data adapters normalize an option chain, the selector builds a defined-risk condor, and the risk layer decides position size and exits.

> CondorPilot is research software, not financial advice. The current release uses synthetic option chains for demos and tests and does not place live orders.

## Strategy defaults

The first strategy profile implements the rules we want to test before adding broker execution:

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
```

Example with explicit assumptions:

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

The demo builds a deterministic synthetic option chain, selects a condor, and prints its strikes, short deltas, entry credit, defined maximum profit/loss, breakevens, and risk-budgeted contract count.

## Architecture

```text
Market data / broker adapter
          |
          v
   normalized OptionQuote[]
          |
          v
  mechanical strategy selector
   DTE -> delta -> wings -> credit filter
          |
          v
       IronCondor
   payoff / breakevens / max risk
          |
          v
      risk policy
 position size / TP / SL / time exit
```

Current package layout:

```text
src/condorpilot/
├── market.py      # provider boundary + synthetic chain
├── models.py      # option quotes, condor, strategy config
├── pricing.py     # dependency-free Black-Scholes demo helper
├── risk.py        # sizing and exit policy
├── strategy.py    # mechanical strike selection
└── cli.py         # inspection/demo CLI
```

## Development

```bash
ruff check .
pytest
python -m condorpilot demo --spot 100 --iv 0.25 --min-credit-to-width 0
```

GitHub Actions runs linting, tests, and the CLI smoke test on Python 3.11, 3.12, and 3.13.

## Roadmap

The intended implementation order is deliberately conservative:

1. **Strategy core** — deterministic selection, payoff, risk, tests.
2. **Historical data model** — persist underlying and option-chain snapshots with provenance.
3. **Event-driven backtester** — realistic fills, slippage, commissions, assignment/expiration handling, portfolio accounting.
4. **Research layer** — compare DTE, delta, wing width, profit target, stop, VIX/IV regimes, and portfolio sizing.
5. **Paper trading adapter** — broker integration behind the existing provider/execution boundaries.
6. **Live safeguards** — idempotent orders, reconciliation, kill switch, exposure limits, audit trail, and alerting.
7. **Dashboard/API** — strategy status, candidates, positions, P/L attribution, and experiment results.

## Design principles

- **Defined risk first.** Every supported strategy must expose bounded maximum loss before an order can be considered.
- **No hidden broker coupling.** Strategy logic consumes normalized domain objects, not vendor payloads.
- **No fake backtests.** Historical research should use timestamped option-chain data and explicit fill assumptions rather than reconstructing results from the underlying alone.
- **Mechanical rules are configuration.** DTE, delta, wings, entry filters, exits, and risk budgets must remain testable parameters.
- **Paper before live.** Live execution should not be enabled until reconciliation and risk controls are independently testable.
