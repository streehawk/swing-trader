C-Series — Connors/Alvarez RSI-2 Strategies on SPY (Pre-Registered)

Branch: dedicated branch for strategies from Connors & Alvarez, Short Term Trading Strategies That Work. This document is the registration for the first three variants (C1–C3). No other strategy from the book may be implemented without its own pre-registered addendum — 16 strategies tested until one passes is data mining, and this project does not do that.

Relationship to edge_search_protocol.md: the C-series runs under the same GLOBAL RULES (no tuning after results, train/test split, no look-ahead, honest labels, report-even-if-FAIL). H2–H4 remain queued and unchanged. The C-series is a registered detour, not a replacement.

Why this is a fair test: the book's results (1993–2007) are in-sample — the authors developed the rules on that data. Everything after 2007 is a true out-of-sample exam. Instrument is SPY only: no survivorship bias (the instrument existed the entire period), no signal clustering/capacity problem (one ticker), minimal slippage (most liquid instrument in existence). This is the cleanest data situation the project has had.

Hard guardrails (unchanged): analysis/backtest only. No brokerage connection, no live orders. This branch trades SPY exclusively — universe files untouched.

TICKET C-0 — Prerequisites (build first, in this order)
C-0a — Extended SPY history
Fetch maximum available SPY daily OHLCV (target: 1993-01-29 inception → present) into a dedicated cache (do not touch prices.parquet).
Report: first bar, last bar, total bars, any gaps > 5 trading days (flag, don't silently bridge).
Define the analysis windows, registered now:
FULL: first usable bar → present (usable = after 200-day MA warm-up).
BOOK (in-sample reference): 1993 → 2007-12-31. Expect results similar to the book's; this validates the implementation, not the edge.
POST-BOOK (the real exam): 2008-01-01 → present. This window decides the verdict. It contains 2008, 2011, 2015-16, 2018, 2020, 2022 — six distinct stress episodes.
C-0b — RSI(2), Wilder's method (the book's method)
Implement rsi(closes, period=2) using Wilder's smoothing (initial average of first period gains/losses, then avg = (prev_avg*(period-1) + current)/period). NOT the simple-average variant — the two diverge and the book uses Wilder.
Unit tests: (a) known-value test against a hand-computed or published RSI(2) sequence (assert to 2 decimals); (b) all-gains series → 100; (c) all-losses → 0; (d) insufficient history → None (fail closed).
cumulative_rsi(closes, period=2, x_days=2) = sum of the last x_days RSI(2) values. Unit test: X=2 with RSI values [20, 10] → 30.
C-0c — Close-fill execution mode
Add an execution mode to the backtest: fill at the signal bar's close (the book's convention: signal computed on the close, buy that same close).
Look-ahead note, registered: this is legitimate ONLY because the signal uses no data beyond the close it fills at. The existing confirm_no_lookahead() replay must pass.
Also run every variant in a second mode — fill at next day's open — as the conservative reality check (a retail trader placing the order after the close). The gap between close-fill and next-open-fill results measures how much of the edge is execution-fragile. Report both; the GATE is judged on close-fill, but a variant whose edge vanishes at next-open must be flagged EXECUTION-FRAGILE.
Costs: 2bps/side for SPY (registered; SPY spread is ~1 cent), plus the same commission model as prior phases.
THE VARIANTS — all share these rules
Trend filter: SPY close > SMA200(SPY). No new entries otherwise.
No stop-loss. Registered deliberately — it is the book's design and the win rates are meaningless without it. Consequence: risk is managed by the trend filter and position sizing only. This makes DRAWDOWN REPORTING MANDATORY (see gate).
No pyramiding/scale-in in v1. (The book's "aggressive" versions scale in; out of scope for this registration.)
One position at a time per variant. Each variant is evaluated independently.
Sizing for reporting: run each variant with 100% of a notional account in SPY per signal (the book's convention) — this is an analysis convention, not advice. Per-trade result is reported in % return per trade (NOT R — there is no stop to define R). Also report a 10%-allocation overlay for context.
Time-limit safety valve, registered: if neither exit condition occurs within 25 trading days, exit at close (prevents a broken-trend position from riding for months below water; rare — count how often it fires).
C1 — "The Holy Grail" baseline (book p.55)
SPY close > SMA200.
RSI(2) of SPY closes below 5.
Buy at that close.
Exit at the close when SPY closes above its SMA5.
C2 — Cumulative RSI, tight (X=2, Y=35)
SPY close > SMA200.
Cumulative RSI = RSI(2) today + RSI(2) yesterday.
Buy at the close when Cumulative RSI < 35.
Exit at the close when RSI(2) closes above 65.
C3 — Cumulative RSI, loose (X=2, Y=50)

Same as C2 with Y = 50.

Registered note on overlap: C1–C3 are siblings of one idea (short-term oversold in an uptrend), not independent hypotheses. They are NOT combined into one system, and a pass by one is a pass by one — not "the RSI-2 concept confirmed three times." Report the trade-date overlap between variants so the correlation is visible.

Acceptance (each variant): pure signal function, unit-tested: uptrend + qualifying RSI → True; below SMA200 → False; RSI above threshold → False. Exit functions unit-tested (SMA5 cross for C1; RSI > 65 for C2/C3; 25-day valve).

VALIDATION LADDER (per variant, in order)
Run	Window	Fill	Costs
L0	BOOK (1993–2007)	close	off
L1	POST-BOOK (2008–present)	close	off
L2	POST-BOOK	close	on (2bps/side)
L3	POST-BOOK	next open	on
L4	FULL	close	on

L0 is an implementation check, not evidence. If L0's win rate is not within ±10 percentage points of the book's published figure (C1 ≈ 83.6%, C2 ≈ 88%, C3 ≈ 85.5%), suspect the RSI implementation or fill logic BEFORE suspecting the book — debug the harness, don't touch the rules. Report the comparison explicitly.

Report per run: #trades, win rate, avg gain %/trade, avg loss %/trade, worst single trade %, profit factor, max drawdown of the equity curve, avg hold (days), % of days in market, and — mandatory given no stops — performance during 2008-09/2020-02→03/2022 specifically, plus max adverse excursion (worst open-trade drawdown before exit) for the 10 worst trades.

Train/test within POST-BOOK: split at the 70% date boundary; sign of avg gain/trade must hold in both halves.

PASS/FAIL GATE — registered now, judged on L2 (POST-BOOK, close-fill, costs on)
PASS requires ALL of:
Profit factor ≥ 1.3 and avg gain per trade ≥ +0.20% after costs.
≥ 100 trades in POST-BOOK.
Sign holds in both POST-BOOK splits.
Equity-curve max drawdown ≤ SPY buy-and-hold max drawdown over the same window (a no-stop dip-buyer that drawdowns WORSE than just holding SPY has negative value).
Not EXECUTION-FRAGILE: L3 (next-open) profit factor ≥ 1.1.
MARGINAL: positive but misses any bar → not tradable; do not tune; record and close.
FAIL: avg gain/trade ≤ 0 after costs in POST-BOOK.
Note: no survivorship caveat applies (single continuously-listed ETF) — a PASS here is a real PASS on the data, the first fully-trustable verdict available to this project. Remaining caveat is regime dependence: mean reversion's character changed after 2010; the POST-BOOK window exists precisely to measure that.

Attribution questions the report must answer:

L0 vs L1: how much did the edge decay after publication?
L2 vs L3: how much of the edge is the close-fill convention?
Did the 25-day valve ever fire, and what did those trades look like?
C1 vs C2 vs C3: overlap % and whether looser thresholds (more trades) held quality, as the book claims.
Anti-overfitting rules
X and Y are FIXED at the registered values (2/35, 2/50). No sweeps. The book publishes tables of other X/Y combos — implementing them is forbidden under this registration.
No exit swaps, no added filters (no RVOL, no regime variants beyond SMA200), no stop-loss retrofitting "to see."
If all three variants fail, the C-series first registration is closed; any further Connors strategies need a new pre-registered addendum naming ONE strategy and its gate.
Out of scope

Live trading, options, leverage, scale-in versions, the book's other 13 strategies, parameter tables from the book beyond the three registered variants, any instrument other than SPY.

COMMANDS

Step 1: Read this spec. Implement C-0a/b/c with all unit tests passing before any backtest. Report the SPY history coverage and the L0 implementation check for all three variants. STOP there and show me. Step 2 (after my approval): run the full ladder L1–L4 for C1, C2, C3. One report: all tables, gate verdicts per variant, attribution answers, postmortem paragraph. No threshold moves regardless of results.