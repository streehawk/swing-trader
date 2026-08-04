# C-Series Addendum 1 — Step 1 Response

**Scope executed:** A1-1 (data) and A1-2 (pipeline) only. All checks pass.
**Stopped before any basket run**, as instructed.

Date: 2026-08-03 · Variant: **C2 only** (C1/C3 out of scope) · Branch: `streehawk/swing-trading_connors-strategy`

---

## A1-1 — Data coverage (12 ETFs)

`data/validation_cache/etf_basket.parquet` — 83,150 rows, 12 tickers, dividend/split
adjusted with `close_raw` retained (same registered convention as SPY).

| Ticker | What | First bar | Last bar | Bars | First usable | POST-BOOK bars | Gaps >5 |
|---|---|---|---|---|---|---|---|
| QQQ | Nasdaq-100 | 1999-03-10 | 2026-08-03 | 6,892 | 1999-12-21 | 4,675 | 0 |
| DIA | Dow 30 | 1998-01-20 | 2026-08-03 | 7,178 | 1998-11-02 | 4,675 | 0 |
| IWM | Russell 2000 | 2000-05-26 | 2026-08-03 | 6,584 | 2001-03-13 | 4,675 | 0 |
| XLB | Materials | 1998-12-22 | 2026-08-03 | 6,944 | 1999-10-07 | 4,675 | 0 |
| XLE | Energy | 1998-12-22 | 2026-08-03 | 6,944 | 1999-10-07 | 4,675 | 0 |
| XLF | Financials | 1998-12-22 | 2026-08-03 | 6,944 | 1999-10-07 | 4,675 | 0 |
| XLI | Industrials | 1998-12-22 | 2026-08-03 | 6,944 | 1999-10-07 | 4,675 | 0 |
| XLK | Technology | 1998-12-22 | 2026-08-03 | 6,944 | 1999-10-07 | 4,675 | 0 |
| XLP | Staples | 1998-12-22 | 2026-08-03 | 6,944 | 1999-10-07 | 4,675 | 0 |
| XLU | Utilities | 1998-12-22 | 2026-08-03 | 6,944 | 1999-10-07 | 4,675 | 0 |
| XLV | Health care | 1998-12-22 | 2026-08-03 | 6,944 | 1999-10-07 | 4,675 | 0 |
| XLY | Discretionary | 1998-12-22 | 2026-08-03 | 6,944 | 1999-10-07 | 4,675 | 0 |

- **All 12 first bars match the registered inceptions.** The fetcher flags any
  deviation over 45 days; none fired.
- **Zero gaps > 5 sessions** in any instrument. Gaps are counted in *missing
  weekdays*, not calendar days — a calendar threshold silently forgives the 9/11
  shutdown (Mon→Mon = 7 calendar days but 4 lost sessions). Nothing is bridged or
  interpolated.
- **POST-BOOK coverage: 12/12**, and identically 4,675 bars each — the SMA200
  warm-up completes years before the window opens for every name, so no 2008 trade
  runs on a half-formed SMA200.

---

## A1-2 — SPY control

**PASS — exact.**

| | |
|---|---|
| Registered C2 trades | 146 |
| New pipeline | 146 |
| Fields compared per trade | 11 (dates, indices, bars held, entry/exit price, pct return, MAE, MFE, exit reason) |
| **Mismatches** | **0** |
| Reproduced stats | 74.7% win, **+0.4363%/trade**, PF 2.095, t = +3.20 |

The comparison is against the **stored trades of the run that produced the published
verdict** (`c_series_l1_l2_l3_l4_2026-08-02.json`), not a fresh recomputation — a
recomputation could reproduce a shared bug and agree with itself.

---

## A1-2 — Look-ahead replay

All 13 instruments pass, none vacuously (the floor is ≥2 trades compared, added
after Phase 1B's check passed on zero trades):

| QQQ | DIA | IWM | XLB | XLE | XLF | XLI | XLK | XLP | XLU | XLV | XLY | SPY |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 216 | 215 | 200 | 198 | 197 | 183 | 212 | 197 | 212 | 187 | 206 | 227 | 264 |

SPY also passes under `next_open` (264 compared) — that is the G3 fill path, so it is
verified rather than assumed. These counts are full-history signal frequencies at
zero cost; **no performance number was computed for any basket instrument.**

**42 new unit tests, all passing; 254 across the research suites, no regressions.**

---

## Three things I need you to rule on before Step 2

### 1. Position sizing is genuinely ambiguous, and it moves G2

The addendum says *"capital split equally across open positions"*. Read literally
that means the account is **fully invested whenever ≥1 position is open** — with one
position you are 100% in one sector ETF. I have implemented that as primary
(`equal_open`).

The alternative is fixed slots: 1/3 per slot in P-capped, so the invested fraction is
k/3 and the rest sits in cash. This is not cosmetic — **G2 requires P-capped MAR ≥ SPY
buy-and-hold MAR**, and the two sizings give different CAGR *and* different drawdown.
`fixed_slots` is implemented as a labelled diagnostic.

**Tell me which is the registered one, or confirm the literal reading stands.**

### 2. Slot occupancy — I had to register a rule the addendum does not state

A position holds its slot from its entry bar **through its exit bar inclusive**, so a
signal on the day another position exits cannot take that slot. The alternative
recycles a slot within one session, which assumes you sell at the close and buy at
the same close. I picked the conservative reading; it is unit-tested both ways and
easy to flip if you disagree.

### 3. Two data notes

The SPY drift diagnostic found the registered cache is one bar short of today (it
ends **2026-07-31**) and 26 bars whose close moved by at most **1.5×10⁻⁶** relative —
floating-point noise in yfinance's adjustment, not restatement. **The registered cache
was not modified.** Consequence: the basket window runs one session longer than the
SPY benchmark's (4,675 vs 4,674 bars). Immaterial, but it is why the two will not tie
out to the day.

---

## Files

| File | Purpose |
|---|---|
| `scripts/fetch_etf_basket.py` | A1-1 — fetch, coverage report, gap detection, SPY drift check |
| `core/basket_backtest.py` | A1-2 — multi-instrument runner + portfolio simulator + control |
| `scripts/run_etf_basket.py` | Step 1 / Step 2 runner |
| `tests/test_etf_basket.py` | 42 acceptance tests |
| `data/validation_cache/etf_basket.parquet` | The basket cache (+ `.meta.json`) |
| `data/validation_cache/etf_basket_step1_2026-08-03.json` | This run's artefact |

**Nothing is committed. No basket backtest has been run.**
