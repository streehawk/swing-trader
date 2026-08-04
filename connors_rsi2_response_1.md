C-Series Step 1 — complete. L0 reproduces the book.

C-0a — SPY history

┌───────────────────────┬─────────────────────────────────────┐
│                       │                                     │
├───────────────────────┼─────────────────────────────────────┤
│ First bar             │ 1993-01-29 (SPY's actual inception) │
├───────────────────────┼─────────────────────────────────────┤
│ Last bar              │ 2026-07-31                          │
├───────────────────────┼─────────────────────────────────────┤
│ Total bars            │ 8,433                               │
├───────────────────────┼─────────────────────────────────────┤
│ First usable bar      │ 1993-11-11 (after SMA200 warm-up)   │
├───────────────────────┼─────────────────────────────────────┤
│ Gaps > 5 trading days │ 0                                   │
└───────────────────────┴─────────────────────────────────────┘

Cache: data/validation_cache/spy_extended.parquet — dedicated, prices.parquet untouched.

My first gap check used a calendar-day threshold and reported zero. That was wrong — it would have forgiven the 9/11 shutdown (Mon→Mon is only 7 calendar days but four lost sessions). Recounting in missing trading sessions finds exactly three closures, all real and correctly dated:

┌─────────────────────────┬───────────────┬───────────────────────────────┐
│           Gap           │ Sessions lost │          What it was          │
├─────────────────────────┼───────────────┼───────────────────────────────┤
│ 2001-09-10 → 09-17      │ 4             │ 9/11 market shutdown          │
├─────────────────────────┼───────────────┼───────────────────────────────┤
│ 2006-12-29 → 2007-01-03 │ 2             │ New Year + Ford state funeral │
├─────────────────────────┼───────────────┼───────────────────────────────┤
│ 2012-10-26 → 10-31      │ 2             │ Hurricane Sandy               │
└─────────────────────────┴───────────────┴───────────────────────────────┘

None exceeds the spec's 5-session flag threshold. Nothing bridged or interpolated.

Registered data choice: bars are dividend/split-adjusted. A dip-buying strategy holds through ex-dividend dates; on raw prices every ex-div day prints as a loss the trader never took, and it distorts RSI(2), which reads exactly two days of change. The raw close is kept as close_raw so the choice is auditable.

C-0b — RSI(2), Wilder

strategies/rsi2.py. Wilder's smoothing, not the simple-average variant — one test explicitly asserts the two disagree on the same series, so the spec's insistence isn't decorative.

All registered acceptance cases pass:

┌─────────────────────────────────┬───────────────────────────────────────────────────────────────────────────┐
│              Test               │                                  Result                                   │
├─────────────────────────────────┼───────────────────────────────────────────────────────────────────────────┤
│ Known-value sequence, 2 dp      │ ✅ hand-computed [66.67, 88.89, 47.06, 81.63], derivation in the test     │
│                                 │ file                                                                      │
├─────────────────────────────────┼───────────────────────────────────────────────────────────────────────────┤
│ All-gains → 100                 │ ✅                                                                        │
├─────────────────────────────────┼───────────────────────────────────────────────────────────────────────────┤
│ All-losses → 0                  │ ✅                                                                        │
├─────────────────────────────────┼───────────────────────────────────────────────────────────────────────────┤
│ Insufficient history → None     │ ✅ fails closed, never a neutral 50                                       │
├─────────────────────────────────┼───────────────────────────────────────────────────────────────────────────┤
│ cumulative_rsi X=2, [20,10] →   │ ✅                                                                        │
│ 30                              │                                                                           │
└─────────────────────────────────┴───────────────────────────────────────────────────────────────────────────┘

Two extras worth having: a flat series returns 50 rather than dividing by zero mid-backtest, and a prefix-reproduction test — every RSI and SMA value must equal what you'd get from a truncated history. That's the property the close-fill convention depends on, so it's asserted rather than argued.

C-0c — execution modes

core/c_series_backtest.py. Percent-per-trade (no stop ⇒ no R to divide by), drawdown reporting mandatory, 25-day valve, 2bps/side.

Look-ahead check passed on real SPY data — and not vacuously:

┌─────────┬─────────────────┐
│ Variant │ Trades compared │
├─────────┼─────────────────┤
│ C1      │ 129             │
├─────────┼─────────────────┤
│ C2      │ 264             │
├─────────┼─
│ C3      │ 371             │
└─────────┴─────────────────┘

One judgement call I want on the record: next-open mode applies to exits too, not just entries. The spec frames it as "a retail trader placing the order after the close," and that trader is late leaving as well as late arriving. Modelling only the late entry would flatter the result at exactly the point the mode exists to stress.

52 tests pass; 377 across all strategy/backtest suites.

L0 — implementation check (BOOK 1993-11-11 → 2007-12-31, close fill, costs off)

┌─────────┬─────────┬───────┬────────┬─────────┬──────────┬────────┬───────┬────────┬──────┬──────────────┐
│ Variant │ #Trades │ Win%  │  Avg%  │ AvgWin% │ AvgLoss% │ Worst% │  PF   │ MaxDD% │ Hold │ %Days in mkt │
├─────────┼─────────┼───────┼────────┼─────────┼──────────┼────────┼───────┼────────┼──────┼──────────────┤
│ C1      │ 49      │ 83.7% │ +1.050 │ +1.50   │ −1.24    │ −2.45  │ 6.176 │ 3.2    │ 3.3  │ 4.5%         │
├─────────┼─────────┼───────┼────────┼─────────┼──────────┼────────┼───────┼────────┼──────┼──────────────┤
│ C2      │ 118     │ 83.0% │ +0.814 │ +1.20   │ −1.07    │ −3.05  │ 5.497 │ 3.0    │ 3.5  │ 11.6%        │
├─────────┼─────────┼───────┼────────┼─────────┼──────────┼────────┼───────┼────────┼──────┼──────────────┤
│ C3      │ 164     │ 81.1% │ +0.660 │ +1.09   │ −1.17    │ −3.05  │ 3.974 │ 4.5    │ 3.7  │ 17.0%        │
└─────────┴─────────┴───────┴────────┴─────────┴──────────┴────────┴───────┴────────┴──────┴──────────────┘

Versus the book

┌─────────┬───────────┬───────┬──────┬───────────────┐
│ Variant │ Book Win% │ Ours  │ Diff │ Within ±10pp? │
├─────────┼───────────┼───────┼──────┼───────────────┤
│ C1      │
├─────────┼───────────┼───────┼──────┼───────────────┤
│ C3      │ 85.5%     │ 81.1% │ −4.4 │ ✅            │
└─────────┴───────────┴───────┴──────┴───────────────┘

All three pass. C1 lands within a tenth of a percentage point, which is about as strong a signal as you can get that the Wilder RSI(2) and the close-fill logic are faithful.

The 25-day valve fired zero times in all three variants — the exit rules resolve on their own, as the book claims.

I checked whether my dividend-adjustment choice explains the −5pp/−4pp on C2/C3. It doesn't: on raw prices C1 goes 83.7→85.1%, C2 83.0→84.1%, C3 81.1→79.9% — about a point, in both directions, no systematic bias. So the residual gap is window/vendor differences in the book's own data, not a harness defect. Per the spec I changed nothing, and the registered adjusted-price choice stands on its merits regardless of which matches the book better.

Sibling overlap (L0)

┌──────────┬────────────────────┬─────────┐
│   Pair   │ Shared entry dates │ Jaccard │
├──────────┼────────────────────┼─────────┤
│ C1 vs C2 │ 28                 │ 0.201   │
├──────────┼────────────────────┼─────────┤
│ C1 vs C3 │ 19                 │ 0.098   │
├──────────┼────────────────────┼─────────┤
│ C2 vs C3 │ 74                 │ 0.356   │
└──────────┴────────────────────┴─────────┘

Less correlaagainst 3–4.5% strategy drawdowns at ~5–17% time in market.

Stopping here, as instructed

Step 2 (L1–L4) awaits your approval. Two notes:

- I did not create a branch. The spec opens by naming a dedicated branch, but the tree currently carries uncommitted H1/H2 work; branching now would sweep that onto the C-series branch. That's your history to arrange — say the word and I'll do it, or commit the H-series first.
- L0 is not evidence. It's in-sample by construction — the authors developed these rules on this data. It says the harness is faithful, nothing more. The verdict lives entirely in L2.