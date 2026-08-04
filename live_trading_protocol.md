# Live Trading Protocol — C2 Basket, Real Money

**Registered before the first live trade. No edits while live except via the
Amendment rule (Section 10).**

Owner: Ray · Account: Fidelity, $3,000 dedicated pool · Start date: __________
Status of every other pool: the manual swing plan is PAUSED while this runs — one
pool, one system.

**Nature of this document:** this is a self-binding contract with yourself. Every
number in it was decided before live results existed, exactly like the backtest
gates that got you here. The system being traded is the one that passed validation —
any deviation means you are trading an untested system.

**Standing disclaimer:** analysis framework, not financial advice or a guarantee of
profit. The strategy's backtested edge is modest (+4–7%/yr class), decaying since
publication, and may not persist. Expected GOOD quarter on $3,000: roughly +$30–60.

---

## 1. The system (frozen — validated as C-series C2 + Addendum 1)

- **Instruments (13):** SPY, QQQ, DIA, IWM, XLB, XLE, XLF, XLI, XLK, XLP, XLU, XLV, XLY.
- **Entry signal (computed on each day's close):** instrument close > its SMA200,
  AND Cumulative RSI(2) [today + yesterday, Wilder] < 35.
- **Exit signal:** RSI(2) closes above 65. Safety valve: exit any position at 25
  trading days regardless (never fired in backtest; kept anyway).
- **No stop-loss on individual trades.** Seatbelts are: 1/3 sizing, SMA200 filter,
  and the account kill switch (Section 6). Do NOT add stops mid-flight — that is an
  untested modification.
- **Slots:** max 3 concurrent positions, $1,000 nominal each (recompute as
  account/3 monthly, rounded down to nearest $50). One position per instrument.
  A slot frees the session AFTER its exit fills.
- **Tiebreak when signals > free slots:** take in the order the bot lists them
  (registered: bot lists by lowest Cumulative RSI first, matching the validated
  configuration — even though the backtest suggests the tiebreak barely matters).

## 2. Execution conventions (matching validated run L3 / next-open)

- Signals computed after the close. Orders placed the same evening.
- **Entries:** market-on-open (MOO) order for the next session. Fractional shares
  to hit the slot dollar amount within $5.
- **Exits:** when the exit signal fires on a close, place market-on-open for the
  next session. (Next-open exits are part of the validated convention — do not
  "wait for a better price.")
- Never intraday improvisation. If you miss placing an order in the evening, place
  it before 9:25am ET or skip that signal entirely and log the skip. Never chase
  after the open.

## 3. Division of labor — hard line

- **The bot:** analysis only. Runs the end-of-day scan, reports signals, slot
  state, exact orders to place, and exit flags. It has NO brokerage connection and
  never will under this protocol.
- **You:** place every order manually at Fidelity, record every fill, keep the log.
- **Nobody else.** No autotrading, no APIs, no "just this once."

## 4. Daily routine (≤10 minutes)

Evening (after 4pm ET close):
1. Run/read the bot's daily report.
2. Place any MOO entry/exit orders it specifies.
3. Update the trade log (Section 7) with yesterday's fills.

That's the whole job. Most evenings: no signals, no action. Roughly one trade per
week on average; clusters on bad market days are normal (that's when dips happen).

## 5. What is FORBIDDEN while live

- Changing any threshold (35 / 65 / SMA200 / slots / sizing) for any reason.
- Adding stops, targets, hedges, or "intuition overrides" (skipping a signal
  because it "feels wrong" = override; log-and-skip is allowed only for mechanical
  failures, and must be logged).
- Adding instruments, options, leverage, or crypto to this pool.
- Averaging down, doubling after losses, or "making it back."
- Peeking at intraday prices and acting on them. The system trades opens and
  closes; you are its clerk between 4:00 and 9:30, not its manager.

## 6. Kill switch and review gates (pre-registered)

- **K1 — Hard stop:** account equity closes below **$2,700** (−10%): sell
  everything at the next open, stop trading, write the postmortem. No "one more
  trade." This may fire on bad luck rather than a broken edge — accepted; it is a
  capital rule, not a verdict on the strategy.
- **K2 — Execution failure:** if average realized slippage vs the bot's modeled
  next-open price exceeds 0.25% per side over any 10 consecutive trades, pause and
  investigate before continuing. (Backtest assumed 2–5 bps; 25 bps means reality
  is ~5–10× worse than modeled and the edge may not survive it.)
- **K3 — Scheduled review at 3 months (date: __________), regardless of P&L.**
  Compare live vs backtest expectation:
  | Metric | Backtest expectation | Live |
  |---|---|---|
  | Win rate | ~70–75% | |
  | Avg %/trade | ~+0.33% | |
  | Avg hold | ~4 days | |
  | Trades taken | ~12–13/quarter | |
  | Avg slippage/side | ≤ 0.05% | |
  Outcomes: CONTINUE (metrics roughly in line, equity above $2,700) · STOP
  (K1 fired, or win rate < 55% on 12+ trades, or K2 unresolved) · EXTEND
  (fewer than 8 trades occurred — sample too small, run one more quarter).
- **K4 — Strategy-level fuse:** two consecutive STOP-eligible reviews = the
  strategy is retired from live trading. Back to research, no appeal.

## 7. Trade log (every trade, no exceptions)

One row per fill:
`Date · Ticker · Side (entry/exit) · Signal date · CumRSI at signal · Modeled open
price · Actual fill price · Slippage % · Shares · $ amount · P/L on exit ·
Days held · Notes (any deviation and why)`

Also log: skipped signals (and why), K2 slippage running average, and account
equity every Friday close.

This log is the real product of the quarter: it is the paper-trading experiment,
run with live tuition. At review time it answers the only question that matters —
does reality match the backtest?

## 8. Taxes and account mechanics

- All gains are short-term (ordinary income rates). The log doubles as the tax record.
- **Wash sales will happen** (re-buying an ETF within 30 days of selling it at a
  loss). This is normal for this strategy: Fidelity tracks it automatically, the
  deferred losses are not lost, but the 1099 will look messy. Do not try to avoid
  wash sales by skipping signals — that changes the system.
- PDT rule: holds average ~4 days, entries and exits at different sessions' opens —
  day trades should be rare to never. Never make 4+ same-day round trips in 5 days.
- Dividends: ETFs pay them; they'll dribble into the account. Fold them into equity;
  no action needed.

## 9. Expectation anchor (read this when impatient or scared)

- A GOOD quarter is +$30–60. That is the system working exactly as measured.
- A losing quarter is NORMAL and consistent with an intact edge — 12 trades is a
  coin-flip-dominated sample.
- The backtest's worst single trade was −32.8% on one slot (XLF, 2008). On this
  sizing that would be ≈ −$330 on one position — near the kill switch from a single
  tail event. That risk is real, accepted, and why slots are 1/3.
- The value of this quarter is the LOG, not the P&L. You are buying data about
  whether a verified backtest translates to a real account, for a capped tuition
  of $300.

## 10. Amendment rule

This document may only be changed between quarters, in writing, with the change
and its reason added to the Amendment Log below — never mid-quarter, never on a
losing day. The bot may be asked to verify a proposed amendment against the
validated configuration before adoption.

Amendment log: (empty)

---

## Appendix — Prompt for the bot (daily signal report, analysis only)

```
Build the C2-live daily report (analysis only — no brokerage code, no order
placement, hard guardrail unchanged). After each market close, using the
validated C2 basket configuration (13 instruments incl. SPY, CumRSI(2)<35
entry above SMA200, RSI(2)>65 exit, 25-day valve, 3 slots, fixed $ slots):

Report:
1. New entry signals (ticker, CumRSI value, listed lowest-first), which
   free slot to use, and the exact MOO order: ticker, $1,000 nominal,
   fractional shares at last close as estimate.
2. Exit flags on open positions (RSI2 > 65 at today's close, or valve at
   day 25): exact MOO sell order.
3. Slot state: open positions, entry date/price, days held, unrealized P/L.
4. Modeled next-open reference price for every order (for the slippage log).
5. Regime note: each instrument's close vs SMA200 (how many are eligible).
State clearly when there is nothing to do. Read open positions from a
positions file I maintain from my actual fills — never assume a modeled
fill happened. Provide the positions-file template.
```
