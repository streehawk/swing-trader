# Phase 1 — Entry Filter Spec (v2)

> **Note:** this file arrived empty (0 bytes). It has been reconstructed from
> **Stock Swing Trading Plan v2, Section 4** (the "Entry Checklist"), which is
> the authoritative source. If you have the original Writeup 1, diff it against
> this and tell me about any difference — the code implements what is below.

Implemented in `core/swing_setups.py`. Every gate is checked **in order**; any
failure means **no trade**. This is what turns "uptrend + a pause" from a coin
flip into a filtered setup.

---

## The seven gates

### 1. Market regime — *hard gate*
SPY must close **above both its 50-day and 200-day SMA**.

A hostile market overrides any setup, no matter how clean. When this gate is
shut, the universe sweep is skipped entirely rather than run to produce a list
nobody may act on. Existing positions are still managed by their stops.

**Fails closed:** if SPY data is unavailable, the answer is "no new longs",
never "proceed". A regime we cannot verify is not a regime we may trade into.

*Code:* `market_regime_ok()` · env `SWING_REGIME_GATE=0` disables (testing only)

### 2. Universe
| Rule | Threshold |
|---|---|
| Minimum price | ≥ $10 |
| Average daily volume | ≥ 1,000,000 **shares** (20-day mean) |
| Permanent exclusions | `PLTR` (extendable via `SWING_EXCLUDED_TICKERS`) |
| Allowed instruments | large-cap US stocks, major ETFs |
| Forbidden | penny stocks, leveraged/inverse ETFs |

**Fails closed** on unknown volume: a stop we cannot prove is fillable is not a
risk plan.

### 3. Earnings
No earnings report **within the next 10 calendar days**, and **never hold
through a report**.

Because the holding window is 15 trading days (≈21 calendar), the code rejects
any name reporting inside that window — which is the stricter of the two rules
and therefore satisfies both.

*Code:* `_enrich()` — runs only on names that passed every price gate, since it
is the one gate needing a network lookup.

### 4. Genuine uptrend
- `close > SMA50 > SMA200`
- `SMA50` rising versus its value 20 bars ago

### 5. Relative strength
The stock's return over the last **63 trading days (~3 months)** must exceed
SPY's over the same period. **Buy leaders, not laggards.**

Required, not optional: if SPY is unavailable the setup is rejected rather than
passed with the gate skipped.

### 6. Valid pause — not a breakdown
All three must hold:

| Condition | Rule |
|---|---|
| Depth | pullback of **1–3 × ATR(14)** from the 20-day high (volatility-adjusted, not a fixed %) |
| Volume | pause average volume **≤** the prior 20-day average — selling pressure drying up, not distribution |
| Structure | **no close below SMA50** during the pause (checked on closes, so an intraday wick is forgiven) |

The pause window is located by finding the lowest low of the last 10 bars; the
pause runs from there to the current bar.

### 7. Structure supports ≥ 2R
```
entry  = pause high × 1.001              (buy-stop trigger)
limit  = entry × 1.002                   (slippage cap)
stop   = min(pause low × 0.999, entry − 1 × ATR)
risk   = entry − stop                    ( = 1R )
target = entry + 2 × risk
```

Rejected when:
- `risk / entry > 8%` — too volatile for the risk model
- `target − entry > 4 × ATR` — a 2R move is not plausible in 15 sessions, so
  the 2:1 target is not *structurally realistic*. **Never force it.**
- `R:R < 2.0`

---

## Removed in v2: BREAKOUT setups

v1 had a second setup type that bought breakouts from tight ranges. It is
**gone**, per the plan's own journal entry (AAPL, 2026-07-07):

> "a breakout chased the day after a strong run is prone to shakeout. Fix now
> codified in Section 4: wait for the pullback entry instead of chasing the
> high."

`PULLBACK` is the only setup type the engine emits.

---

## Why these gates exist (the v1 failure)

The v1 engine was replayed over 5.5 years and 4,193 trades. Result:
**expectancy −0.017R, win rate 34.5%** against a 35.1% breakeven — a coin flip
that paid costs. It failed its pre-registered gate on five of six criteria.

Reading the failure onto the gates above:

| v1 weakness | v2 gate that addresses it |
|---|---|
| No regime filter → bought all through 2022's downtrend (worst year, −0.245R) | Gate 1 |
| No relative-strength filter → bought laggards as readily as leaders | Gate 5 |
| R:R floor of only 1.5 → needed a ~40% hit rate it never achieved | Gate 7 (2.0) |
| Pullback band 1–4 ATR → admitted deep, broken-looking pullbacks | Gate 6 (1–3) |
| No volume or SMA50 condition on the pause | Gate 6 |
| Chased breakouts | BREAKOUT removed |

**These thresholds come from the written plan, not from fitting.** Per the
plan's own guardrail: *"No tuning rules mid-stream to chase a result."* v2 is
re-validated by `scripts/run_swing_validation.py` against the same
pre-registered gate that failed v1 — the gate was not relaxed.

**v2 result: still REVERT.** Expectancy −0.023R over 741 trades, win rate 30.2%
against a 31.2% breakeven. The filters cut 82% of trades and neutralised 2022,
but did not move expectancy off zero. Full detail in the README.

---

## Ticket P1-7 — status

| Item | Status | Where |
|---|---|---|
| 1. Survivorship | ⚠️ **Fallback taken** (no delisted dataset exists here) | `core/swing_backtest.universe_provenance()` |
| 2. Costs + slippage | ✅ Done | `core/swing_backtest.CostModel` |
| 3. No look-ahead | ✅ Verified at runtime | `core/swing_backtest.confirm_no_lookahead()` |
| 4. Hold else constant | ✅ Done, one caveat | `config.exits` in the run JSON |

**Item 1 detail.** A true point-in-time universe is not obtainable from the
repo: `prices.parquet` has 241 tickers and **zero delistings**, and its own
provenance records `survivorship_inflation: true`. The ticket's fallback
clause was taken — *"at minimum document the bias and treat any positive
result as an upper bound"* — and hardened beyond prose:

- the universe type is **detected empirically** (count series ending early),
  not read from a label somebody could forget to update;
- it is printed at the top of every run and stored in the output JSON;
- `evaluate_gate()` downgrades any GO on a survivor universe to
  `GO_UPPER_BOUND_ONLY`, so a positive result cannot be read as an edge.

Closing it properly needs CRSP / Sharadar / Norgate (all paid). Say the word
and I will wire whichever you subscribe to.

**Item 4 caveat.** A like-for-like v1-vs-v2 *entry* comparison is not possible
— the v1 entry code was replaced rather than kept behind a flag. The controlled
comparison that IS available is the regime-gate ablation (everything else
identical): **+0.066R attributable to plan rule 11 alone.**
