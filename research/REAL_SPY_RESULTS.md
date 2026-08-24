# CondorPilot real SPY research results

This document records the first real-data falsification study for the CondorPilot Iron Condor
strategy. The purpose is not to present an optimized backtest. It is to determine whether the
current strategy family shows enough evidence of positive expectancy to justify further work.

## Data

Source: `anahatsingh-ui/options-dataset-hist`, a public historical options-chain mirror.

The study uses real SPY end-of-day option-chain observations with bid/ask, implied volatility,
Delta, volume/open interest, and underlying prices. The primary baseline period is 2020-01-02
through 2025-12-12. Earlier SPY history is used for predeclared discovery tests.

The research workflow downloads the parquet files at runtime. Historical market data is not
committed to this repository.

## Frozen baseline

- 45 DTE target
- 15 Delta short put and short call
- 5-point wings
- 50% profit target
- stop when loss reaches 2x initial credit
- 21 DTE time exit
- 2% maximum account risk
- 10% minimum credit/width
- 25% mid-to-natural slippage assumption
- $0.65 per contract per leg commission assumption

## 2020-2025 baseline result

| Metric | Result |
| --- | ---: |
| Initial equity | $50,000.00 |
| Final equity | $46,908.05 |
| Net P&L | -$3,091.95 |
| Total return | -6.18% |
| CAGR | -1.07% |
| Maximum drawdown | 6.91% |
| Trades | 98 |
| Win rate | 56.12% |
| Profit factor | 0.63 |
| Average trade | -$31.55 |
| Average winner | +$93.90 |
| Average loser | -$192.02 |
| Worst trade | -$515.65 |
| Market exposure | 88.44% |

Exit counts were 38 take-profits, 20 stop-losses, and 40 time exits.

Annual returns were approximately:

- 2020: -2.41%
- 2021: -0.06%
- 2022: +0.84%
- 2023: -1.65%
- 2024: -1.11%
- 2025: -1.93%

The result therefore is not explained by a single stress year.

## Cost ablation

The same baseline was rerun for 2020-2025 with zero commission and zero slippage.

| Metric | Result |
| --- | ---: |
| Total return | -2.02% |
| Profit factor | 0.85 |
| Win rate | 57.89% |
| Maximum drawdown | 3.98% |
| Trades | 95 |

Removing modeled trading costs improves the result but does not produce positive expectancy.
This is evidence against treating fees/slippage as the primary cause of failure.

## Predeclared 2016-2019 sensitivity study

The candidate list was fixed before running the study. A candidate had to satisfy all of:

- at least 40 trades
- profit factor > 1.0
- total return > 0
- maximum drawdown < 10%

| Candidate | Trades | Return | Profit factor | Passed |
| --- | ---: | ---: | ---: | --- |
| Baseline | 66 | -4.71% | 0.53 | No |
| 10 Delta | 46 | -3.47% | 0.42 | No |
| 20 Delta | 65 | -3.21% | 0.68 | No |
| 10-point wings | 51 | -2.41% | 0.60 | No |
| 25% profit target | 93 | -4.07% | 0.57 | No |
| 75% profit target | 58 | -2.44% | 0.69 | No |
| 1.5x stop | 73 | -3.71% | 0.59 | No |
| 3x stop | 60 | -5.18% | 0.52 | No |
| No practical stop | 59 | -3.40% | 0.61 | No |
| 14 DTE exit | 63 | -4.68% | 0.56 | No |
| 10 Delta + 10-point wings | 10 | -0.28% | 0.71 | No |
| 20 Delta + 10-point wings | 59 | -1.32% | 0.81 | No |

No candidate qualified for temporal validation. The research engine intentionally emitted
`SELECTED=None` rather than selecting the least-bad parameter set.

The 25% profit-target case is especially useful: it reached a 72.0% win rate and still lost
money. This is direct evidence that a high win rate is not sufficient for this payoff structure.

## No-lookahead IV percentile test

A second hypothesis tested whether Iron Condors should only be entered when implied volatility
is relatively high.

The strategy parameters above remained frozen. The only changing input was entry eligibility.

Daily volatility proxy:

1. choose the expiration nearest 45 DTE within the existing entry window;
2. choose the strike nearest the SPY close;
3. take the median call/put implied volatility at that point;
4. compare current IV with only the prior 252 IV observations;
5. require at least 126 prior observations before trading.

The current day's IV is never included in its own historical percentile, so the filter contains
no forward-looking percentile information.

Discovery gates were fixed at at least 20 trades, profit factor > 1.10, positive return, and
maximum drawdown < 10%.

| Minimum IV percentile | Trades | Return | Max DD | Win rate | Profit factor | Passed |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| 50th | 42 | -3.69% | 5.02% | 54.8% | 0.49 | No |
| 70th | 27 | -3.60% | 4.27% | 48.1% | 0.39 | No |
| 80th | 22 | -2.44% | 2.80% | 54.5% | 0.46 | No |

No volatility-regime candidate qualified. The workflow emitted `SELECTED_REGIME=None`.
Therefore QQQ external holdout testing was deliberately not run. Running an external holdout
for a strategy that already failed discovery would not validate a predeclared hypothesis.

## Data-quality policy

Real historical option data contains occasional contract-day gaps and internally inconsistent
end-of-day four-leg quotes.

The research runner uses these rules:

- entries always require same-day quotes;
- no stale quote is permitted for entry;
- held positions normally require same-day exact-contract bid/ask;
- if an exact held contract is absent, only the immediately previous trading observation may be
  used once;
- if four same-day legs imply an impossible negative close debit, the complete prior-day combo
  may be used once instead of clipping the price to zero;
- all fallbacks are counted and disclosed;
- a longer gap remains a hard failure.

The 2020-2025 baseline required six stale leg marks across two trading days, too few to explain
the overall negative result.

## Research conclusion

The current evidence does **not** support the frozen mechanical Iron Condor strategy as a
positive-expectancy strategy.

Four independent checks point in the same direction:

1. the 2020-2025 real-data baseline has profit factor 0.63 and negative return;
2. all 12 predeclared parameter variations fail the 2016-2019 discovery gates;
3. removing all modeled commission and slippage still produces a negative 2020-2025 result;
4. no-lookahead high-IV percentile entry filters also fail, with profit factors below 0.50.

The correct result of this research stage is therefore **strategy-family rejection**, not another
round of unconstrained parameter optimization.

Further work on CondorPilot should either test a materially different economic hypothesis with
predeclared rules (for example an explicitly defined volatility-risk-premium model), or pivot
away from the current mechanical Iron Condor strategy. It should not declare a production or
paper-trading edge from the existing model.
