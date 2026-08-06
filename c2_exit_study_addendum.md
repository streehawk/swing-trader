C-Series Addendum 2 — Exit Study for the C2 Basket (Pre-Registered)

Question: does any of the book's alternative dynamic exits beat the validated RSI(2)>65 exit on the C2 basket — by enough, and consistently enough, to matter?

Source: Connors/Alvarez exit chapter. Their table (1995–2007, in-sample, on a DIFFERENT entry — stocks at 10-day lows) showed dynamic exits beating fixed-day holds clearly, but the spread AMONG dynamic exits was ~0.17pp — likely noise. Their table also compares avg %/trade across exits with different hold lengths, which mechanically favors later exits. This study corrects that: the primary metric is edge per day of exposure, judged out-of-sample, at portfolio level.

Hard rules:

The ENTRY is frozen — C2 exactly as validated (close > SMA200, CumRSI(2) < 35, buy at close; next-open mode reported as before). Any entry change invalidates the study.
The LIVE system is unaffected regardless of results. Live trades RSI>65 as registered in live_trading_protocol.md. An exit change may only reach live via that protocol's Amendment rule (between quarters, in writing), and only if this study's replacement gate passes.
Analysis/backtest only. No brokerage code. Separate branch.
THE CONTENDERS — registered now, no additions

All computed on the day's close; exit fills follow the same conventions as the validated runs (close fill primary, next-open reported). The 25-day valve applies to ALL variants (count firings — MA10 and RSI>75 hold longer, the valve may finally fire).

ID	Exit rule	Status
E0	RSI(2) > 65	INCUMBENT (validated, live)
E1	RSI(2) > 70	challenger
E2	RSI(2) > 75	challenger
E3	close > SMA5	challenger (the book's favorite MA exit; C1's exit)
E4	close > SMA10	challenger

No other exits, no combinations, no trailing variants. Five is already four chances to get fooled by luck — the replacement gate below exists because of that.

SETUP
Instruments: the validated 13 (SPY + 12-ETF basket). Window: POST-BOOK (2008-01-01 → present) for judgment; FULL reported for context.
Costs on (2 bps broad / 5 bps sectors per side). Same caches, same pipeline.
Control: E0 must reproduce the registered Addendum-1 results exactly (same trades, same numbers). Mismatch = pipeline bug; fix pipeline, never rules.
Identical entries across all five variants by construction — every difference in results is attributable to the exit alone. Report the shared entry count.
METRICS — registered hierarchy (this is the anti-gaming core)
PRIMARY: avg % per trading day held (per-trade return ÷ days held, pooled). Corrects the hold-length confound in the book's table.
PRIMARY: portfolio MAR (CAGR/MaxDD) in the registered P-capped fixed-slots sim — the number that describes the system you actually run.
Secondary: pooled avg %/trade, PF, win rate, avg hold, % days in market, worst trade, MAE top-10, valve firings, clustered t-stat (by entry day).
All of the above per 70/30 date split and per instrument.

Note registered in advance: longer-hold exits (E2, E4) will show higher avg %/trade almost automatically. That alone means NOTHING. They must win per-day and at portfolio level to count.

REPLACEMENT GATE — what it takes to dethrone E0

A challenger REPLACES E0 as the recommended exit only if ALL hold (POST-BOOK, close fill, costs on):

Per-day return ≥ E0's × 1.10 (at least 10% relative improvement), AND
Portfolio MAR ≥ E0's × 1.10, AND
Both of the above hold in BOTH 70/30 splits, AND
Beats E0 on per-day return on ≥ 8 of 13 instruments, AND
Not execution-fragile: next-open PF ≥ 1.1 and next-open per-day return still ≥ E0's.

Anything less = E0 STANDS. "Slightly better in the pooled table" is expected noise among sibling exits and changes nothing. If two challengers both clear the gate, the one with higher portfolio MAR wins; ties go to the shorter avg hold (less exposure).

If E0 stands (the likely outcome): record it as a positive result — the validated exit survived four registered challengers on out-of-sample data. That strengthens confidence in the live system.

If a challenger wins: it does NOT go live automatically. It becomes a written amendment proposal for the next quarterly window per live_trading_protocol.md Section 10, including a restatement of this study's numbers and the bot's verification that the change matches the validated configuration.

Attribution the report must answer
Does the book's in-sample ranking (RSI>75 ≳ RSI>70 ≈ MA10 ≳ RSI>65 ≈ MA5) survive out-of-sample on this entry? Where does it break?
Exit-family comparison: RSI-style (E0–E2) vs MA-style (E3–E4) — is either family systematically better per day held?
How much does hold length alone explain of the avg %/trade differences?
Valve firings by variant — do the slower exits start leaving trades stranded?
Anti-overfitting rules
Five variants, run once each, judged against the registered gate. No threshold interpolation (no "RSI>72 looks interesting"), no exit combinations, no per-instrument exit selection — one exit for the whole basket, as live trades it.
Report honest results even if every challenger loses. E0 standing IS a finding.
COMMANDS

Step 1: Implement the exit variants + tests (each exit unit-tested; E0 regression-tested against registered results). Run the E0 control. Report control tie-out + shared entry count. STOP and show me. Step 2 (after approval): Run E1–E4, full report per the metrics hierarchy, replacement-gate verdict, attribution answers, postmortem. Commit on the study branch. Live system untouched.

Out of scope

Entry changes, stops, targets, trailing stops, new instruments, scale-ins, any exit not in the table, live deployment of anything.