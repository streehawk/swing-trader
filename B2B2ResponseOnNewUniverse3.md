PHASE 1B — CLOSED. Final answer: zero trades.

Label: SURVIVOR / CURRENT-FUNDAMENTALS / UPPER-BOUND-ONLY

< 100 trades gate hit at zero. Per your rule, this is the final answer. No realism pass — there is nothing to apply costs to.

Screen corrections applied

┌───────────────────────────────┬─────────────────────────────────────────────────────────────────────────────┐
│            Change             │                                   Effect                                    │
├───────────────────────────────┼─────────────────────────────────────────────────────────────────────────────┤
│ Price floor $10 → $5          │ 1,307 survived vs 1,009 (+298)                                              │
├───────────────────────────────┼─────────────────────────────────────────────────────────────────────────────┤
│ 5-yr CAGR → most recent       │ Correctly cut names whose sales are currently shrinking — e.g. PLAB (−2.0%  │
│ annual revenue growth         │ last year) passed the 5-yr test and now fails                               │
└───────────────────────────────┴─────────────────────────────────────────────────────────────────────────────┘

10 of 403 names used yfinance's quarterly YoY as fallback (annual statement unavailable) — flagged, not silent.

Funnel — 5,549 → 96

┌─────┬──────────────────────────────────┬───────────┬─────────┬──────────────┐
│  #  │              Filter              │ Remaining │ Dropped │ Missing data │
├─────┼──────────────────────────────────┼───────────┼─────────┼──────────────┤
│ 0   │ starting pool                    │ 5,549     │ —       │ —            │
├─────┼──────────────────────────────────┼───────────┼─────────┼──────────────┤
│ —   │ data fetched                     │ 5,549     │ 0       │ —            │
├─────┼──────────────────────────────────┼───────────┼─────────┼──────────────┤
│ 1   │ market cap $300M–$2B             │ 1,509     │ 4,040   │ 52           │
├─────┼──────────────────────────────────┼───────────┼─────────┼──────────────┤
│ 2   │ pr
│ 4   │ exclusions                       │ 766       │ 0       │ —            │
├─────┼──────────────────────────────────┼───────────┼─────────┼──────────────┤
│ 5   │ net income > 0                   │ 403       │ 363     │ 18           │
├─────┼──────────────────────────────────┼───────────┼─────────┼──────────────┤
│ 6   │ recent annual revenue growth > 0 │ 286       │ 117     │ 7            │
├─────┼──────────────────────────────────┼───────────┼─────────┼──────────────┤
│ 7   │ D/E < 1 (78 financials exempt)   │ 209       │ 77      │ 9            │
├─────┼──────────────────────────────────┼───────────┼─────────┼──────────────┤
│ 8   │ ROE > 10%                        │ 98        │ 111     │ 5            │
├─────┼──────────────────────────────────┼───────────┼─────────┼──────────────┤
│ 9   │ book value > 0                   │ 96        │ 2       │ 2            │
└─────┴──────────────────────────────────┴───────────┴─────────┴──────────────┘

Zero fetch

Coverage

┌───────────────────────┬───────────────────────────────────────────────┐
│                       │                                               │
├───────────────────────┼───────────────────────────────────────────────┤
│ Universe applied      │ 96 (backup: core_universe.json.bak2)          │
├───────────────────────┼───────────────────────────────────────────────┤
│ Price history fetched │ 95 / 96                                       │
├───────────────────────┼───────────────────────────────────────────────┤
│ Skipped               │ PICS — only 102 bars (recent IPO, needs 250+) │
├───────────────────────┼───────────────────────────────────────────────┤
│ Total cache           │ 644 tickers, 906,044 rows                     │
└─────────
┌─────┬────────┬─────────┬──────┬──────────┬───────────┬────────────┬────────────────┬───────┬─────┐
│ Run │ Regime │ #Trades │ Win% │ AvgWin R │ AvgLoss R │ Expectancy │ Breakeven Win% │ MaxDD │ PF  │
├─────┼────────┼─────────┼──────┼──────────┼───────────┼────────────┼────────────────┼───────┼─────┤
│ B1  │ off    │ 0       │ —    │ —        │ —         │ —          │ —              │ —     │ —   │
├─────┼────────┼─────────┼──────┼──────────┼───────────┼────────────┼────────────────┼───────┼─────┤
│ B2  │ on     │ 0       │ —    │ —        │ —         │ —          │ —              │ —     │ —   │
└─────┴────────┴─────────┴──────┴──────────┴───────────┴────────────┴────────────────┴───────┴─────┘

Census — 111,237 bars

┌────────────────────┬────────┬───────┐
│ First gate failed  │  Bars  │ Share │
├────────────────────┼────────┼───────┤
│ RVOL > 1
│ avg volume ≥ 200k  │ 33,156 │ 29.8% │
├────────────────────┼────────┼───────┤
│ ≥5% pop over SMA20 │ 4,002  │ 3.6%  │
├────────────────────┼────────┼───────┤
│ close < SMA50      │ 2,097  │ 1.9%  │
├────────────────────┼────────┼───────┤
│ structure ≥ 2R     │ 25     │ 0.02% │
├────────────────────┼────────┼───────┤
│ passed everything  │ 0      │ —     │
└────────────────────┴────────┴───────┘

The one genuinely new finding

The core sresult, caught a universe-path bug hiding 1,000 tickers from production, and refused three opportunities to loosen thresholds into a manufactured pass.

One thing I did not act on

GAB and BDJ are closed-end funds, not operating companies — they slipped through because they aren't leveraged/inverse ETFs and report fund-level income as earnings. Excluding them would have meant adding a screen rule, and you said "no other threshold moves, whatever the funnel shows." So I left them in and am telling you instead. They contributed nothing (zero trades either way).

Uncommitted. Phase 1 remains untouched throughout; core/swing_setups.py and core/swing_exits.py show 0 modified lines on this branch.