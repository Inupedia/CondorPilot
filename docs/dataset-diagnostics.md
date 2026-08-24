# Dataset diagnostics

CondorPilot treats historical option data as evidence, not as an implementation detail. A backtest is only as credible as the market snapshots that feed it.

`diagnose_dataset()` evaluates normalized history at four levels:

1. **Calendar coverage** — weekday coverage and unexpected weekend snapshots.
2. **Quote quality** — zero bids, wide bid/ask spreads, missing implied volatility, and snapshots whose overlapping quotes are suspiciously unchanged.
3. **Contract continuity** — whether contracts that could still be held remain present in the next snapshot instead of disappearing from the data window.
4. **Strategy availability** — the fraction of snapshots that can faithfully represent the configured strategy through four gates: target DTE, target short delta, exact protective wings, and a fully executable Iron Condor.

The default strategy-quality gate is aligned with the research-preview configuration: 45 DTE with a +/-7 day tolerance, 0.15 short delta with a +/-0.05 tolerance, 5-point wings, and the configured maximum bid/ask spread.

Every report also includes a deterministic SHA-256 `fingerprint` over timestamps, spot prices, contracts, bid/ask, delta, and IV. Store this fingerprint with research output so a result can be traced back to the exact normalized dataset used to produce it.

## Example

```python
from condorpilot import diagnose_dataset, load_option_chain_csv

history = load_option_chain_csv("data/spy-thetadata.csv")
report = diagnose_dataset(history)

print(report.research_grade)
print(report.fingerprint)
print(report.dte_coverage)
print(report.delta_coverage)
print(report.wing_coverage)
print(report.executable_coverage)
print(report.issues)
```

`PASS` means the dataset cleared the configured quality thresholds. It does **not** mean the strategy is profitable, only that the normalized history is sufficiently complete and internally consistent for the requested research configuration.

Raw vendor rows that violate domain invariants such as negative prices, crossed bid/ask markets, invalid deltas, or malformed timestamps are rejected earlier by the adapter/import layer and therefore do not survive into this normalized diagnostic stage.
