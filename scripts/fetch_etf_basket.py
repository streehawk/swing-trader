#!/usr/bin/env python3
"""scripts/fetch_etf_basket.py — A1-1: daily history for the 12-ETF basket.

Writes `data/validation_cache/etf_basket.parquet`. A dedicated cache, for
the same reason SPY got one: the equity caches start in 2020, and this
addendum needs each ETF back to its inception so the FULL window is real
rather than an artefact of when someone happened to build a cache.

REGISTERED CONVENTIONS, inherited from `scripts/fetch_spy_extended.py`
verbatim so the basket and the SPY control are measured the same way:

  - Dividend/split adjusted (`auto_adjust=True`), raw close kept as
    `close_raw` so the choice can be audited. Sector ETFs pay meaningful
    dividends — XLU and XLP especially — and on raw prices every ex-div
    day prints as a loss the holder did not take. RSI(2) is computed on
    exactly two days of change, so an unadjusted ex-div day is not a
    rounding error to that indicator, it is a fabricated signal.
  - Gaps are REPORTED, NEVER BRIDGED. An interpolated bar is a trading
    day that did not happen, and the strategy would trade it.
  - Gaps counted in MISSING WEEKDAYS, not calendar days — the 2001-09-11
    shutdown spans Mon->Mon, 7 calendar days but four lost sessions.

SPY IS NOT FETCHED HERE. The addendum excludes it from the basket, and
the control run deliberately reads the *registered* `spy_extended.parquet`
rather than a fresh download: if the pipeline control failed because
yfinance restated a 1990s bar, that would look identical to a pipeline
bug. `--spy-drift-check` re-fetches SPY into a scratch file and diffs it
against the registered cache, which answers the data question separately
from the pipeline question.
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "data" / "validation_cache"
OUT = CACHE / "etf_basket.parquet"
SPY_REGISTERED = CACHE / "spy_extended.parquet"

# The basket, registered in `c2_etf_basket_addendum.md`. No swaps, no
# additions, no post-hoc removals. Inception dates are the addendum's,
# kept here so a fetch that starts materially later is visible as a
# discrepancy instead of being quietly accepted.
BASKET = [
    ("QQQ", "Nasdaq-100",    "1999-03"),
    ("DIA", "Dow 30",        "1998-01"),
    ("IWM", "Russell 2000",  "2000-05"),
    ("XLB", "Materials",     "1998-12"),
    ("XLE", "Energy",        "1998-12"),
    ("XLF", "Financials",    "1998-12"),
    ("XLI", "Industrials",   "1998-12"),
    ("XLK", "Technology",    "1998-12"),
    ("XLP", "Staples",       "1998-12"),
    ("XLU", "Utilities",     "1998-12"),
    ("XLV", "Health care",   "1998-12"),
    ("XLY", "Discretionary", "1998-12"),
]

START = "1990-01-01"      # ask earlier than any inception; take what exists
POST_BOOK_START = "2008-01-01"
WARMUP_BARS = 200         # SMA200 needs 200 closes; first usable bar is index 199

log = logging.getLogger("etf_basket")


def fetch_one(ticker: str, start: str = START, retries: int = 3):
    """Download one ticker, adjusted and raw. Retries before believing a
    failure: an empty frame from a rate-limited fetcher is indistinguishable
    from 'this ticker has no history', and that exact ambiguity nearly closed
    Phase 1B on an API artefact."""
    import pandas as pd
    import yfinance as yf

    last = ""
    for attempt in range(retries):
        try:
            adj = yf.download(ticker, start=start, auto_adjust=True,
                              progress=False, actions=False)
            raw = yf.download(ticker, start=start, auto_adjust=False,
                              progress=False, actions=False)
            if adj is None or len(adj) == 0:
                last = "empty frame"
                raise ValueError(last)
            if isinstance(adj.columns, pd.MultiIndex):
                adj.columns = adj.columns.get_level_values(0)
            if raw is not None and len(raw) and isinstance(raw.columns, pd.MultiIndex):
                raw.columns = raw.columns.get_level_values(0)
            return adj, raw
        except Exception as e:                       # noqa: BLE001
            last = f"{type(e).__name__}: {e}"
            if attempt < retries - 1:
                time.sleep(2.0 * (attempt + 1))
    raise RuntimeError(f"{ticker} fetch failed after {retries} attempts: {last}")


def to_frame(ticker: str, adj, raw):
    import pandas as pd

    df = adj.rename(columns=str.lower)[["open", "high", "low", "close", "volume"]].copy()
    df.index.name = "date"
    df = df.reset_index()
    df["date"] = pd.to_datetime(df["date"]).dt.tz_localize(None)
    if raw is not None and len(raw):
        r = raw.rename(columns=str.lower)[["close"]].copy()
        r.index.name = "date"
        r = r.reset_index()
        r["date"] = pd.to_datetime(r["date"]).dt.tz_localize(None)
        df = df.merge(r.rename(columns={"close": "close_raw"}), on="date", how="left")
    df["ticker"] = ticker
    return df.sort_values("date").reset_index(drop=True)


def session_gaps(dates, min_missing: int = 2):
    """Gaps in MISSING WEEKDAYS. Holidays land in the small bucket by
    construction — only the large ones mean anything."""
    import numpy as np

    gaps = []
    for i in range(1, len(dates)):
        prev, cur = dates.iloc[i - 1], dates.iloc[i]
        missing = int(np.busday_count(prev.date(), cur.date())) - 1
        if missing >= min_missing:
            gaps.append((str(prev.date()), str(cur.date()), missing))
    gaps.sort(key=lambda g: -g[2])
    return gaps


def coverage(df, ticker: str, expected_inception: str) -> dict:
    """The A1-1 deliverable, per ticker."""
    import pandas as pd

    d = df["date"]
    first_usable = (str(d.iloc[WARMUP_BARS - 1].date())
                    if len(d) >= WARMUP_BARS else None)
    gaps = session_gaps(d)
    big = [g for g in gaps if g[2] > 5]

    post = df[df["date"] >= pd.Timestamp(POST_BOOK_START)]
    # POST-BOOK coverage is only real if the warm-up finished BEFORE the
    # window opens — otherwise the first trades of 2008 would run on a
    # half-formed SMA200.
    usable_before_post = (first_usable is not None
                          and first_usable < POST_BOOK_START)

    exp = pd.Timestamp(expected_inception + "-01")
    return {
        "ticker": ticker,
        "first_bar": str(d.min().date()),
        "last_bar": str(d.max().date()),
        "total_bars": int(len(df)),
        "first_usable_bar": first_usable,
        "expected_inception": expected_inception,
        "inception_matches": bool(abs((d.min() - exp).days) <= 45),
        "post_book_bars": int(len(post)),
        "post_book_usable": bool(usable_before_post and len(post) > 0),
        "gaps_gt_5_sessions": [
            {"from": a, "to": b, "missing_sessions": n} for a, b, n in big
        ],
        "n_gaps_ge_2_sessions": len(gaps),
        "largest_gaps": [
            {"from": a, "to": b, "missing_sessions": n} for a, b, n in gaps[:3]
        ],
    }


def spy_drift_check(out_dir: Path) -> dict:
    """Re-fetch SPY and diff against the registered cache.

    Separates two failure modes that look identical from inside the control
    assertion: 'the new pipeline computes something different' versus
    'yfinance restated a bar since 2026-08-02'. Reported, never used to
    modify the registered cache.
    """
    import numpy as np
    import pandas as pd

    if not SPY_REGISTERED.exists():
        return {"checked": False, "reason": f"{SPY_REGISTERED} missing"}

    reg = pd.read_parquet(SPY_REGISTERED)
    reg["date"] = pd.to_datetime(reg["date"])
    adj, raw = fetch_one("SPY", start="1993-01-01")
    fresh = to_frame("SPY", adj, raw)

    m = reg[["date", "close"]].merge(
        fresh[["date", "close"]], on="date", how="outer",
        suffixes=("_registered", "_fresh"), indicator=True)
    only_reg = int((m["_merge"] == "left_only").sum())
    only_fresh = int((m["_merge"] == "right_only").sum())
    both = m[m["_merge"] == "both"].copy()
    both["rel"] = (both["close_fresh"] - both["close_registered"]).abs() / \
                  both["close_registered"].abs().clip(lower=1e-12)
    worst = float(both["rel"].max()) if len(both) else 0.0
    n_moved = int((both["rel"] > 1e-6).sum())

    return {
        "checked": True,
        "registered_bars": int(len(reg)),
        "fresh_bars": int(len(fresh)),
        "dates_only_in_registered": only_reg,
        "dates_only_in_fresh": only_fresh,
        "bars_with_changed_close": n_moved,
        "worst_relative_change": round(worst, 10),
        "verdict": ("IDENTICAL" if (only_reg == 0 and only_fresh == 0 and n_moved == 0)
                    else "DRIFTED — see counts; registered cache is NOT modified"),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(OUT))
    ap.add_argument("--start", default=START)
    ap.add_argument("--spy-drift-check", action="store_true",
                    help="also re-fetch SPY and diff vs the registered cache")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    import json

    import pandas as pd

    frames, reports, failures = [], [], []
    for i, (tkr, what, inception) in enumerate(BASKET, 1):
        try:
            adj, raw = fetch_one(tkr, start=args.start)
            df = to_frame(tkr, adj, raw)
            frames.append(df)
            reports.append({**coverage(df, tkr, inception), "what": what})
            print(f"  [{i:2d}/12] {tkr:4s} {what:<14s} "
                  f"{df['date'].min().date()} -> {df['date'].max().date()}  "
                  f"{len(df):,} bars")
        except Exception as e:                       # noqa: BLE001
            failures.append({"ticker": tkr, "error": f"{type(e).__name__}: {e}"})
            print(f"  [{i:2d}/12] {tkr:4s} FAILED: {e}")
        time.sleep(0.4)

    if not frames:
        print("no data fetched — aborting, nothing written")
        return 1

    out = pd.concat(frames, ignore_index=True).sort_values(
        ["ticker", "date"]).reset_index(drop=True)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    out.to_parquet(args.out, index=False)

    # ── The A1-1 report ──────────────────────────────────────────────
    print()
    print("=" * 98)
    print("A1-1 — DATA COVERAGE (12-ETF basket)")
    print("=" * 98)
    print(f"{'Ticker':<7}{'What':<15}{'First bar':<12}{'Last bar':<12}"
          f"{'Bars':>8}  {'First usable':<13}{'POST-BOOK':>10}  {'Gaps>5':>7}")
    print("-" * 98)
    for r in reports:
        print(f"{r['ticker']:<7}{r['what']:<15}{r['first_bar']:<12}"
              f"{r['last_bar']:<12}{r['total_bars']:>8,}  "
              f"{str(r['first_usable_bar']):<13}"
              f"{r['post_book_bars']:>10,}  "
              f"{len(r['gaps_gt_5_sessions']):>7}")
    print("-" * 98)

    flagged = [r for r in reports if r["gaps_gt_5_sessions"]]
    if flagged:
        print("\n!! GAPS > 5 SESSIONS (flagged, NOT bridged):")
        for r in flagged:
            for g in r["gaps_gt_5_sessions"]:
                print(f"   {r['ticker']:4s} {g['from']} -> {g['to']}  "
                      f"({g['missing_sessions']} missing sessions)")
    else:
        print("\nNo gaps > 5 sessions in any instrument.")

    bad_inception = [r for r in reports if not r["inception_matches"]]
    if bad_inception:
        print("\n!! First bar differs from the registered inception by > 45 days:")
        for r in bad_inception:
            print(f"   {r['ticker']:4s} registered {r['expected_inception']}, "
                  f"got {r['first_bar']}")

    not_covered = [r for r in reports if not r["post_book_usable"]]
    print(f"\nPOST-BOOK ({POST_BOOK_START} -> present) coverage: "
          f"{len(reports) - len(not_covered)}/{len(reports)} instruments "
          f"usable (SMA200 warm-up complete before the window opens)")
    for r in not_covered:
        print(f"   !! {r['ticker']}: first usable {r['first_usable_bar']}")

    if failures:
        print(f"\n!! {len(failures)} FETCH FAILURES:")
        for f in failures:
            print(f"   {f['ticker']}: {f['error']}")

    drift = None
    if args.spy_drift_check:
        print("\n" + "=" * 98)
        print("SPY DATA DRIFT CHECK (diagnostic — the registered cache is not touched)")
        print("=" * 98)
        drift = spy_drift_check(CACHE)
        for k, v in drift.items():
            print(f"  {k:28s}: {v}")

    meta = {
        "generated": pd.Timestamp.utcnow().isoformat(),
        "source": "yfinance, auto_adjust=True (raw close retained as close_raw)",
        "basket": [{"ticker": t, "what": w, "inception": i} for t, w, i in BASKET],
        "coverage": reports,
        "failures": failures,
        "spy_drift_check": drift,
        "gaps_policy": "reported in missing weekdays, never bridged or interpolated",
    }
    meta_path = Path(args.out).with_suffix(".meta.json")
    meta_path.write_text(json.dumps(meta, indent=2))

    print(f"\n-> {args.out}  ({len(out):,} rows, {out['ticker'].nunique()} tickers)")
    print(f"-> {meta_path}")
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
