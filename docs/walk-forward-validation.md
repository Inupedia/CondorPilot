# Walk-forward and out-of-sample validation

CondorPilot does not treat the best parameter set on one historical sample as evidence of a durable edge. `run_walk_forward()` repeatedly separates the past from the future:

1. build a training window using only snapshots already available at that point in time;
2. evaluate every valid parameter combination on that training window;
3. select the best eligible training result using the configured ranking metric;
4. freeze those parameters;
5. evaluate the frozen strategy on the immediately following out-of-sample window;
6. advance the clock and repeat.

Rolling mode keeps a fixed-size training window. Anchored mode keeps the first training observation fixed and expands the training history as time advances.

## Integrity controls

- **No train/test overlap.** OOS folds are non-overlapping by construction.
- **Dataset quality gate.** Validation can require the PR #6 dataset diagnostics to pass before any parameter selection occurs.
- **Dataset provenance.** Every result retains the exact normalized dataset fingerprint.
- **Fold-end entry embargo.** New positions are blocked when there is not enough calendar time left in a fold to reach the configured time exit. This prevents `end_of_data` liquidation from becoming a hidden source of training or OOS P/L.
- **No forced-liquidation candidates.** Training candidates containing `end_of_data` exits are excluded. An OOS fold that still produces one fails validation instead of silently accepting the result.
- **Frozen OOS parameters.** Test-window parameters are selected exclusively from the preceding training window and cannot be re-optimized inside the test period.
- **Continuous OOS equity.** The ending equity of one OOS fold becomes the starting equity of the next fold.

## Benchmarks

Every fold records two simple benchmarks:

- **Cash:** 0% return.
- **Buy & Hold:** price-only underlying return from the first to the last snapshot in that OOS fold.

The buy-and-hold benchmark intentionally excludes dividends, financing, taxes, and implementation costs. It is a sanity benchmark, not a total-return index substitute.

## Example

```python
from condorpilot import (
    BacktestConfig,
    ParameterGrid,
    WalkForwardConfig,
    load_option_chain_csv,
    run_walk_forward,
)

history = load_option_chain_csv("data/spy-thetadata.csv")
result = run_walk_forward(
    history,
    grid=ParameterGrid(
        target_dte=(30, 45, 60),
        short_delta=(0.10, 0.15, 0.20),
        wing_width=(5.0, 10.0),
        profit_target_fraction=(0.25, 0.50),
        stop_loss_credit_multiple=(1.5, 2.0, 3.0),
        exit_dte=(14, 21),
        max_risk_fraction=(0.005, 0.01),
    ),
    base_config=BacktestConfig(initial_equity=50_000),
    config=WalkForwardConfig(
        train_size=252,
        test_size=63,
        step_size=63,
        rank_by="sortino",
        min_train_trades=3,
    ),
)

print(result.dataset.fingerprint)
print(result.oos_total_return)
print(result.oos_max_drawdown)
print(result.buy_hold_total_return)
print(result.selection_stability)
```

A positive walk-forward result is still not proof that a strategy will make money. It is stronger evidence than in-sample optimization because the selected parameters are repeatedly tested on data that was not used to select them.
