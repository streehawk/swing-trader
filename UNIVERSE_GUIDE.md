# The Stock Universe — How It's Built and How to Change It

Plain-English answers to two questions. Last updated 29 July 2026.

---

## 1. How is the current universe created?

### The short answer

**It's a saved file that a script generates.** Not typed by hand, not fetched live.

The file is **`core_universe.json`**, sitting in the main project folder. Open it in
any text editor and you'll see 230 companies listed like this:

```json
{ "ticker": "MSFT", "annual_yield_pct": 0.7,
  "note": "OS + cloud + AI, maximum switching costs" }
```

### So are the tickers hardcoded?

**Half and half — and the distinction matters.**

- **On any given evening: yes, they're fixed.** The bot reads the file. It does not
  go looking for new companies while it scans.
- **But the file is generated, not authored.** A script wrote it, and re-running that
  script rewrites it.

Think of it like a shopping list. The list is fixed when you go to the shop — but you
wrote it from rules ("we need vegetables, nothing with nuts"), and you can rewrite it
any time.

### Where the 230 came from

The script is **`scripts/refresh_core_universe.py`**. When it runs, it:

1. Pulls a starting pool of roughly **3,500 US stocks**
2. Checks each one against a quality screen
3. Keeps the ones that pass
4. Writes the survivors into `core_universe.json`

The screen, as currently set:

| Test | Threshold |
|---|---|
| Company size | market value **≥ $5 billion** |
| Gross margin | **≥ 35%** — keeps a healthy slice of each sale |
| Revenue growth | **≥ 5% per year**, averaged over 5 years |
| *Escape hatch* | *or* a **Dividend Aristocrat** — 25+ straight years of raising its dividend gets in regardless |

**230 was never a target.** It's simply how many companies passed, last time it ran
(25 May 2026). Run it tomorrow and you might get 226, or 241.

### Manual overrides

The file has an `overrides` section that always wins over the screen:

```json
"overrides": { "force_include": [], "force_exclude": [] }
```

Put a ticker in `force_include` and it stays even if it fails the screen. Put one in
`force_exclude` and it's banned even if it passes. Both are currently empty.

### There's also a second, bigger list

**`us_universe_static.json`** holds **1,518** tickers — a much broader net with no
quality screen applied. The bot uses it only when you ask:

| What you type | What it scans |
|---|---|
| `/scan_now` | the 230 quality names |
| `/scan_now --full` | the 1,518 broad list (slower) |

---

## 2. How do I get a different universe?

### You do NOT need to hand me a list of tickers

**Rules are enough.** Say something like *"price above $20, average volume above 2
million, market value over $2 billion"* and the list gets generated from that. You
never have to name individual companies.

Naming specific tickers is also supported — it's just rarely what you want, because a
hand-typed list goes stale the moment a company changes.

### What already works today, with zero new code

Three of the four screen values are command-line options:

```bash
# Smaller companies, lower margin bar
python scripts/refresh_core_universe.py --min-cap 2 --min-gm 25 --min-rev 3

# See what would change without touching the file
python scripts/refresh_core_universe.py --dry-run

# Test the screen on a handful of names first
python scripts/refresh_core_universe.py --tickers AAPL,MSFT,NVDA
```

| Option | Meaning | Default |
|---|---|---|
| `--min-cap` | Minimum company size, in $ billions | 5 |
| `--min-gm` | Minimum gross margin, % | 35 |
| `--min-rev` | Minimum 5-year revenue growth, % | 5 |
| `--dry-run` | Show the changes, write nothing | off |

**Always use `--dry-run` first.** It tells you what would be added and removed before
anything is overwritten.

### What needs a small amount of new code

The current screen only knows about **size, margin and growth**. It has no concept of
price or trading volume. So these two would need adding:

| Rule you might want | Status |
|---|---|
| market value > $X | ✅ works now (`--min-cap`) |
| gross margin > X% | ✅ works now (`--min-gm`) |
| revenue growth > X% | ✅ works now (`--min-rev`) |
| **price > $20** | ⚠️ needs adding — roughly an hour |
| **average volume > 2M shares** | ⚠️ needs adding — same |
| specific sector only | ⚠️ needs adding |
| exclude sector | ⚠️ needs adding |

Adding price and volume filters is genuinely small: the screener already downloads
that data for every stock, it just doesn't test against it yet.

### So, what do you actually need to give me?

Just answer these:

1. **How big?** e.g. "over $2 billion" — this is the single biggest lever
2. **How liquid?** e.g. "at least 1 million shares a day"
3. **What price floor?** e.g. "above $20"
4. **Quality bar?** — margin and growth, or "don't care, I want everything liquid"
5. **Any sectors in or out?**
6. **Anything permanently banned?** (PLTR already is)

That's it. From those I generate the list, show you what it produced, and you approve
it before anything is switched over.

---

## Why this matters right now

The Phase 1B test (the "buy the dip in an uptrend" strategy) produced **zero trades in
five and a half years**. The reason was the universe, not the strategy:

> The setup needs a stock to be **5% below** its 50-day average *and* **5% above** its
> 20-day average on **1.5× normal volume**. Large, stable, high-quality companies —
> which is precisely what the 230 are — almost never do that. They dip gently and
> recover gently.

Across 293,361 trading days examined, only **17** came close, and none passed
completely.

The strategy came from a book whose author screened the *whole market*, including
smaller companies that swing far harder. So a fair test of it needs a different
universe: **smaller companies, lower quality bar, but still liquid enough to trade.**

Something like:

```bash
python scripts/refresh_core_universe.py --min-cap 2 --min-gm 20 --min-rev 0 --dry-run
```

plus the price and volume filters that still need adding.

⚠️ **One honest warning.** Widening the universe means more candidates but *worse*
candidates: smaller companies have wider spreads, so more of your profit disappears
into the cost of trading. And they're likelier to go bust — which the historical test
can't see, because the price data has no dead companies in it. A bigger universe makes
the strategy *testable*; it does not make it *profitable*.

---

## One-paragraph summary

The universe lives in `core_universe.json` — 230 companies, generated by
`scripts/refresh_core_universe.py`, which screens about 3,500 US stocks for size,
margin and growth. It's a saved file, so it's fixed while the bot runs, but it's
rebuilt whenever you re-run the script. **To change it you give me rules, not
tickers.** Size, margin and growth filters work today via command-line options; price
and volume filters need about an hour of work. Always run `--dry-run` first.
