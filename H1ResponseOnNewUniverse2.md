H2 — VERDICT: FAIL

The hypothesis generated from our own data is refuted, and it's refuted cleanly.

Universe restore, verified

┌────────────────────┬─────────────────────────────────────────────┐
│                    │                                             │
├────────────────────┼─────────────────────────────────────────────┤
│ Restored from      │ core_universe.json.bak2 → 407 names         │
├────────────────────┼─────────────────────────────────────────────┤
│ With price history │ 405 (JHG, CPRX absent)                      │
├────────────────────┼─────────────────────────────────────────────┤
│ Tradeable          │ 395 after the earnings-coverage requirement │
├────────────────────┼─────────────────────────────────────────────┤
│ Provenance         │ SURVIVOR, 0 delistings                      │
└────────────────────┴─────────────────────────────────────────────┘

The ladder

Look-ahead   PASS (4 closed trades compared, 27 tickers scanned)
Window       2021-08 → 2026-06, 469,829 bars
Slippage     15bps for 222 names <1M median vol, 10bps for 173 above (rule 4)

┌──────────────────────┬─────────┬───────┬──────────┬───────────┬────────────┬───────────┬───────┬───────┐
│         Run          │ #Trades │ Win%  │ AvgWin R │ AvgLoss R │ Expectancy │ Breakeven │  PF   │   t   │
├─────────
│ costs ON (10/15bps)  │ 180     │ 29.4% │ +1.21    │ −0.60     │ −0.069     │ 33.2%     │ 0.838 │ −0.89 │
├──────────────────────┼─────────┼───────┼──────────┼───────────┼────────────┼───────────┼───────┼───────┤
│ stress (15bps flat)  │ 180     │ 28.3% │ +1.17    │ −0.60     │ −0.100     │ 34.0%     │ 0.768 │ −1.35 │
├──────────────────────┼─────────┼───────┼──────────┼───────────┼────────────┼───────────┼───────┼───────┤
│ train (≤ 2024-12-24) │ 139     │ 30.9% │ +1.11    │ −0.57     │ −0.054     │ 34.2%     │ 0.864 │ −0.65 │
├──────────────────────┼─────────┼───────┼──────────┼───────────┼────────────┼───────────┼───────┼───────┤
│ test (> 2024-12-24)  │ 41      │ 24.4% │ +1.65    │ −0.69     │ −0.118     │ 29.4%     │ 0.773 │ −0.61 │
└──────────────────────┴─────────┴───────┴──────────┴───────────┴────────────┴───────────┴───────┴───────┘

┌────────────────────────┬──────────────────────────┐
│       Gate check       │          Result          │
├────────────────────────┼──────────────────────────┤
│ Sign holds both splits │ PASS — reliably negative │
├────────────────────────┼──────────────────────────┤
│ ≥ 300 trades           │ FAIL — 180               │
├────────────────────────┼──────────────────────────┤
│ Expectancy ≥ +0.10R    │ FAIL — −0.069R           │
├────────────────────────┼──────────────────────────┤
│ PF ≥ 1.2               │ FAIL — 0.838             │
└────────────────────────┴──────────────────────────┘

180 is above the 100 sample floor, so this is FAIL, not INSUFFICIENT-SAMPLE.

The foundi is already −0.012R before any costs — so there was no edge to erode; 35 trades selected on the outcome we admired simply weren't evidence. The protocol said that in advance, and it was right.

The exit ladder worked exactly as registered — the SMA10 trail let winners run to +1.21R against −0.60R losers. The payoff is fine. The hit rate needed 33.2% and delivered 29.4%.

Gap size doesn't rescue it either — no monotonic relationship (1.0–1.5 ATR: −0.08R; 1.5–2.5: −0.13R; 2.5–4: −0.03R; >4 ATR: +0.03R on 12 trades).

Concurrency hurts here too, as in H1: 54.4% of signals rejected by the 2-position cap, and the 82 you could actually take made −0.144R versus −0.006R for the 98 you were capped out of. Portfolio: −8.7%, 14.1% max drawdown.

The by-product that matters more than the verdict

H2's rules exclude earnings gaps specifically so it doesn't become H3. Every previous run in this project disabled the earnings gate for want of a calendar, so I built one from EDGAR 8-K item 2.02 filings — free, filed on the day, genuinely point-in-time:

┌─────────────────────────┬───────────────────────────────────────────────────────────────────┐
│                         │                                                                   │
├─────────────────────────┼───────────────────────────────────────────────────────────────────┤
│ Coverage                │ 395/407 (97.1%), 9,913 filings                                    │
├─────────────────────────┼───────────────────────────────────────────────────────────────────┤
│ Full quarterly coverage │ 381 names                                                         │
├───────── signals are rare (0.038% of bars). H1's equivalent compared 49.

Status

Tests: 53 H2 tests pass; 336 across all strategy and backtest suites. The 68 unrelated failures remain pre-existing (verified against a clean worktree at HEAD earlier).

Stopping here as instructed. H2 closed, postmortem written to README, H3 not started.

Two things for you to decide, not for me to assume:
- H3 is now viable. Its data precondition — historical earnings dates for the universe — was the thing that would have closed it as DATA-UNAVAILABLE. It's satisfied at 97.1%.
- core_universe.json currently holds the 407-name list, and a live scan appended 229 rows to core_alert_db.csv while it held H1's 230-name list. The protocol's universe swaps are visible to the running bot; worth deciding which universe is production before the next hypothesis.