# How DipRadar Works — Plain English

Last updated 3 August 2026.

This project is **two things bolted together**, and confusing them is the main way
to misread it:

| | What it is | What it does |
|---|---|---|
| **The bot** | A Telegram scanner that runs every evening | Hands you complete trade plans, with stop and size already worked out |
| **The harness** | A research rig for testing whether an idea makes money | Tells you, honestly, which ideas don't — which so far is nearly all of them |

The bot is disciplined but **has no proven edge**. The harness has found exactly one
edge, and it is small. Sections 1–2 explain the bot. Sections 3–6 explain the
harness and why you should believe what it says. Section 7 is the summary.

---

## 1. How the bot chooses stocks

Every evening it runs the same seven checks, **in order**. A stock must pass **all
seven**. The moment one fails, that stock is dropped and the rest aren't even
checked. Seven doors in a corridor — fail any door, you're out.

### Door 1 — Is the market safe today?

Before looking at *any* individual stock, the bot checks the S&P 500 (`SPY`):

- Is SPY above its **50-day average** price?
- Is SPY above its **200-day average** price?

**If either answer is no, the bot stops completely.** No stocks are scanned at all.
A bad market ruins good setups, so there's no point browsing a list you shouldn't
act on.

> **Real example, 28 July 2026:** SPY closed at 740.86 but its 50-day average was
> 743.98. Below it → gate shut → zero trades that day, no matter what any
> individual stock looked like.

If SPY data can't be fetched, the bot also says no. *"I don't know if the market is
safe"* is treated the same as *"the market isn't safe."*

This door is the single most valuable rule the bot has — see section 6.

### Door 2 — Is the stock tradeable?

- Price **at least $10** (no penny stocks)
- At least **1 million shares** traded per day on average, so you can get out
- Not on the live ban list — currently just `PLTR` (settable via
  `SWING_EXCLUDED_TICKERS`)

If the bot can't confirm the trading volume, it says no rather than guessing.

> **A gap worth knowing about.** Leveraged/inverse ETFs and closed-end funds are
> kept out at the *list-building* stage, not by this door — so the live bot is only
> as protected as `core_universe.json` is clean. Phase 1B caught two closed-end
> funds (GAB, BDJ — funds dressed as companies) that had slipped a screen, and the
> fix went into `strategies/filters.py`, which the **research** strategies all use.
> The live scanner (`core/swing_setups.py`) doesn't share that blacklist. Neither
> fund is in today's 407-name list, so nothing is wrong right now; it's a
> single-point-of-failure to be aware of if the universe is rebuilt.

### Door 3 — Is the stock in a real uptrend?

- Today's price is above the 50-day average
- The 50-day average is above the 200-day average
- The 50-day average is **rising** compared to a month ago

This is the opposite of bargain hunting. The bot buys things going **up**.

### Door 4 — Is it a leader or a laggard?

Over the last **three months**, has this stock gone up *more than the S&P 500*? If
it's rising but slower than the market, it's rejected.

### Door 5 — Is this a healthy pause, or is it breaking down?

The bot wants a stock that's been climbing and has recently **taken a breather**:

| Check | What it means in plain terms |
|---|---|
| **Depth** | The dip is a normal-sized wobble for *this* stock — not tiny, not huge. Measured against the stock's own typical daily swing, so a jumpy stock is allowed a bigger dip than a calm one. |
| **Volume** | Fewer shares changing hands during the dip than before it. Quiet selling means people are pausing, not running. |
| **Support** | The price never *closed* below its 50-day average during the dip. If it did, that's a breakdown, not a pause. |

### Door 6 — Is there earnings risk?

Earnings announcements can move a stock 10–20% overnight, straight through any
safety net you set. **If a report is due while you'd still be holding, the stock is
rejected.** No exceptions.

*(Note: the live bot has always had this door. The **backtests** couldn't use it
until August 2026, because nobody had a reliable history of earnings dates. That's
now fixed — see section 5.)*

### Door 7 — Is the maths worth it?

```
Buy at    : just above the top of the pause
Safety net: just below the bottom of the pause
Target    : twice as far up as the safety net is down
```

You risk $1 to try to make $2. Then two final rejections:

- If the safety net would be more than **8% below** your entry, the stock is too
  wild — skip it.
- If reaching the target would need an unusually large move for this stock, skip it.
  **If a 2-to-1 target isn't realistic, don't force it.**

### What comes out the other end

A complete plan you can hand to your broker without doing any maths:

```
AAPL
  Buy at    231.40  (only if the price actually rises to it)
  Safety net 224.10  (this is your "1R" — the amount you're risking)
  Target     246.20
  Give up after 15 trading days if neither happens
  Buy 12 shares  →  risks exactly $30, which is 1% of a $3,000 pot
```

If the price never reaches 231.40, **you never buy and you never risk a penny.**

---

## 2. Where the list of stocks comes from

### It's a file you can open

**`core_universe.json`**, in the main project folder. It currently holds **407
tickers**. It isn't typed by hand and it isn't permanent — `scripts/refresh_core_universe.py`
rebuilds it by scanning a pool of ~1,500 listed US companies and keeping the ones
that pass a quality screen:

- Market value at least **$2 billion**
- **Profitable** (net income positive over the last twelve months)
- **Return on equity above 10%**
- **Revenue growing** over five years
- **Debt under 1× equity** (banks and insurers are exempt — the ratio means
  something different for them)
- Price ≥ $10, average volume ≥ 200k shares

407 isn't a target. It's just how many companies passed, last time it ran (30 July
2026): 1,532 in the pool → 407 through. Run it again and you might get 390 or 420.
You can also force a stock permanently in or out via the `overrides` section, which
always wins over the screen.

### There are four universe files, and that's deliberate

Research kept needing different candidate pools, so old lists were kept rather than
overwritten:

| File | Names | What it is |
|---|---|---|
| `core_universe.json` | **407** | **The live list.** $2B+ profitable screen |
| `core_universe.json.bak2` | 407 | Identical backup of the above |
| `core_universe.json.bak` | 230 | The original hand-curated "quality compounder" list |
| `core_universe.json.p1b_final` | 96 | A small/mid-cap ($300M–$2B) list built to stress-test one hypothesis |

⚠️ **The bot scans whatever `core_universe.json` says right now.** During the
research runs this file was swapped several times, and the live scanner followed it
each time — that's how 229 rows landed in `core_alert_db.csv` while it briefly held
the 230-name list. If you care which universe is "production", check this file
before trusting a scan.

### One honest warning about every one of these lists

They contain companies that are healthy **today**. Anything that went bankrupt or
fell apart between 2021 and 2026 isn't in the file — it got dropped, silently.

That's fine for trading tomorrow. It's a real problem for *testing the past*,
because the test never sees the disasters, and the disasters are exactly what a
dip-buying system would have bought into. The file admits it in its own metadata:

```json
"bias_label": "SURVIVOR / CURRENT-FUNDAMENTALS / UPPER-BOUND-ONLY"
```

This is why almost every backtest number in this project carries an "upper bound"
warning: reality would have been worse by an amount nobody can measure without paid
data. It's logged automatically in every run so it can't be quietly forgotten.

---

## 3. The harness — and why you should believe it

Anyone can write a backtest that says their idea works. Most of them are wrong in
one of four specific ways. Here is what this project does about each, in plain
English.

### Cheat #1 — Using tomorrow's information today

The classic bug: your code peeks at data that hadn't happened yet. It's easy to do
by accident and it makes worthless strategies look brilliant.

**What the harness does about it, at runtime, on every run:** it takes real price
history, **glues 40 fake days of a massive rally onto the end**, and re-runs the
whole thing. Any trade that had already closed *before* the fake data starts must
come out **byte-for-byte identical**. If the future changed the past, the code was
peeking, and the run's verdict is forced to `INVALID`.

It's a nasty test to pass and it caught a real gap: Phase 1B's check returned
"inconclusive" because that strategy produced no trades, so there was nothing to
compare — the check had passed without proving anything. Every check since then
**requires at least two trades to compare** before it's allowed to say "pass."

### Cheat #2 — Only testing on the survivors

Covered in section 2. The harness can't fix it without paid data, so instead it
**detects it and refuses to forget it**: before each run it counts how many tickers'
price histories stop early. Zero delistings in five years is impossible in a real
universe, so the run gets stamped `SURVIVOR`, and the scoring gate is **physically
unable to issue a clean pass** on survivor data — the best available result is
downgraded to "upper bound only."

The one exception is the C-series, which trades SPY and nothing else. SPY has been
continuously listed since 1993 and cannot have delisted. That's a large part of why
the C-series result is the only one worth taking seriously.

### Cheat #3 — Trading for free

Real trades cost money, and a strategy that's barely profitable often isn't once
you pay for it. The harness charges:

- **Commission**, counted separately so you can see what each costs
- **Slippage** — the gap between the price you wanted and the price you got —
  charged at 10 basis points per side (15bps for thinly traded names), with a floor
  of one cent
- **Only on the fills where slippage is real.** A market order or a stop order takes
  whatever price is there, so it slips. A limit order fills **at your price or not
  at all**, so charging slippage on it would be inventing a cost that doesn't exist.

This is not a rounding detail. The first version of the swing backtest charged
nothing on stop exits, which made every loss look smaller than it was. Charged
properly, **a stop-out costs more than the 1R you thought you were risking.**

### Cheat #4 — Assuming you always get filled

The plan places a **stop-limit** order: "buy if it rises to $231.40, but don't pay
more than 0.2% above that." A naive backtest fills that order always. Reality
doesn't: if the stock gaps straight past your limit and never comes back, **you
simply don't own it.**

Modelling this correctly removed 35 trades from the Phase 1 sample — and those 35
were the *best ones*, averaging +0.43R at a 68.6% win rate. That makes sense: a
stock violent enough to blow through your chase-guard is in a powerful move.

Which raises the obvious temptation: *widen the limit and keep those trades.* The
harness records the observation and **does not act on it**, because changing a
parameter after seeing which change would help is the exact thing section 4 exists
to prevent. (That observation later became its own pre-registered hypothesis, H2 —
and when tested honestly on fresh rules, it **failed**. The 35 trades were noise.)

### What's still not modelled — stated, not hidden

Real bid-ask spreads (the price cache is daily open/high/low/close/volume only, so
slippage is an *assumption*, not a measurement) · borrow costs · trading halts and
limit-up days. **All three inflate results.**

---

## 4. Pre-registration — the rule that makes the rest mean anything

Here's the trap every backtest falls into. You test an idea. It fails. You tweak a
threshold. It fails less. You tweak another. Eventually it "works" — and what you've
actually built is a description of the last five years, which tells you nothing
about the next five.

The defence is boring and total:

> **Write down the rules and the pass mark BEFORE running anything. Then don't move
> them.**

In practice that means:

1. **A spec document comes first** — `Phase1B_Reversal_Strategy_Spec.md`,
   `edge_search_protocol.md`, `connors_rsi2_spec.md`. Every threshold is written
   down and declared final.
2. **The pass mark is written down too**, before any result exists. For most
   hypotheses: *every $1 risked must return at least $1.10 on average, across at
   least 300 trades, with profits at least 1.2× losses.*
3. **The spec forbids editing itself** once results exist. No adding a filter "to
   see", no swapping the exit, no retrofitting a stop-loss.
4. **The result is split in half by date** and the edge must point the same way in
   both halves. An edge that only exists in the first half is a coincidence you
   fitted to.
5. **The verdict gets written up whatever it says**, and everything gets a name:
   `PASS`, `MARGINAL`, `NO-EDGE`, `FAIL`, `INSUFFICIENT-SAMPLE`, `INVALID`.

**The proof this is working is that the results are mostly bad.** Eight ideas have
been through the harness: **four failed outright**, **two came up short** (one real
but too small to use, one twenty trades shy of its sample floor), and **two passed**.
If the process were being bent, that ratio would look very different.

There's one specific temptation worth naming, because it came up. C1 — the most
famous of the Connors variants — scored the **best per-trade result of anything ever
tested here** (+0.528%, profit factor 2.29) and missed on exactly one criterion: 80
trades against a floor of 100. Twenty trades. The spec says MARGINAL means *"not
tradable; do not tune; record and close."* So C1 is closed. Being the best number in
the project doesn't buy it an exception, because the floor was set before anyone
knew what the number would be — which is the entire point of setting it then.

---

## 5. What's actually been tested, and what came out

Each strategy lives in its own file under `strategies/`. Status as of 3 August 2026:

| File | The idea, in one line | Verdict |
|---|---|---|
| `trend_pullback.py` | Buy the quiet pause in a rising leader (**this is the live bot**) | ❌ **No edge** — dead on zero |
| `dip_in_uptrend.py` | A stricter version of the same, needing a volume surge | ❌ **Failed** — produced *zero trades* |
| `panic_reversion.py` | Bid below a sharp flush in a stock still in an uptrend | ⚠️ **Too small** — real, but unusable |
| `gap_continuation.py` | Chase a violent gap up, expecting it to keep running | ❌ **Failed** — lost money |
| `rsi2.py` | Buy SPY when it's briefly oversold inside a bull market | ✅ **Two of three passed** |

### The one that failed most interestingly

`dip_in_uptrend.py` produced **zero trades on three different universes** — 407
large caps, 96 small caps, 240 quality names. Not "a few losses": nothing at all.

A zero result is worthless unless you know whether it's a bug, so the harness
counted every single bar and recorded which door it failed first. On the main
universe, **86% of all bars died at one rule** (needing above-average volume), and
only **17 bars in five and a half years** got as far as the final check. Even
deleting that final check entirely caps the strategy at 17 possible trades — thirty
times short of the 300 needed.

The diagnosis: the rules asked a bar to be **quiet enough to be a pause and busy
enough to show a volume surge, simultaneously.** Those are near-opposites. No
universe fixes that, and no threshold tweak would have been honest.

### The by-product worth more than the hypothesis that produced it

`gap_continuation.py` failed. But building it required knowing exactly when every
company reported earnings — because a gap on earnings day is a *different* idea, and
mixing them in would have meant testing one hypothesis under another's name. (When
measured: **73.5% of qualifying signals were earnings reactions.** Three-quarters of
the "gap continuation" test would have been something else entirely.)

Nobody sells that data cheaply, so the project built it from **SEC EDGAR 8-K
filings** — specifically item 2.02, "Results of Operations", which companies file on
the day they report. Free, public, and genuinely dated at the time.
`scripts/fetch_earnings_dates.py` now covers **395 of 407 tickers (97.1%), 9,913
filings**. The twelve it can't cover are mostly foreign issuers that file a
different form; they're **excluded from tests rather than waved through**, because
applying a rule to some tickers and not others biases the sample.

Every backtest before August 2026 had to run with the earnings door switched off.
That's now fixable, and it's what makes the next queued hypothesis (H3, post-earnings
drift) possible at all.

### The two that passed

`rsi2.py` implements three variants of an idea from Connors & Alvarez's *Short Term
Trading Strategies That Work*: when SPY is in a long-term uptrend but gets briefly
oversold, buy it, and sell a few days later when it bounces. No stop loss — that's
the book's design, not an oversight.

Two of the three cleared every bar of their pre-registered gate on **18.6 years of
data the authors never saw** (they published on 1993–2007; the test that counts runs
2008–2026):

| | Trades | Win rate | Average per trade | Profit factor |
|---|---|---|---|---|
| **C2** | 146 | 74.7% | **+0.44%** | 2.10 |
| **C3** | 207 | 73.0% | **+0.34%** | 1.84 |

To make this possible the project keeps its own SPY price history —
`data/validation_cache/spy_extended.parquet`, daily bars back to 1993. Building it
turned up three genuine market closures (9/11, the Ford funeral in January 2007, and
Hurricane Sandy), which is a good sign the data is real rather than smoothed.

**What this does and doesn't mean** is important enough to spell out in section 6.

---

## 6. What the verdicts actually mean

### "No edge" — the bot's own verdict

**Does this make money on purpose, or is it a coin flip?**

The live strategy was tested on 5½ years of real prices — 666 trades. Result:
**essentially zero.** Not a disaster. Not a profit. Zero.

Here's the part that surprises people. Winners averaged about **1.5×** what was
risked; losers about **0.85×**. Winners bigger than losers — so surely it profits?
Only if you win often enough. With that payoff you need to be right **36.6%** of the
time to break even. The system was right **36.2%** of the time.

**Four-tenths of a percent short.** That's the whole story.

And it's not a fluke of one configuration. Several were tested:

| Version | Needed to break even | Actually achieved |
|---|---|---|
| First version | 35.1% | 34.5% |
| Filtered version | 36.6% | 36.2% |
| Simpler exits | 31.2% | 30.2% |

Every version lands **just under its own break-even line.** Make the target bigger
and the win rate drops by exactly enough to cancel it. Add filters and the same
thing happens. That's the signature of a system that genuinely doesn't know which
way a stock is going next — you can shuffle *how* you win and lose, but you can't
shuffle your way into a profit that isn't there.

**What "no edge" does NOT mean:**

- ❌ It doesn't mean the code is broken. It works exactly as designed.
- ❌ It doesn't mean you'll definitely lose. It means you'll drift around zero,
  minus costs.
- ❌ It doesn't mean the rules are pointless. Door 1 (the market check) measurably
  helps — turning it off makes results clearly worse, and it saved you from most of
  2022. It's just not enough on its own.

**The bot can tell you "this passes all seven checks." It cannot tell you "this will
make money."** What you're getting is *discipline*: a stop on every trade, correct
sizing, mechanical exits, and a filter that stops you buying into falling markets.
That's genuinely valuable. It just isn't an edge.

### "Pass" — and the three things it doesn't buy you

C2 and C3 are the first genuine passes this project has produced, and they're
trustworthy in a way nothing before them was — SPY can't have delisted, the cost
assumption is defensible, and there's no queue of signals competing for the same
capital.

But read the size before getting excited:

**1. The edge is small.** +0.34% to +0.44% per trade. Real, statistically solid —
and not a number that survives sloppy execution.

**2. It's been shrinking since the book came out.** Compare the authors' window to
the years after it:

| | Before publication | After publication |
|---|---|---|
| C1 | +1.05% | +0.53% |
| C2 | +0.81% | +0.44% |
| C3 | +0.66% | +0.34% |

Roughly halved, across the board. That's what usually happens once a strategy is
printed in a book, and the trend points the wrong way.

**3. It does not beat buying and holding.** This is the one to sit with:

| | These strategies | Just owning SPY |
|---|---|---|
| Annual return | 2.4 – 4.1% | **11.24%** |
| Worst drawdown | 14 – 15% | 51.9% |
| Time invested | 6 – 17% | 100% |

They earn less because they're **in cash 84–94% of the time**. What they beat
buy-and-hold on is *fear*: a 15% worst-case instead of a 52% one. Per unit of pain
they're slightly better; per dollar they're far worse.

So the honest description is: **a rule for deploying cash you weren't going to
invest anyway** — and it's only interesting if that idle 84–94% is earning something
elsewhere. It is not a replacement for owning the index.

**And the risk lives somewhere unusual.** There's no stop loss, by design. The worst
single trade closed at **−10.2%** after going **−15.6% underwater** first. With no
stop, position sizing *is* your entire risk control.

### One number worth remembering

There's a pre-written pass mark: **+0.25R** for the live swing system — every $1
risked should return $1.25 on average. That mark was written down *before* any test
was run, specifically so it couldn't be quietly lowered later.

The system scores **−0.009R**. It isn't close, and **the mark hasn't moved.**

---

## 7. The one-paragraph summary

Every evening the bot checks whether the market is safe, and if it is, looks through
407 profitable US companies for one in a genuine uptrend, beating the market,
currently taking a quiet breather, with no earnings due — then hands you a complete
plan with an exact buy price, safety net, target and share count. It does all of
this correctly and with real discipline. **But five and a half years of testing say
those trades are a coin flip**, so treat the bot's output as structured practice
rather than a money machine. Alongside it, a research harness has put eight ideas
through pre-registered tests that couldn't be bent after the fact: five failed, one
was real but too small to use, and two — a simple oversold-bounce rule on SPY —
passed on nearly nineteen years of out-of-sample data. Those two work, modestly,
and they still don't beat just owning the index. **That gap between "statistically
real" and "worth doing" is the most useful thing this project has learned.**
