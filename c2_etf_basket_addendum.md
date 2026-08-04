C-Series Addendum 1 — C2 Replication on an ETF Basket (Pre-Registered)

Purpose: Test whether the C2 edge (Cumulative RSI(2), X=2, Y<35) is a property of short-term mean reversion in liquid index products generally, or a SPY-specific fluke. Secondary purpose: raise signal frequency honestly — by adding instruments, NOT by loosening thresholds.

This is a replication, not a new strategy. The C2 rules are FROZEN exactly as registered and validated in connors_rsi2_spec.md:

Instrument close > its own SMA200.
Cumulative RSI = RSI(2, Wilder) today + yesterday. Buy at close when < 35.
Exit at close when RSI(2) > 65.
25-day time valve (registered carry-over; fired 0 times on SPY).
No stop-loss. One position per instrument.

No rule, threshold, or exit may differ per instrument. Any per-instrument adjustment = tuning = invalid. C3 and C1 are NOT part of this addendum.

Hard guardrails (unchanged): analysis/backtest only. No live orders. Universe files untouched — this addendum uses its own ETF cache.

THE BASKET — registered now, no swaps, no additions, no post-hoc removals
Ticker	What it is	Inception	Cost model (per side)
QQQ	Nasdaq-100	1999-03	2 bps
DIA	Dow 30	1998-01	2 bps
IWM	Russell 2000	2000-05	2 bps
XLB	Materials	1998-12	5 bps
XLE	Energy	1998-12	5 bps
XLF	Financials	1998-12	5 bps
XLI	Industrials	1998-12	5 bps
XLK	Technology	1998-12	5 bps
XLP	Staples	1998-12	5 bps
XLU	Utilities	1998-12	5 bps
XLV	Health care	1998-12	5 bps
XLY	Discretionary	1998-12	5 bps

12 instruments; all continuously listed since inception (no survivorship issue); all among the most liquid ETFs in existence. SPY itself is EXCLUDED from the basket (its verdict exists) but runs through the identical new pipeline as a CONTROL — it must reproduce the registered C2 result exactly, or the pipeline is broken.

Known limitation, registered: these instruments are heavily correlated with SPY and each other. 12 positive results would NOT be 12 independent confirmations — report the pooled result AND the correlation/clustering analysis so the evidence is weighed honestly.

TICKET A1-1 — Data
Fetch full daily OHLCV history for all 12 (dividend/split-adjusted, close_raw retained — same registered convention as SPY) into a dedicated cache.
Report per ticker: first bar, last bar, total bars, first usable bar (post-SMA200 warm-up), missing-session gaps > 5 (flag, never bridge).
Windows per instrument: FULL (usable → present) and POST-BOOK (2008-01-01 → present; all 12 have full coverage there, which is where the verdict lives).
TICKET A1-2 — Pipeline
Reuse strategies/rsi2.py and core/c_series_backtest.py unchanged. New code limited to: multi-instrument runner + portfolio simulator (below).
CONTROL: run SPY through the new runner; assert per-trade results are identical to the registered C2 verdict run (same trades, same dates, same numbers). A mismatch is a pipeline bug — fix the pipeline, never the strategy.
Look-ahead: confirm_no_lookahead() replay per instrument, non-vacuous.
TICKET A1-3 — Per-instrument runs

For each of the 12, POST-BOOK window, both fill modes, costs ON: report #trades, win %, avg %/trade, avg win/loss %, worst trade %, PF, max DD, avg hold, % days in market, t-stat, valve firings.

TICKET A1-4 — Portfolio simulation (the realistic view)

Signals will cluster — when the market dips, many sectors signal at once. Two sims, POST-BOOK, close fill, costs ON:

P-uncapped: take every signal, capital split equally across open positions.
P-capped: max 3 concurrent positions; when >3 signal on one day, priority = lowest Cumulative RSI (most oversold first) — registered tiebreak, chosen now. Report for both: equity curve stats (CAGR, max DD, MAR), trades/year, % days ≥1 position open, largest single-day cluster, % of signals refused by the cap, and the expectancy of taken vs refused signals (the H1 capacity autopsy, repeated here on purpose — this is where H1 died, so it must be measured, not assumed). Benchmarks on the same window: SPY buy-and-hold, and the registered SPY-only C2 result.
GATES — registered before any result

G1: Replication gate (the science question).

SUCCESS if ≥ 8 of 12 instruments show positive avg %/trade after costs in POST-BOOK, AND the pooled (all trades, all instruments) result shows PF ≥ 1.3 and avg ≥ +0.20%/trade with ≥ 400 pooled trades, sign holding in both 70/30 splits.
PARTIAL if 5–7 of 12 positive: the edge is real but not universal — report which instrument types carry it; no cherry-picking a sub-basket for deployment (that would need its own future registration with fresh reasoning).
REFUTED if ≤ 4 of 12 positive: the SPY result was likely luck or SPY-specific; downgrade confidence in the C2 verdict itself and say so in the README.

G2: Frequency gate (the practical question).

Pooled trades/year in P-capped ≥ 25 (vs ~8/yr on SPY alone), with P-capped MAR ≥ SPY buy-and-hold MAR. Otherwise the basket adds activity without adding quality — record and keep SPY-only as the reference implementation.

G3: Fragility check. Pooled next-open PF ≥ 1.1 (same bar as the original spec).

Anti-tuning: no threshold moves, no instrument swaps, no dropping losers after results, no per-instrument exits. If G1 is REFUTED, this addendum closes and the C-series stands at: C2/C3 PASS on SPY, replication failed — an honest asterisk.

Attribution the report must answer:

Does the edge concentrate in broad indexes (QQQ/DIA/IWM) vs sectors?
Clustering: how correlated are signal dates across instruments (pairwise overlap)?
Does the P-capped priority rule (most oversold first) help or hurt vs random-take? (Report only — whatever the answer, the rule stays as registered for this run.)
Crisis windows (2008-09, 2020-02/03, 2022): pooled behavior, worst MAE top 10.
COMMANDS

Step 1: Implement A1-1 and A1-2. Report data coverage for all 12 + the SPY control assertion result + look-ahead checks. STOP and show me before any basket run. Step 2 (after my approval): Run A1-3 and A1-4. One report: per-instrument table, pooled result, both portfolio sims, gate verdicts G1/G2/G3, attribution answers, postmortem paragraph. Commit on the C-series branch. Nothing deployed.

Out of scope

C1/C3 on the basket, individual stocks, international/bond/commodity ETFs, leveraged products, threshold variations, stop-loss retrofits, live trading. Any of these requires a new pre-registered addendum.