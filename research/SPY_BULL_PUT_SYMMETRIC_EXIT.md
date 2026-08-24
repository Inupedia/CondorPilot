# SPY Bull Put symmetric SMA200 exit study

This post-diagnostic study tests one position-management hypothesis without adding a new
lookback or percentage threshold. Entry remains the prior-day-close-above-SMA200 Bull Put rule.
While a position is open, if the previous trading day's SPY close is at or below SMA200, the
spread is closed at today's EOD option snapshot.

Because this hypothesis was formulated after inspecting all SPY periods, the SPY results below
are model-development robustness checks, not untouched out-of-sample evidence. QQQ remains an
untouched external holdout.

## Frozen structure

- 45 DTE target
- 15 Delta short put
- exact 5-point protective wing
- 50% profit target
- 2x-credit stop
- 21 DTE time exit
- 2% maximum account risk
- existing slippage and commissions
- entry only when previous-session close > previous-session SMA200
- new rule: exit today when the prior-session close <= prior-session SMA200

## Results

| Period | Trades | Return | PF | Win rate | Trend exits | Original net P&L | Managed net P&L |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 2011-2015 | 98 | +0.64% | 1.15 | 70.4% | 11 | +$567.55 | +$321.65 |
| 2016-2019 | 89 | +1.09% | 1.28 | 77.5% | 8 | +$390.85 | +$543.95 |
| 2020-2025 | 128 | **-0.50%** | **0.94** | **68.0%** | 13 | -$192.05 | **-$252.35** |

The rule is inconsistent across periods. It improves 2016-2019 by about $153 but gives back
about $246 in 2011-2015 and worsens 2020-2025 by about $60.

## What changed in 2020-2025

The management rule does reduce loss severity:

- original average loser: -$120.66
- managed average loser: **-$99.55**
- original stop count: 31
- managed stop count: 26

However, it damages the successful-trade frequency even more:

- original win rate: 71.8%
- managed win rate: **68.0%**
- original take-profits: 89
- managed take-profits: 84

Thirteen positions are replaced by trend-break exits. Earlier exits also change the future entry
path, increasing the number of completed trades from 124 to 128. The net effect is worse despite
smaller average losses.

This is important evidence against the idea that simply cutting exposure once the slow trend
boundary breaks solves the recent downside-path problem. SMA200 is a slow state variable, and a
number of the previously observed damaging moves occur over only a few sessions. At the same
time, some positions that cross the trend boundary would have recovered enough to become
profitable under the original management rules.

## Research conclusion

The symmetric SMA200 exit is **rejected**. It does not restore positive expectancy in 2020-2025
and actually lowers PF from about 0.95 to 0.94.

`QQQ_READY=False`; QQQ remains untouched.

The next useful test should not add another trend threshold. If Bull Put research continues, a
more fundamental position-management hypothesis is needed. One defensible candidate is to test
whether the existing option-price stop itself creates adverse path dependency by forcing losses
while the spread is still defined-risk. That hypothesis should be tested as a single frozen
management rule rather than as another stop-multiple grid.
