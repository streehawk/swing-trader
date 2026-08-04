C-Series Step 2 — C2 and C3 PASS. The project's first.

The full ladder

┌─────┬────────┬─────┬─────┬──────┬──────┬─────┬──────┬──────┬───────┬──────┬─────┬──────┬─────┬─────┬─────┐
│     │        │     │     │      │      │     │      │      │       │      │     │      │     │ %Da │     │
│ Run │ Window │ Fil │ Cos │ Vari │ #Tra │ Win │ Avg% │ AvgW │ AvgLo │ Wors │ PF  │ MaxD │ Hol │ ys  │  t  │
│     │        │  l  │ ts  │ ant  │ des  │  %  │      │ in%  │  ss%  │  t%  │     │  D%  │  d  │ in  │     │
│     │        │     │     │      │      │     │      │      │       │      │     │      │     │ mkt │     │
├─────┼────────┼─────┼─────┼──────┼──────┼─────┼──────┼──────┼───────┼──────┼─────┼──────┼─────┼─────┼─────┤
│ L1  │ POST-B │ clo │ off │ C1   │ 80   │ 77. │ +0.5 │ +1.2 │ −1.78 │ −10. │ 2.4 │ 11.6 │ 3.3 │ 5.6 │ +2. │
│     │ OOK    │ se  │     │      │      │ 5%  │ 69   │ 5    │       │ 16   │ 23  │      │     │ %   │ 78  │
├─────┼────────┼─────┼─────┼──────┼──────┼─────┼──────┼──────┼───────┼──────┼─────┼──────┼─────┼─────┼─────┤
│ L1  │        │
├─────┼────────┼─────┼─────┼──────┼──────┼─────┼──────┼──────┼───────┼──────┼─────┼──────┼─────┼─────┼─────┤
│ L2  │ POST-B │ clo │ 2bp │ C1   │ 80   │ 77. │ +0.5 │ +1.2 │ −1.81 │ −10. │ 2.2 │ 11.8 │ 3.3 │ 5.6 │ +2. │
│     │ OOK    │ se  │ s   │      │      │ 5%  │ 28   │ 1    │       │ 20   │ 94  │      │     │ %   │ 58  │
├─────┼────────┼─────┼─────┼──────┼──────┼─────┼──────┼──────┼───────┼──────┼─────┼──────┼─────┼─────┼─────┤
│ L2  │        │     │     │ C2   │ 146  │ 74. │ +0.4 │ +1.1 │ −1.57 │ −9.5 │ 2.0 │ 12.0 │ 3.6 │ 11. │ +3. │
│     │        │
│ L3  │        │     │     │ C3   │ 207  │ 69. │ +0.3 │ +1.1 │ −1.30 │ −8.7 │ 1.9 │ 10.6 │ 3.7 │ 16. │ +3. │
│     │        │     │     │      │      │ 1%  │ 85   │ 4    │       │ 7    │ 61  │      │     │ 5%  │ 41  │
├─────┼────────┼─────┼─────┼──────┼──────┼─────┼──────┼──────┼───────┼──────┼─────┼──────┼─────┼─────┼─────┤
│     │ FULL   │ clo │ 2bp │      │      │ 79. │ +0.7 │ +1.3 │       │ −10. │ 3.1 │      │     │ 5.1 │ +4. │
│ L4  │ 1993–2 │ se  │ s   │ C1   │ 129  │ 1%  │ 11   │ 2    │ −1.59 │ 20   │ 37  │ 11.8 │ 3.3 │ %   │ 65  │
│     │ 026    │
│ L4  │        │     │     │ C3   │ 371  │ 76. │ +0.4 │ +1.0 │ −1.37 │ −9.5 │ 2.4 │ 12.2 │ 3.7 │ 16. │ +6. │
│     │        │     │     │      │      │ 3%  │ 61   │ 3    │       │ 9    │ 17  │      │     │ 7%  │ 17  │
└─────┴────────┴─────┴─────┴──────┴──────┴─────┴──────┴──────┴───────┴──────┴─────┴──────┴─────┴─────┴─────┘

Look-ahead re-verified on real SPY data: C1 129 trades compared, C2 264, C3 371 — all PASS.


All three hold sign, and — unusually — the later half is as good or better in every case.

Gate verdicts (judged on L2)

┌───────────────────
│ Sign holds both splits              │ ✅              │ ✅              │ ✅              │
├─────────────────────────────────────┼─────────────────┼─────────────────┼─────────────────┤
│ DD ≤ buy-and-hold DD                │ ✅ 11.8 vs 51.9 │ ✅ 12.0 vs 51.9 │ ✅ 12.2 vs 51.9 │
├─────────────────────────────────────┼─────────────────┼─────────────────┼─────────────────┤
│ Not execution-fragile (L3 PF ≥ 1.1) │ ✅ 2.004        │ ✅ 2.180        │ ✅ 1.961        │
├─────────────────────────────────────┼─────────────────┼─────────────────┼─────────────────┤
│ VERDICT                             │ MARGINAL        │ PASS            │ PASS            │
└─────────────────────────────────────┴─────────────────┴─────────────────┴─────────────────┘

C1 misses on one bar only — the trade-count floor — while being the strongest per trade. Per the spec that is "not tradable; do not tune; record and close." It does not get rescued.

│ C2      │ +0.814%        │ +0.436%            │ −46%  │ 83.0% → 74.7% │
├─────────┼────────────────┼────────────────────┼───────┼───────────────┤
│ C3      │ +0.660%        │ +0.336%            │ −49%  │ 81.1% → 73.0% │
└─────────┴────────────────┴────────────────────┴───────┴───────────────┘

Losses also got fatt
├─────────┼────────────┼────────────────┼────────┤
│ C1      │ +0.528%    │ +0.450%        │ −0.078 │
├─────────┼────────────┼────────────────┼────────┤
│ C2      │ +0.436%    │ +0.462%        │ +0.026 │
├─────────┼────────────┼────────────────┼────────┤
│ C3      │ +0.336%    │ +0.385%        │ +0.049 │
└─────────┴────────────┴────────────────┴────────┘

C2 and C3 do better waiting for the next open. That is the opposite of the usual finding and it kills the main objection to the book's convention — the edge is not an artifact of buying the exact close. C1 does lose about 15% of its edge, consistent with it firing on the sharpest one-day washouts where the overnight bounce matters.

3. Did the 25-day valve ever fire? Never — zero times, in every variant, in every run, across 4,672 trades. Every trade exited on its own signal. The exit rules resolve on their own; the valve is dead weight that cost nothing to carry.
└──────────────────┴────────────────────┴─────────┘

Loosening Y from 35 → 50 buys 42% more trades (146 → 207) and pays 23% of the per-trade edge (+0.436% → +0.336%) plus 1.7pp of win rate. The book's claim that looser thresholds hold quality is directionally supported but overstated — quality degrades smoothly. And these are only 33% overlapping, so C2 and C3 are less redundant than I expected — but still
│ Variant │    2008-09 crisis    │ 2020 COVID │ 2022 bear │
├─────────┼──────────────────────┼────────────┼───────────┤
│ C1      │ 3t, +2.07%/t, +6.3%  │ 1t, −4.17% │ 2t, −2.5% │
├─────────┼──────────────────────┼────────────┼───────────┤
│ C2      │ 8t, +1.54%/t, +13.0% │ 1t, −4.17% │ 2t, −2.9% │
├─────────┼──────────────────────┼────────────┼───────────┤
│ C3      │ 9t, +1.41%/t, +13.4% │ 1t, −7.35% │ 3t, −5.0% │
└─────────┴──────────────────────┴────────────┴───────────┘

The SMA200 filter did its job: the strategies were mostly out during the worst of each episode, and 2008-09 was their best period.

Worst tail, C2/C3: 2011-07-27, −9.6% closed, −15.6% max adverse excursion — 12 days underwater during the US downgrade. COVID: C3 took −7.4% with a −14.4% MAE. With no stop, position sizing is the entire risk control.


The C-series is the first thing in this project to survive its own gate, and the reason is the data situation rather than the cleverness of the strategy: one continuously-listed instrument removes survivorship bias, signal clustering, capacity limits and slippage guesswork all at once — every problem that made Phase 1, Phase 1B, H1 and H2 uninterpretable or fatal. C2 and C3 pass every registered bar on 18.6 years of true out-of-sample data, with t-statistics above 3, sign holding in both split halves, and no dependence on the close-fill convention. What passed is narrow and should be described narrowly: a short-term mean-reversion edge worth roughly +0.34 to +0.44% per trade, active a tenth of the time, that halved after publication and would halve again if it keeps decaying at the same rate. It does not beat owning SPY; it beats owning SPY per unit of drawdown, modestly. C1 fails the trade-count floor and is closed without appeal. Per the registration, the C-series' first three variants are now spent — any further Connors strategy requires a new pre-registered addendum naming one strategy and its gate, and the 13 untested strategies in that book are exactly the data-mining trap the spec was written to prevent.

Nothing was tuned, no threshold moved, and nothing is deployed. Tests: 52 C-series, 377 across all suites. Universe files untouched — the C-series trades SPY only. H2 remains closed as FAIL; H3 (now viable on the EDGAR earnings calendar) and H4 are still queued.