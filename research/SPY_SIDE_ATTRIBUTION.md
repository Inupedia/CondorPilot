# SPY Iron Condor side-attribution study

This document records the follow-up study after the frozen mechanical Iron Condor family failed
the first real-data falsification stage. The purpose is to identify whether the failure is shared
by both credit-spread sides or concentrated in one side.

## Frozen rules

No strategy parameter was optimized for this study:

- 45 DTE target
- 15 Delta short option
- 5-point protective wing
- 50% profit target
- stop at 2x entry-credit close debit
- 21 DTE time exit
- 2% maximum account risk
- 10% minimum credit/width
- 25% mid-to-natural slippage
- $0.65 per contract per leg commission

## Two different questions

### 1. Exact attribution of the rejected Condor

The 2020-01-02 through 2025-12-12 Condor baseline is replayed with exactly the same entries,
exits, contract counts, execution assumptions, and commissions as the prior study. Each closed
trade is then decomposed algebraically into its two vertical spreads.

The two two-leg commissions exactly sum to the original four-leg commission. The side P&Ls must
reconcile to the full Condor P&L on every trade. The runner also hard-fails unless it reproduces
the previously established 98 trades and -$3,091.95 total net P&L.

| Metric | Put side | Call side | Full Condor |
| --- | ---: | ---: | ---: |
| Net P&L | **+$316.40** | **-$3,408.35** | **-$3,091.95** |
| Average P&L / trade | +$3.23 | -$34.78 | -$31.55 |
| Profitable-trade fraction | 78.57% | 61.22% | 56.12%* |
| Best side trade | +$126.55 | +$160.55 | — |
| Worst side trade | -$616.70 | -$513.70 | — |

`*` The Condor win rate is from the prior full-strategy study and is not the arithmetic average of
the two side-level profitable-trade fractions.

The side totals reconcile to -$3,091.95 with a maximum floating-point reconciliation error of
approximately `3.4e-13`. There were no negative algebraic side close marks in the 98 exits.

This is strong structural evidence that the call-credit-spread side, not the put-credit-spread
side, generated the rejected Condor's aggregate loss over 2020-2025. The put side offset about
$316 of the call side's roughly $3,408 loss.

## Standalone spread falsification

Attribution alone does not prove that either side is a viable standalone strategy. Each side was
therefore run independently. A missing or invalid opposite-side option cannot block a standalone
spread entry.

Discovery is fixed to 2016-01-04 through 2019-12-31. A standalone side must satisfy all of:

- at least 40 completed trades
- profit factor > 1.10
- total return > 0
- maximum drawdown < 10%

Only a side that clears every discovery gate may be run on the 2020-2025 temporal holdout.

| Standalone side | Trades | Return | Max DD | Win rate | Profit factor | Passed |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| Bull Put Spread | 12 | **+1.09%** | 0.30% | 91.7% | **5.93** | **No** |
| Bear Call Spread | 69 | **-3.58%** | 4.10% | 59.4% | **0.59** | **No** |

The Bear Call Spread fails economically: both return and profit factor fail the fixed gates.

The Bull Put Spread is different. Its small discovery sample is profitable and has a high profit
factor, but only 12 trades completed in four years. It therefore fails only the minimum-sample
rule and must not be promoted to temporal validation.

The main reason for the small put-spread sample is visible in the audit trail: 727 of 910 flat-day
entry attempts were rejected because the selected put spread did not meet the frozen 10%
credit-to-width threshold. This is useful diagnostic evidence, but it is not permission to relax
the threshold after seeing the result.

## Holdout policy

Neither side cleared the predeclared discovery gates, so:

- SPY 2020-2025 standalone temporal validation was not run for either spread;
- QQQ external holdout was not run;
- `external_holdout_candidates` is empty.

This preserves the study's falsification logic instead of using later data to search for a more
favorable threshold.

## Conclusion

The first structural question now has a clear answer: the rejected mechanical SPY Iron Condor
was dragged down by the call side. In the exact 98-trade decomposition, the put side was slightly
positive while the call side lost more than the full Condor's net loss.

The second question remains unresolved for the put side. The frozen standalone Bull Put Spread
produced an encouraging but statistically inadequate 12-trade sample. It is therefore a
**research lead, not evidence of an edge**.

The next legitimate study should be predeclared before looking at new results and should address
why the 10% credit/width constraint produces so few put-spread entries. It should not simply lower
that threshold until a backtest passes. A useful next design would compare a small set of
principled entry-premium definitions or use a longer independent historical sample, with the
selection rule and minimum-sample requirement fixed in advance.
