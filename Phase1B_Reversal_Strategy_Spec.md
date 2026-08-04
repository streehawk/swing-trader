# Phase 1B — "Dip in a Long-Term Uptrend" Strategy Spec + Backtest Protocol

**Purpose:** Test a *second, separate* entry strategy against the existing trend-pullback system (Phase 1). This is NOT a modification of Phase 1 — it is a competing hypothesis. Adapted from Brian Pezim, *How To Swing Trade* (p. 219): scan for a stock that has popped **above its 20-day SMA while still below its 50-day SMA, but is still above its 200-day SMA** — i.e. a stock in a long-term uptrend that took a real correction and is now turning back up.

**Note on the source:** Pezim's book text describes a *deep* reversal (below both the 50 AND 200-day). Per user decision, we are NOT testing that version. We test only the "dip inside a long-term uptrend" variant (`close > SMA200`), which was the setting shown in his Finviz screenshot and is the more defensible setup.

**How this differs from Phase 1.** Phase 1 buys leaders still above their 50-day (`close > SMA50 > SMA200`). Phase 1B buys names that have dipped *below* their 50-day but held *above* their 200-day (`SMA200 < close < SMA50`) and are turning back up (`close > SMA20 * 1.05`). A stock cannot pass both at once, so keep them as two distinct strategies selectable by a flag — never merge them, or the gates cancel out and nothing is emitted.

**Hard guardrails (unchanged):** analysis/backtest only. No brokerage connection, no live order placement. PLTR permanently excluded. Equities only. Hold sizing (Phase 2) and exits (Phase 3) constant so any difference vs Phase 1 is attributable to the entry signal alone.

---

## TICKET P1B-1 — Dip-in-uptrend trend filter (the core signal)

**Rule — a symbol is a candidate on the signal day if ALL are true:**
- `close >= SMA20 * 1.05` — price is **≥ 5% above the 20-day SMA** (the pop that signals the turn back up).
- `close < SMA50` — currently **below** the 50-day (it took a real dip out of the short-term trend).
- `close > SMA200` — still **above** the 200-day (the long-term uptrend is intact — this is a correction, not a broken trend).

**Acceptance:** pure function `is_dip_in_uptrend(bars) -> bool`; unit-tested with: price 6% above SMA20, below SMA50, above SMA200 (True); price below SMA20 (False); price above SMA50 (False); price below SMA200 (False).

---

## TICKET P1B-2 — Liquidity filter: average volume (NEW, user-specified)

**Rule:**
- 20-day average daily volume `>= 200,000` shares. (User's swing-trade minimum — looser than Phase 1's 1M because these candidates can be smaller names; keep it as its own tunable constant `MIN_AVG_VOL = 200_000`.)
- Keep the existing exclusions too: `price >= $10`, not on the ban list (PLTR), no leveraged/inverse ETFs.

**Note:** This 200k floor is deliberately lower than Phase 1's 1M. Fine for a separate strategy, but thinner names mean worse slippage — the cost model in P1B-6 must be applied honestly, not skipped.

**Acceptance:** `passes_avg_volume(bars, min_avg_vol=200_000) -> bool`; unit-tested at 150k (False), 250k (True). Missing volume history → False (fail closed, never guess).

---

## TICKET P1B-3 — Relative volume filter (NEW, user-specified)

**Rule:**
- Relative volume = `today_volume / avg_volume_over_prior_N_days`. Use `N = 50` prior trading days (standard "RVOL" denominator; make `RVOL_LOOKBACK` a constant).
- Require `RVOL > 1.5` on the signal day — the turn-up must come on **above-average participation** (conviction), not a low-volume drift.

**Acceptance:** `passes_relative_volume(bars, min_rvol=1.5, lookback=50) -> bool`; unit-tested: today 2× the 50-day avg (True, RVOL≈2.0), today at the average (False, RVOL≈1.0). Guard divide-by-zero and short history (fail closed).

---

## TICKET P1B-4 — Earnings gate (unchanged from Phase 1)

- Reject if the company reports earnings within the next 10 calendar days, and never hold through a report. Identical logic to Phase 1's earnings gate — reuse it, don't reimplement.

**Acceptance:** reuse existing `passes_earnings_gate`; one integration test confirming a name reporting in 3 days is rejected.

---

## TICKET P1B-5 — Entry trigger tied to structure

- **Trigger (stop-entry):** buy only if price rises through the recent swing high of the turn-up move + small buffer (+0.1%). Confirmation the bounce is continuing, not a one-day head-fake.
- **Stop:** below the recent swing low OR `entry - 1*ATR(14)`, whichever is the more sensible structure; `risk_per_share = entry - stop`.
- **Target:** `entry + reward_multiple * risk_per_share`, `reward_multiple >= 2.0`.
- If a ≥2R target isn't structurally realistic, **reject** — don't force it.
- Keep the same time stop (close after 15 trading days if neither hit) and break-even ratchet as Phase 1 (these live in Phase 3 — do not change them).

**Acceptance:** given a fixture dip, returns `{entry, stop, target, risk_per_share}` with target ≥ 2R; rejects when structure can't support 2R.

---

## TICKET P1B-6 — Master gate (cheap checks first, short-circuit)

**Emit a candidate ONLY if ALL pass, in this order:**
1. Exclusions: price ≥ $10, not PLTR/leveraged/inverse. (cheapest)
2. `passes_avg_volume` (≥ 200k).
3. `passes_relative_volume` (RVOL > 1.5).
4. `is_dip_in_uptrend` (≥5% above SMA20, below SMA50, above SMA200).
5. `passes_earnings_gate` (no earnings ≤ 10 days).
6. Structure supports ≥ 2R (P1B-5) → emit `{entry, stop, target, shares}`.

**Note on market regime:** Phase 1 uses an SPY>200SMA gate. For this strategy the gate is philosophically ambiguous (dips often turn up while the broad tape is still soft). So run this **without** the regime gate as the base case, and **with** it as one comparison run (B2 below) to measure its effect — let the data decide, don't assume.

**Acceptance:** integration test — a symbol passing all emits a full plan; failing any one emits nothing; order short-circuits.

---

## TICKET P1B-7 — Backtest integrity (REQUIRED — same as Phase 1, non-negotiable)

1. **Survivorship bias — FATAL, fix first.** Point-in-time universe including delisted/dead tickers. **This matters even MORE here:** buying names that have dipped below their 50-day catches some that keep falling and eventually delist — exactly the names a "healthy-today" list deletes. A survivor-only backtest of this strategy is dangerously optimistic. If no true delisted dataset, treat any positive result as an upper bound and say so.
2. **Costs + slippage.** Commission + adverse slippage per side (≥ 0.05–0.15% or 1 tick, whichever larger). Apply the **larger** slippage assumption here given the 200k-volume (thinner) names.
3. **No look-ahead.** All inputs (SMA20/50/200, RVOL, avg vol, swing high/low) computed as of the signal day's close; fill next day at/through the trigger.
4. **Hold everything else constant.** Same sizing (0.75% backtest), same Phase-3 exits, same 15-day time stop. Only the entry differs.

**Acceptance:** run config logs universe type (PIT vs survivor), cost model, slippage bps, no-look-ahead confirmation, and whether the regime gate is on.

---

## VALIDATION PROTOCOL — does this strategy have an edge?

Run as an ordered experiment; report ONE table. **Do not tune to pass.** Run the pre-registered configs, then judge against the gate.

**Runs (full 5.5y, same sizing/exits as Phase 1):**
| Run | Strategy config | Universe | Costs |
|---|---|---|---|
| B0 | **Phase 1** (trend-pullback, your current system) — the benchmark | point-in-time | costs+slippage |
| B1 | Phase 1B (dip-in-uptrend), **no** regime gate | survivor | none |
| B2 | Phase 1B + **SPY>200SMA regime gate** | survivor | none |
| B3 | Better of B1/B2 | **point-in-time (delisted incl.)** | none |
| B4 | Best config so far | point-in-time | **costs+slippage** |

**Report per run:** #trades, win rate, avg winner (R), avg loser (R), **expectancy (R/trade)**, breakeven win rate, max drawdown, profit factor.

**Expectancy:** `E = winrate*avgWin_R - (1-winrate)*avgLoss_R`. Breakeven win rate = `avgLoss/(avgWin+avgLoss)`.

**PASS/FAIL GATE (pre-registered — decide BEFORE looking, no goalpost-moving):**
- **PASS** only if the realistic run **B4** (point-in-time + costs) shows **expectancy ≥ +0.10R/trade**, **#trades ≥ 300**, and **profit factor ≥ 1.2** — AND beats the Phase 1 benchmark B0 on expectancy. A new strategy must clear the bar *and* be better than what you already have.
- **MARGINAL** if B4 expectancy is 0 to +0.10R, or positive but below B0 — no reliable/relative edge. Do not deploy, do not tune to force it.
- **FAIL** if B4 expectancy ≤ 0. Stop. The strategy has no retail-accessible edge in your universe.

**Attribution:** B1 vs B2 → does the market-regime gate help this strategy or hurt it. B2→B3 → survivorship illusion (expect a big drop). B3→B4 → cost drag (expect large, given thin names).

**Anti-overfitting (same rules as Phase 1):**
- Run pre-registered configs only. No threshold sweeping to chase a pass (don't fiddle the 5% / 1.5 RVOL / 200k to make it work).
- If optimizing, do it on a **train split (first ~70% of dates)**, confirm on a **held-out test split (last ~30%)**; report both. In-sample-only edge is not an edge.
- Report honest results even if they FAIL. A clean "no edge" is a successful test.

---

## THE COMMANDS TO RUN (paste into Claude in your IDE)

**Step 1 — implement the spec:**
```
Read Phase1B_Reversal_Strategy_Spec.md and CLAUDE.md. Implement tickets P1B-1
through P1B-7 as a SEPARATE strategy module (e.g. strategies/dip_in_uptrend.py),
selectable by a --strategy flag. Do NOT modify the existing Phase 1
trend-pullback entry. Add the two new filters (avg volume >= 200k, RVOL > 1.5)
as reusable, unit-tested functions. Write the unit tests specified in each
ticket's Acceptance section and make them pass before running any backtest.
```

**Step 2 — run the validation experiment:**
```
Execute the Phase 1B VALIDATION PROTOCOL exactly as written: runs B0 through B4,
holding sizing and exits constant. Do not tune any threshold to chase a pass.
Produce the single results table with all required columns (#trades, win rate,
avg winner R, avg loser R, expectancy, breakeven win rate, max drawdown, profit
factor) and then state the PASS / MARGINAL / FAIL verdict against the
pre-registered gate, including whether B4 beats the Phase 1 benchmark B0.
```

**Illustrative CLI form** (adapt flag names to however your harness is actually wired — Claude should map these to the real argparse options, not invent new behavior):
```bash
# Benchmark: your current system, realistic
python backtest.py --strategy trend_pullback --universe pit --costs on \
  --start 2021-01-01 --end 2026-07-01 --risk 0.0075 --report B0

# Dip-in-uptrend, exploratory (survivor, no costs) — upper bounds only
python backtest.py --strategy dip_in_uptrend --regime off \
  --universe survivor --costs off --min-avg-vol 200000 --min-rvol 1.5 --report B1
python backtest.py --strategy dip_in_uptrend --regime on \
  --universe survivor --costs off --min-avg-vol 200000 --min-rvol 1.5 --report B2

# Realism ladder on the best config
python backtest.py --strategy dip_in_uptrend --regime <best> \
  --universe pit --costs off --min-avg-vol 200000 --min-rvol 1.5 --report B3
python backtest.py --strategy dip_in_uptrend --regime <best> \
  --universe pit --costs on  --min-avg-vol 200000 --min-rvol 1.5 --report B4
```

---

## Out of scope
- No brokerage/live-trading code. No deep-reversal (below-200) variant. No merging Phase 1B into Phase 1. No parameter mining to force a pass. No options/crypto/futures. PLTR never included.
