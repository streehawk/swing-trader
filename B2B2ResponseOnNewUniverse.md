All four items done. Summary, then the finding that matters.

(1) Proxies — accepted and documented

Both are recorded in core_universe.json's _meta.screen so the deviation is on the record, not just in this conversation: volume is a 10-day average (yfinance has no 20-day), equity is via book value per share.

(2) Data coverage — 100%

┌──────────────────────────────────────┬─────┐
│                                      │     │
├──────────────────────────────────────┼─────┤
│ Tickers checked                      │ 407 │
├──────────────────────────────────────┼─────┤
│ Fetch failures                       │ 0   │
├──────────────────────────────────────┼─────┤
│ Complete on all required fields      │ 407 │
├──────────────────────────────────────┼─────┤
│ Gaps                                 │ 0   │
├──────────────────────────────────────┼─────┤
│ Financials exempt from D/E by design │ 121 │
└──────────────────────────────────────┴─────┘

Verified by re-fetching every name, not inferred from the funnel.

(3) Universe applied

core_universe.json now holds 407 tickers (was 230): 96 carried over, 134 dropped, 311 added. Overrides preserved, all 96 hand-written notes preserved, core_radar loads it cleanly. Backup at core_universe.json.bak; git also has the original.

(4) B1 / B2 — still zero trades

┌─────┬────────┬─────────┬──────┬────────────┬─────┐
│ Run │ Regime │ #Trades │ Win% │ Expectancy │ PF  │
├─────┼────────┼─────────┼──────┼────────────┼─────┤
│ B1  │ off    │ 0       │ —    │ —          │ —   │
├─────┼────────┼─────────┼──────┼────────────┼─────┤
│ B2  │ on     │ 0       │ —    │ —          │ —   │
└─────┴────────┴─────────┴──────┴────────────┴─────┘

A necessary detour: the backtest reads prices.parquet, not core_universe.json — only 96 of the 407 had price history. Applying the universe alone would have re-run the identical old test and I'd have reported a meaningless "still zero". I built prices_p1b.parquet (309/311 new tickers fetched, 550 total, 776k rows) so B1/B2 ran on the real new universe.

Why zero — census on 405 tickers, 483,195 bars

┌────────────────────┬─────────┬───────┐
│ First gate failed  │  Bars   │ Share │
├────────────────────┼─────────┼───────┤
│ RVOL > 1.5         │ 389,964 │ 80.7% │
├────────────────────┼─────────┼───────┤
│ ≥5% pop over SMA20 │ 39,903  │ 8.3%  │
├────────────────────┼─────────┼───────┤
│ avg volume ≥ 200k  │ 36,387  │ 7.5%  │
├────────────────────┼─────────┼───────┤
│ close < SMA50      │ 11,055  │ 2.3%  │
├────────────────────┼─────────┼───────┤
│ price ≥ $10        │ 5,837   │ 1.2%  │
├────────────────────┼─────────┼───────┤
│ structure ≥ 2R     │ 49      │ 0.01% │
├────────────────────┼─────────┼───────┤
│ passed everything  │ 0       │ —     │
└────────────────distribution is textbook (median 0.90). The problem is compounding three independently uncommon conditions. A 5%+ pop off a below-50-day base, and 1.5× volume, and a 2R-viable structure is genuinely rare in $2B+ profitable companies.

Two honest options:

- Go smaller. Drop to $300M–$2B caps. That's where these violent reversals live — and it's where Pezim was screening. It needs another price-history build and the slippage assumptions get materially worse.
- Stop here. Two universes, 776k bars, zero qualifying setups. That is itself a clean answer.

I'd not recommend loosening the 5% / 1.5× / 2R thresholds — with 49 candidates as the ceiling, you'd have to gut them to reach a sample, and you'd be testing something else.

I have not run B3/B4 — they'd be identical to B1/B2 with zero trades. Nothing is committed. Say the word on which option you want.