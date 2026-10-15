H1 — VERDICT: NO-EDGE

First non-zero, correctly-signed result the project has produced. It still fails the pre-registered gate.

What was built

┌───────────────────────────────┬────────────────────────────────────────────────────────────┐
│             File              │                          Purpose                           │
├───────────────────────────────┼────────────────────────────────────────────────────────────┤
│ strategies/panic_reversion.py │ H1 entry — panic detection + limit plan                    │
├───────────────────────────────┼────────────────────────────────────────────────────────────┤
│ core/h1_backtest.py           │ Limit-fillt ladder                │
├───────────────────────────────┼────────────────────────────────────────────────────────────┤
│ tests/test_panic_reversion.py │ 54 tests, all registered acceptance cases                  │
├───────────────────────────────┼───────────────────────────────────┤
│ scripts/run_h1_validation.py  │ The ladder                                                 │
├───────────────────────────────┼────────────────────────────────────────────────────────────┤
│ strategies/filters.py         │ CEF blacklhase 1B postmortem fix) │
└───────────────────────────────┴────────────────────────────────────────────────────────────┘

Universe restored to the 230-name quality lie_universe.json matching HEAD exactly, so the restore is byte-identical to the original. core/swing_setups.py and core/swing_exits.py: 0 modified lines.

HOLX is genuinely gone — yfinance returns "pAPL succeeds in the same call, so it isn'trate limiting. One of the 230 actually delisted. 229 traded.

The ladder

Provenance   SURVIVOR — 229 tickers, 0 delistings
Look-ahead   PASS (49 real closed trades com
Window       2021-08 → 2026-06, 273,654 bars

┌──────────────────────┬─────────┬───────┬──────────┬───────────┬────────────┬───────────┬───────┬───────┐
│         Run          │ #Trades │ Win%  │ AvgWin R │ AvgLoss R │ Expectancy │ Breakeven │  PF   │   t   │
├──────────────────────┼─────────┼───────┼──────────┼───────────┼───────┼───────┤
│ costs OFF            │ 1509    │ 64.6% │ +0.46    │ −0.69     │ +0.049     │ 60.3%     │ 1.200 │ +2.90 │
├──────────────────────┼─────────┼───────┼──────────┼───────────┼───────┼───────┤
│ costs ON (10bps)     │ 1509    │ 63.5% │ +0.44    │ −0.69     │ +0.028     │ 61.0%     │ 1.110 │ +1.64 │
├──────────────────────┼─────────┼───────┼──────────┼───────────┼────────────┼───────────┼───────┼───────┤
│ stress (15bps)       │ 1509    │ 62.8% │ +017     │ 61.3%     │ 1.067 │ +1.01 │
├──────────────────────┼─────────┼───────┼──────────┼───────────┼────────────┼───────────┼───────┼───────┤
│ train (≤ 2025-01-05) │ 1076    │ 63.6% │ +0.43    │ −0.69     │ +0.023     │ 61.5%     │ 1.093 │ +1.18 │
├──────────────────────┼─────────┼───────┼──────────┼───────────┼───────┼───────┤
│ test (> 2025-01-05)  │ 433     │ 63.3% │ +0.47    │ −0.70     │ +0.039     │ 59.9%     │ 1.152 │ +1.20 │
└──────────────────────┴─────────┴───────┴──────────┴───────────┴────────────┴───────────┴───────┴───────┘

┌────────────────────────┬──────────────────────────┐
│       Gate check       │          Result          │
├────────────────────────┼──────────────────
│ ≥ 300 trades           │ PASS — 1,509             │
├────────────────────────┼──────────────────────────┤
│ Sign holds both splits │ PASS — +0.023R / +0.039R │
├────────────────────────┼──────────────────────────┤
│ Expectancy ≥ +0.10R    │ FAIL — +0.028R
├────────────────────────┼──────────────────────────┤
│ Profit factor ≥ 1.2    │ FAIL — 1.110
└────────────────────────┴──────────────────────────┘

0 < expectancy < +0.10R → NO-EDGE, not FAIL.t's just small.

Three reasons it falls short — none tunable

1. 2022 broke it. −0.116R at t = −2.55 — significantly negative in the one real bear market. The regime gate is
registered OFF, so the strategy bought everykes +0.056R; I'm reporting that number onlyto locate the damage, not as a variant — adding a regime gate now would be exactly the post-hoc tuning the
protocol forbids.

2. Capacity kills it. This is the finding I'

┌─────────────────────────────────┬──────┬──
│             Subset              │  n   │ Expectancy │
├─────────────────────────────────┼──────┼────────────┤
│ All signals                     │ 1509 │ +0.028R    │
├─────────────────────────────────┼──────┼──
│ Actually taken (2-position cap) │ 377  │ −0.009R    │
├─────────────────────────────────┼──────┼──
│ Capped out                      │ 1132 │ +0.040R    │
└─────────────────────────────────┴──────┴──

Panics cluster — 19 names can flush on one day, 37% of trades land on days with >5 signals. You get filled on the
first two of a cascade and are locked out of can take are the bad ones. Portfolio sim:75% rejection, −3.0% total return, 9.4% max drawdown. Per-trade expectancy here describes a system you cannot actually run.

3. Survivor data. Zero delistings, so the true number is below +0.028R by an unknown margin. Protocol rule 5 caps this data at MARGINAL anyway — and it didn't reach that.

The registered kill-switch (avg loss > 1.4R) did not fire: avg loss 0.69R. Stops aren't gapping; the SMA5 exit takes many small losses before the stop is reached. Mean 2.94 bars held; 1,169 of 1,509 exits were SMA5.

Test status

283 pass across every suite touching strategies, backtests, exits and dispatch — including 54 new H1 tests.

68 failures elsewhere (test_ml_training, test_monthly_retrain, test_production_quality, test_admin_retrain_cmd,
test_position_pct). Pre-existing, not mine —ose five files in a clean worktree at HEAD:exactly 68 failures there too. None of them import anything H1 touches.

Two notes on method

The look-ahead check compares 49 real closed trades. My first fixture produced only trailing trades, which would have made that assertion vacuously true — the same INCONCLUSIVE result Phase 1B reported. I rebuilt the fixture with repeated mid-series panics and added an assertion that refuses to pass on zero comparisons.

The scratchpad venv was partially deleted by(pandas imported but pandas.__version__raised AttributeError). I rebuilt it before running anything real — a half-gutted environment producing a
plausible-looking number is the same failurete-limit artifact from the Phase 1B run.

Stopping here, as instructed

H1 is closed. Postmortem written to README per the protocol. Nothing deployed, no threshold moved, H2 not started.

The protocol's next hypothesis is H2 (strong-gap continuation) on the 407-name list, and note that its universe is currently in core_universe.json.p1b_final / aced it, so H2 will need its own restorefirst.