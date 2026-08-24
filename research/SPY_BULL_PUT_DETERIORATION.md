# SPY Bull Put temporal-deterioration diagnostics

This report diagnoses the fixed SMA200 Bull Put Spread trades from the prior real-SPY study. It
does **not** change any entry or exit rule and does not search for a new threshold. The replay is
hard-checked to reproduce exactly 96 trades in 2011-2015, 87 trades in 2016-2019, and 124 trades
in 2020-2025.

## Executive finding

The 2020-2025 failure is not explained by thinner option premium or by a handful of isolated
outlier losses. The evidence points primarily to a **decline in successful-trade frequency under
more adverse underlying price paths**, with a secondary increase in average loss severity.

The recent period actually entered at richer modeled premium and a larger average IV-minus-RV
spread. What changed was the realized path after entry: stop frequency increased, win rate fell,
and the worst close-to-close move during a typical holding period became more negative.

## Stage-level comparison

| Metric | 2011-2015 | 2016-2019 | 2020-2025 |
| --- | ---: | ---: | ---: |
| Trades | 96 | 87 | 124 |
| Net P&L | +$567.55 | +$390.85 | **-$192.05** |
| Profit factor | 1.26 | 1.18 | **0.95** |
| Win rate | 74.0% | 78.2% | **71.8%** |
| Stop rate | 21.9% | 20.7% | **25.0%** |
| Average winner | +$39.05 | +$37.28 | +$45.29 |
| Average loser | -$88.21 | -$112.84 | **-$120.66** |
| Mean entry credit | $0.384 | $0.366 | **$0.440** |
| Mean credit / max loss | 8.32% | 7.91% | **9.66%** |
| Mean short-put IV | 19.90% | 17.67% | **22.15%** |
| Mean prior-20-session RV | 12.73% | 11.51% | **14.22%** |
| Mean IV minus RV20 | 7.17 pts | 6.16 pts | **7.92 pts** |
| Mean max adverse close move while held | -1.41% | -1.13% | **-1.50%** |
| Mean worst one-session close move while held | -0.99% | -0.94% | **-1.24%** |
| Worst 5 trades' share of total losses | 32.1% | 39.1% | **22.5%** |
| Worst 10 trades' share of total losses | 54.6% | 65.5% | **42.4%** |

The recent period therefore does not look like a premium-compression problem. Entry credit,
credit relative to defined max loss, short-put IV, and IV minus recent realized volatility are
all higher than in 2016-2019.

It also does not look like a single-black-swan explanation. Loss concentration is lower in
2020-2025: the five worst trades account for only 22.5% of total losses versus 39.1% in
2016-2019. The worst recent trades span 2020, 2022, 2023, 2024, and 2025.

## Hit-rate versus payoff decomposition

Using each period's observed average winner and average loser, the approximate break-even win
rates are:

- 2011-2015: 69.3% required versus 74.0% observed, a +4.6 percentage-point cushion.
- 2016-2019: 75.2% required versus 78.2% observed, a +3.0 point cushion.
- 2020-2025: 72.7% required versus 71.8% observed, a **-0.9 point deficit**.

The recent average winner is actually larger, so its break-even hit rate is lower than in
2016-2019. The strategy nevertheless falls below that threshold.

A simple arithmetic counterfactual helps isolate the change; it is descriptive, not causal:

- keep 2020-2025 average winner/loss magnitudes but restore the 2016-2019 win rate: expected
  value is about **+$9.05 per trade**;
- keep 2016-2019 average winner/loss magnitudes but impose the 2020-2025 win rate: expected value
  is about **-$5.10 per trade**.

This makes hit-rate deterioration the larger first-order contributor, while the increase in
average loss magnitude remains a secondary headwind.

## Underlying path evidence

Across all recent trades, the mean maximum adverse SPY close move while held worsens to -1.50%
from -1.13% in 2016-2019. The mean worst one-session close move worsens to -1.24% from -0.94%.

Within 2020-2025, the difference between winners and losers is much larger:

| Feature | Winners | Losers |
| --- | ---: | ---: |
| Mean max adverse close move | -0.74% | **-3.42%** |
| Median max adverse close move | -0.53% | **-3.00%** |
| Mean worst one-session close move | -0.82% | **-2.29%** |
| Median worst one-session close move | -0.82% | **-1.95%** |
| Mean next-session close return | +0.12% | **-0.23%** |

The largest recent losses include rapid downside episodes in February-March 2020, June and
October 2020, April 2022, August-September 2023, July and December 2024, and March 2025. This is
consistent with a broader downside-path problem rather than one isolated crash.

## What does not discriminate recent winners from losers

Several intuitive entry metrics are nearly the same for recent winners and losers:

- credit / max loss: 9.70% for winners versus 9.54% for losers;
- short-put IV: 22.19% versus 22.03%;
- prior-20-session realized volatility: 14.38% versus 13.83%;
- IV minus RV20: 7.82 versus 8.20 volatility points;
- prior-day distance above SMA200: 8.30% versus 8.14%.

These diagnostics do not support immediately introducing a threshold on premium, IV, VRP, or
SMA distance. The failed trades are not obviously separable by those simple entry-state
variables in the already-observed 2020-2025 sample.

## Research conclusion

The fixed Bull Put strategy's temporal deterioration is best described as **path-risk / hit-rate
regime deterioration**:

1. premium compensation did not shrink;
2. IV relative to recent realized volatility did not shrink;
3. losses are not dominated by only a few outliers;
4. stop frequency rises from about 21% to 25%;
5. the realized win rate falls below the payoff-implied break-even rate;
6. adverse SPY close paths while positions are open become more severe;
7. simple pre-entry premium, IV, RV, VRP, and SMA-distance metrics do not clearly separate recent
   winners from losers.

The next research step should therefore **not** be another entry-threshold grid. A materially
different model would need to address dynamic downside-path risk, for example by changing the
position-management hypothesis or using information that reacts to emerging downside movement.
Any such hypothesis must be declared before testing and should use a fresh staged validation
plan. QQQ remains untouched throughout this diagnostic study.
