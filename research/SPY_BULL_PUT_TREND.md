# SPY Bull Put trend-regime study

This study tests one materially different economic hypothesis after the Bull Put premium study
failed 2020-2025 temporal validation: sell the same defined-risk Bull Put Spread only when SPY
is in a long-term uptrend.

## Frozen structure

- 45 DTE target
- 15 Delta short put
- exact 5-point protective wing
- 50% profit target
- 2x-credit stop
- 21 DTE time exit
- 2% maximum account risk
- 25% mid-to-natural slippage
- $0.65 per contract per leg commission
- no minimum premium threshold beyond positive executable credit and defined risk

The only new entry condition is:

> The **previous trading day's** SPY close must be above its trailing 200-session simple moving
> average.

Using the prior day's trend state avoids same-close signal/execution ambiguity. There is one
trend rule, no SMA optimization, and no candidate selection.

## Sequential research design

The study uses strict stage gating:

1. Discovery: 2011-01-03 through 2015-12-31
2. Temporal stage 1: 2016-01-04 through 2019-12-31, evaluated only if discovery passes
3. Temporal stage 2: 2020-01-02 through 2025-12-12, evaluated only if stage 1 passes
4. QQQ external holdout: allowed only if every SPY stage passes

Discovery requires at least 40 trades, profit factor > 1.10, positive total return, and maximum
drawdown < 10%. Validation stages require at least 40 trades, profit factor > 1.00, positive
return, and maximum drawdown < 10%.

## Results

| Stage | Trades | Return | Max DD | Win rate | Profit factor | Passed |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| 2011-2015 discovery | 96 | +1.14% | 0.90% | 74.0% | 1.26 | Yes |
| 2016-2019 temporal stage 1 | 87 | +0.78% | 1.50% | 78.2% | 1.18 | Yes |
| 2020-2025 temporal stage 2 | 124 | **-0.38%** | 1.73% | 71.8% | **0.95** | No |

The stage-2 exit mix was 89 take-profits, 31 stop-losses, and 4 time exits. Average winner was
+$45.29 while average loser was -$120.66. The win rate remained high, but the loss magnitude was
large enough to make expectancy negative.

The stage-2 run used four stale-leg marks across two trading days under the same narrow,
no-lookahead held-position fallback policy used by the earlier real-SPY studies.

## Interpretation

The 200-session trend filter materially improves the evidence compared with the unfiltered
Bull Put study: the same single rule clears two sequential historical periods spanning 2011
through 2019 without any parameter optimization.

However, the edge does not survive the next six-year period. From 2020 through 2025 the strategy
falls to -0.38% total return and profit factor 0.95 despite a 71.8% win rate.

This is therefore a **stage-2 temporal validation failure**, not a validated strategy.
`QQQ_READY=False`; QQQ remains untouched.

The result points away from more entry-threshold mining. A more useful next analysis is to
explain the 2020-2025 deterioration: whether losses cluster around volatility shocks, gap risk,
rapid trend breaks, or a change in option premium versus realized downside movement. That
analysis should diagnose the existing frozen trades rather than introduce another tuned entry
rule.
