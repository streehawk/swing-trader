STEP 1 — 76 survivors. Below your ~150 stop rule. Stopping.

core_universe.json verified unchanged. Nothing applied.

Constituent source — one substitution

iShares IWM returned an HTML page, not CSV — the endpoint is blocked or the product ID is stale. I substituted the Nasdaq Trader symbol directory (nasdaqlisted.txt + otherlisted.txt): free, no auth, and broader than Russell 2000 — it's every listed US common stock.

Filtered to common stock only (dropped ETFs, test issues, warrants, units, rights, preferreds), merged with the existing pool and deduped: 5,549 tickers, +4,017 new.

Cap band population — the fix worked

┌───────────────────┬───────────────────┬───────────────────────┐
│                   │ Before (S&P 1500) │ After (full listings) │
├───────────────────┼───────────────────┼───────────────────────┤
│ In band $300M–$2B │ 170               │ 1,514                 │
├───────────────────┼───────────────────┼───────────────────────┤
│ Below $300M       │ 3                 │ 2,016                 │
├───────────────────┼───────────────────┼───────────────────────┤
│ Above $2B         │ 1,246             │ 1,967                 │
└───────────────────┴───────────────────┴───────────────────────┘

8.9× more in-band companies. The structural problem is solved — the pool now genuinely covers small caps.

The funnel

┌─────┬─────────────────────────┬───────────┬─────────┬───────────┬──────────────┐
│  #  │         Filter          │ Remaining │ Dropped │ % of pool │ Missing data │
├─────┼─────────────────────────┼───────────┼─────────┼───────────┼──────────────┤
│ 0   │ starting pool
├─────┼─────────────────────────┼───────────┼─────────┼───────────┼──────────────┤
│ —   │ data fetched            │ 5,549     │ 0       │ 100%      │ —            │
├─────┼─────────────────────────┼───────────┼─────────┼───────────┼──────────────┤
│ 1   │ market cap $300M–$2B    │ 1,514     │ 4,035   │ 27.3%     │ 52           │
├─────┼─────────────────────────┼───────────┼─────────┼───────────┼──────────────┤
│ 2   │ price ≥ $10             │ 1,009     │ 505     │ 18.2%     │ —            │
├─────┼─────────────────────────┼───────────┼─────────┼───────────┼──────────────┤
│ 3   │ avg volume ≥ 200k       │ 528       │ 481     │ 9.5%      │ 2            │
├─────┼─────────────────────────┼───────────┼─────────┼───────────┼──────────────┤
│ 4   │ exclusions              │ 528       │ 0       │ 9.5%      │ —            │
├─────┼─────────────────────────┼───────────┼─────────┼───────────┼──────────────┤
│ 5   │ net income > 0          │ 305       │ 223     │ 5.5%      │ 15           │
├─────┼─────────────────────────┼───────────┼─────────┼───────────┼──────────────┤
│ 6   │ 5-yr revenue growth > 0 │ 196       │ 109     │ 3.5%      │ 22           │
├─────┼─────────────────────────┼───────────┼─────────┼───────────┼──────────────┤
│ 7   │ D/E < 1 (fin. exempt)   │ 146       │ 50      │ 2.6%      │ 3            │
├─────┼─────────────────────────┼───────────┼─────────┼───────────┼──────────────┤
│ 8   │ ROE > 10%               │ 78        │ 68      │ 1.4%      │ 2            │
├─────┼─────────────────────────┼───────────┼─────────┼───────────┼──────────────┤
│ 9   │ book value > 0          │ 76        │ 2       │ 1.4%      │ 1            │
└─────┴─────────────────────────┴───────────┴─────────┴───────────┴──────────────┘

Your call

1. Proceed at 76 — accept the smaller sample; if B1/B2 yield < 100 trades it's the final answer anyway, which your Step 3 rule already covers.
2. Stop here — 76 < 150, declaration stands, Phase 1B closed.

I won't loosen any threshold to reach 150 — you forbade it and it would invalidate the test.

One thing I'd flag either way: at 76 names, "< 100 trades" is close to guaranteed, so option 1 most likely ends at the same place as option 2 — just with the evidence in hand rather than inferred. That's worth roughly 30 minutes of compute, and nothing else.