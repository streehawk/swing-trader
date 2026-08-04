# Edge-Search Protocol — Pre-Registered Hypothesis Ladder

**Purpose:** A disciplined search for a retail-accessible edge using the existing,
validated backtest harness. Four structurally different hypotheses, each pre-registered
with its rules, its reason-to-work, and its pass/fail gate — all written BEFORE any
result exists. This document is the registration. Nothing in it may be edited after a
hypothesis has produced numbers.

**Prior findings this builds on (do not re-litigate):**
- Phase 1 (trend-pullback): ~zero expectancy across three configurations, each sitting
  on its own breakeven line → the entry carried no directional information. CLOSED.
- Phase 1B (dip-in-uptrend): zero qualifying setups across 3 universes / ~1M bars. CLOSED.
- Known bug-class fixes carried forward: stop-limit fills honored, SPY sliced to signal
  bar, END_OF_DATA labeling, universe-path fix, retry-with-backoff on fetchers.

**Hard guardrails (unchanged):** analysis/backtest only. No brokerage connection, no
live orders. PLTR permanently excluded; also exclude closed-end funds (GAB/BDJ class —
add `is_operating_company` or a CEF blacklist to the exclusion gate; this is a
pre-registered fix from the Phase 1B postmortem, not tuning). Equities/ETFs only.

---

## GLOBAL RULES — apply to every hypothesis

1. **One hypothesis at a time**, in the order listed. Each runs to its verdict before
   the next starts.
2. **No threshold tuning.** Every constant below is final once that hypothesis starts.
   If a hypothesis fails, it fails — the next hypothesis is structurally different,
   not a re-tuned version of the last.
3. **Sizing/exits held constant** unless the hypothesis explicitly redefines exits as
   part of its structure (H2 and H4 do — that redefinition is registered here, now).
   Default: risk 0.75%/trade, max 2 concurrent, 15-bar time stop, break-even ratchet.
4. **Realism ladder per hypothesis:** cheap survivor/no-cost run first (kill fast),
   then costs+slippage on the surviving config. Slippage: ≥ 0.05–0.15%/side or 1 tick,
   whichever larger; use the larger bound for anything under $2B cap or under 1M avg volume.
5. **Labels:** every result on the current data is `SURVIVOR / CURRENT-FUNDAMENTALS /
   UPPER-BOUND-ONLY` where applicable. On survivor data the best achievable verdict is
   **MARGINAL (upper bound)** — PASS requires point-in-time data. (H4 runs on index
   ETFs, where survivorship of the *instrument* is not an issue — SPY/QQQ existed all
   period — so H4 alone can reach PASS on present data.)
6. **Train/test split:** compute results separately for the first ~70% of dates and
   the last ~30%. A gate is only met if the sign of expectancy holds in BOTH splits.
7. **Trade-count floor:** < 100 trades total → verdict is INSUFFICIENT-SAMPLE, not
   FAIL, and the hypothesis closes. (H4 exempt — see its own gate; it is a low-
   frequency strategy by design.)
8. **No look-ahead**, verified by the existing `confirm_no_lookahead()` replay on every run.
9. **Stopping rule for the whole project:** after all four verdicts, STOP. The next
   step is a written project review, not hypothesis five. Universe changes mid-
   hypothesis are forbidden; each hypothesis names its universe below.
10. **Report format per hypothesis:** #trades, win rate, avg win R, avg loss R,
    expectancy R/trade, breakeven win rate, max drawdown, profit factor, per split,
    plus the acceptance log (universe, costs, look-ahead check).

**Default gate (H1–H3):** PASS-quality result = expectancy ≥ +0.10R/trade with costs
ON, ≥ 300 trades, profit factor ≥ 1.2, sign holds in both splits. On survivor data
this reads MARGINAL (upper bound) and would justify — at most — acquiring honest
point-in-time data to confirm. 0 to +0.10R = no reliable edge. ≤ 0 = FAIL.

---

## H1 — Panic Mean-Reversion in Quality Large Caps ("supply liquidity to forced sellers")

**Structural difference from everything tested so far:** buys *weakness* with a
limit order instead of chasing *strength* with a stop order. Short hold, no breakout
confirmation.

**Why it should work (who loses to you):** short-horizon overreaction. When a liquid
quality stock drops hard for several days, part of the selling is forced or emotional —
margin calls, risk-limit liquidations, retail panic. Sellers who *must* sell pay a
premium for immediacy; the buyer supplying liquidity collects it as the price snaps
back. This is one of the best-documented short-horizon effects in equities, and it is
the exact mirror of the pullback strategies that failed: those needed follow-through
(information); this needs only reversion to normal (mechanics).

**Universe:** the ORIGINAL 230-name quality list (restore from backup). Rationale:
mean reversion wants companies that reliably survive panics — the "calm quality" that
made these names useless for breakout hunting makes them ideal here. Liquidity ≥ 1M
avg volume; price ≥ $10.

**Entry rules (all on signal day close):**
- Long-term trend intact: close > SMA200.
- Panic condition: close is ≥ 2.5 × ATR(14) below the highest close of the last 10
  trading days, AND at least 3 of the last 4 days closed down.
- Next day: place a LIMIT buy at the signal day's close minus 0.5 × ATR(14). Fill only
  at or below the limit (no chase — the fill IS the discount). Order expires end of day.
- No earnings within 10 calendar days (reuse gate). Regime: none (panic entries occur
  in soft tape by nature — regime gate registered as OFF, no A/B).

**Exit rules (registered as part of the hypothesis structure):**
- Exit at close of the first day the stock closes above SMA5, OR
- Hard stop: 2 × ATR(14) below fill, OR
- Time stop: 7 trading days.
- NO break-even ratchet, NO 2R target requirement — mean reversion takes many small
  wins; forcing 2R was structurally wrong for this shape and is pre-registered out.

**Acceptance:** unit tests for the panic condition (deep multi-day drop above SMA200
= True; single-day drop = False; drop below SMA200 = False), limit-fill logic
(open below limit fills at open; never fills above limit), SMA5 exit.

**Gate:** default gate. Extra kill-switch: if avg loss R > 1.4 (stops gapping badly),
flag it prominently — that is this strategy's known failure mode.

---

## H2 — Strong-Gap Continuation ("the trades our limit cap refused")

**Structural difference:** buys extraordinary strength at market, accepting the bad
fill as the cost of the signal. No pullback, no pause, no limit cap.

**Why it should work (who loses to you):** this is the ONLY hypothesis in the project
generated from our own data: in Phase 1, the 35 trades that gapped beyond the entry
limit — the ones the system refused — won 68.6% at +0.43R average. Registered
interpretation: a gap so strong it blows through a chase-protection cap reflects real
news/flow that continues for days (underreaction + institutional accumulation that
takes days to complete). Whoever sold into that gap at the open is the counterparty.
This MUST be tested on fresh configuration precisely because it was noticed post-hoc
on 35 trades — that sample is spent; it proves nothing by itself.

**Universe:** the current 407-name mid/large quality list (the $2B+ screen already
built and applied on the branch). Gaps in liquid names — slippage is survivable there.

**Entry rules:**
- Signal: today's OPEN gaps up ≥ 1.0 × ATR(14) above yesterday's close, AND the open
  is above the highest high of the last 20 days, AND RVOL(50) on the day ≥ 2.0
  (measured at close for the census; entry uses the next rule).
- Entry: BUY at next day's open, at market. (Deliberately one day late — this tests
  *continuation*, uses only closed-bar data, and eliminates intraday look-ahead.)
- Trend context: yesterday's close > SMA50 > SMA200. No earnings within 10 days
  AND no earnings in the prior 2 days (exclude earnings-gap events themselves —
  this isolates non-earnings strength; earnings drift would be a different hypothesis).
- Regime gate ON (SPY > SMA200): registered, no A/B.

**Exit rules:** standard Phase-3 set: stop = entry − 1.5 × ATR(14); no fixed target —
trail: exit at close of first day that closes below SMA10; time stop 15 days;
break-even ratchet at +1R. (Strength strategies die by capping winners; the trail is
registered here, before results.)

**Acceptance:** unit tests for gap detection (gap ≥ 1 ATR over 20-day high = True;
gap without new high = False; earnings-day gap = excluded), SMA10 trail exit.

**Gate:** default gate.

---

## H3 — Post-Earnings Announcement Drift (PEAD) — CONDITIONAL on data

**Structural difference:** event-driven. The signal is a scheduled information event,
not a chart shape.

**Why it should work (who loses to you):** after a genuinely big positive earnings
surprise, prices historically keep drifting the same direction for days-to-weeks —
institutions can't buy their full position instantly, and anchored holders sell too
early. The counterparty is the impatient seller unloading into a repricing that isn't
finished.

**Data precondition (check FIRST, before implementing):** this needs historical
earnings dates AND next-day reaction for the universe over 5.5 years. We do NOT have
surprise magnitudes. Registered proxy: the market's own verdict — the earnings-day
price reaction. If historical earnings DATES are unavailable/unreliable for most of
the universe, report DATA-UNAVAILABLE and close H3 without implementing. Do not
approximate earnings dates from volume spikes — that is a different (untested) signal.

**Universe:** 407-name list.

**Entry rules:**
- Event: earnings reported; next regular session closes ≥ +5% vs pre-announcement
  close, on RVOL ≥ 2.0, and closes in the top third of its daily range.
- Entry: buy at open 2 trading days after the reaction day (lets the initial spike
  settle; only closed bars used).
- Context: close > SMA200. Regime gate ON.
- (The "no earnings within 10 days" gate is inverted here by design: the event IS the
  signal. The never-hold-through-the-NEXT-report rule still applies and is trivially
  satisfied by the time stop.)

**Exit rules:** stop = entry − 1.5 × ATR(14); time exit at 15 trading days at close
(drift is time-bounded — this is the registered primary exit); break-even ratchet at +1R.

**Gate:** default gate, with trade-count floor 150 instead of 300 (events are rarer;
registered now).

---

## H4 — Regime Trend-Following on Index ETFs ("own the market only when it's healthy")

**Structural difference:** different asset class (broad index ETFs), different
frequency (weeks-to-months holds), different benchmark (buy-and-hold, not R-expectancy).
This is the boring hypothesis with the strongest published record (time-series momentum).

**Why it should work (who loses to you):** nobody, directly — the edge claimed is not
excess return but *drawdown avoidance*: investors systematically under-react at the
start of major downtrends (2022-style), and a mechanical exit sidesteps the worst of
them at the cost of whipsaw losses in choppy years. The registered question: does the
insurance premium (whipsaws) cost less than the disasters it avoids?

**Instruments:** SPY and QQQ only. 50/50 capital split, evaluated independently and combined.

**Rules (evaluated on the last trading day of each week; act at next open):**
- IN when: close > SMA200 AND SMA200 rising vs 20 days ago.
- OUT (to cash) when: close < SMA200 for 5 consecutive trading days (the 5-day
  confirmation is the anti-whipsaw constant — registered, final).
- No stops, no targets, no position sizing beyond the 50/50 split; this is a switch,
  not a trade.

**Gate (different by design, registered):** over the full 5.5y AND in both splits:
- Max drawdown ≤ 2/3 of buy-and-hold's max drawdown for the same instrument, AND
- CAGR ≥ (buy-and-hold CAGR − 2 percentage points), AND
- MAR ratio (CAGR / MaxDD) strictly greater than buy-and-hold's.
PASS = all three (achievable on present data — no survivorship issue for index ETFs).
FAIL = any miss. Note: 5.5 years contains essentially one full bear cycle (2022) —
even a PASS is one-cycle evidence and must be labeled ONE-CYCLE-SAMPLE.

**Acceptance:** unit tests for the 5-day confirmation (4 days below = still IN),
weekly evaluation timing, next-open execution.

---

## EXECUTION ORDER AND STOP CONDITIONS

Run H1 → H2 → H3 → H4. For each: implement + unit tests → cheap run → (if alive)
costs run → verdict → one-paragraph postmortem in the README → next.

Kill a hypothesis immediately on: trade-count floor breach, expectancy ≤ 0 with costs,
or sign flip between splits. Do not resuscitate with adjustments.

**After all four verdicts — the project review must answer:**
1. Did anything reach MARGINAL-upper-bound (H1–H3) or PASS (H4)?
2. If yes: is it worth paying for point-in-time data to confirm (H1–H3), or paper-
   trading alongside the manual plan for 3 months (H4)? Those are the only two
   permitted next steps — deployment is not one of them.
3. If no: the honest conclusion is that no retail edge was found in this project's
   search space, and the manual plan's discipline remains the portfolio's real asset.

## Out of scope
No brokerage/live code. No options/crypto/futures/shorting. No parameter sweeps. No
hypothesis five. PLTR never included. No editing this document after results exist.
