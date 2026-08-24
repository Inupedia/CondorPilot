# SPY Bull Put entry-premium study

This study follows the side-attribution result that the rejected mechanical Iron Condor lost
primarily on its call side while the put side was slightly positive. The purpose is to test
whether the Bull Put Spread signal survives when the original 10% credit/width gate is replaced
by predeclared premium definitions that produce a meaningful sample.

## Frozen strategy

Everything except entry-premium eligibility remains unchanged:

- 45 DTE target
- 15 Delta short put
- exact 5-point protective wing
- 50% profit target
- 2x-credit stop
- 21 DTE time exit
- 2% maximum account risk
- 25% mid-to-natural slippage
- $0.65 per contract per leg commission

Premium metrics use the modeled execution credit rather than the quote mid.

## Predeclared candidates

1. execution credit / spread width >= 5%
2. execution credit / defined max loss >= 10%
3. simple annualized execution credit / defined max loss >= 30%, using actual entry DTE

Discovery is SPY 2016-01-04 through 2019-12-31. A candidate must have at least 40 trades,
profit factor > 1.10, positive total return, and maximum drawdown < 10%.

If multiple candidates pass, only the highest discovery profit factor advances to SPY
2020-01-02 through 2025-12-12. Temporal validation requires at least 40 trades, profit factor
> 1.00, positive return, and maximum drawdown < 10%.

QQQ is not touched unless SPY temporal validation passes.

## Discovery results

| Rule | Trades | Return | Max DD | Win rate | Profit factor | Passed |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| Credit / width >= 5% | 102 | +0.93% | 1.71% | 77.5% | 1.18 | Yes |
| Credit / max loss >= 10% | 26 | +0.66% | 0.57% | 76.9% | 1.45 | No: sample < 40 |
| Annualized return-on-risk >= 30% | 102 | +0.96% | 1.68% | 77.5% | 1.18 | Yes |

The annualized-return-on-risk rule had the highest discovery profit factor and therefore became
the single frozen temporal-validation candidate.

An important caution is that the annualized 30% threshold was almost non-binding on otherwise
eligible setups: all 102 accepted discovery entries exceeded it, with observed accepted values
from roughly 36.4% to 100.7%. The 5% credit/width rule rejected only one otherwise eligible flat
day. The two passing discovery results are therefore economically very similar rather than
independent confirmations.

## 2020-2025 temporal validation

Frozen rule: annualized credit / max loss >= 30%.

| Metric | Result |
| --- | ---: |
| Trades | 161 |
| Total return | **-0.49%** |
| Final equity | $49,755.05 |
| Maximum drawdown | 2.54% |
| Win rate | 72.05% |
| Profit factor | **0.96** |
| Average trade | -$1.52 |
| Average winner | +$49.27 |
| Average loser | -$132.45 |
| Worst trade | -$304.70 |

Exit counts were 114 take-profits, 41 stop-losses, and 6 time exits.

The candidate fails temporal validation because profit factor is below 1.00 and total return is
negative. The 72% win rate is not sufficient to overcome the loss magnitude.

The temporal run used four stale-leg marks across two trading days under the same narrow,
no-lookahead held-position fallback policy used in the earlier real-SPY research.

## Research conclusion

The 12-trade Bull Put result from the prior side-attribution study does not generalize into a
robust positive-expectancy signal once the entry gate is loosened enough to produce a meaningful
sample.

The discovery period shows a small positive expectancy for two nearly equivalent loose premium
rules, but the selected rule reverses to -0.49% with profit factor 0.96 across 161 future trades.
The result is therefore **temporal validation failure**.

`QQQ_READY=False`; QQQ remains an untouched external holdout.

This result does not prove that all Bull Put Spread strategies are unprofitable. It rejects this
specific mechanical family: 45 DTE / 15 Delta / 5-point wing / 50% TP / 2x stop / 21 DTE exit
with the three predeclared premium definitions above. Further work should require a materially
different economic hypothesis rather than additional threshold mining on the same SPY history.
