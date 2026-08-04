#!/usr/bin/env python3
"""scripts/fetch_spy_extended.py — C-0a: maximum-history SPY daily bars.

Writes `data/validation_cache/spy_extended.parquet`. Deliberately a
DEDICATED cache: `prices.parquet` and `prices_p1b_final.parquet` belong to
the equity-universe work and start in 2020, and the C-series needs SPY
back to its 1993-01-29 inception to reach the book's in-sample window.

ADJUSTED vs RAW, registered choice: bars are dividend- and split-adjusted
(`auto_adjust=True`). A dip-buying strategy holds through ex-dividend
dates, and on raw prices every ex-div day prints as a loss the trader did
not actually take — that would understate the strategy AND distort RSI(2),
which is computed on exactly two days of change. The cost of adjusting is
that historical price LEVELS are restated; the C-series never uses levels,
only returns and indicator values, so this is the correct trade. The raw
close is stored alongside so the choice can be audited.

Gaps are reported, never bridged: a silently interpolated bar is a
fabricated trading day, and RSI(2) would treat it as real information.
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "validation_cache" / "spy_extended.parquet"

INCEPTION = "1993-01-01"      # SPY listed 1993-01-29; ask earlier, take what exists

log = logging.getLogger("spy_extended")


def fetch(ticker: str = "SPY", start: str = INCEPTION, retries: int = 3):
    """Download with retries. An empty frame from a rate-limited fetcher is
    indistinguishable from 'this ticker has no history' — that failure mode
    nearly closed Phase 1B on an API artifact, so it is retried, not
    accepted."""
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
        except Exception as e:
            last = f"{type(e).__name__}: {e}"
            if attempt < retries - 1:
                time.sleep(2.0 * (attempt + 1))
    raise RuntimeError(f"SPY fetch failed after {retries} attempts: {last}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(OUT))
    ap.add_argument("--ticker", default="SPY")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    import numpy as np
    import pandas as pd

    adj, raw = fetch(args.ticker)

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
    df["ticker"] = args.ticker
    df = df.sort_values("date").reset_index(drop=True)

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(args.out, index=False)

    # ── Coverage report (C-0a deliverable) ───────────────────────────
    # Gaps are counted in MISSING WEEKDAYS, not calendar days. A calendar
    # threshold silently forgives real closures: the 2001-09-11 shutdown
    # spans Mon->Mon, only 7 calendar days, but four lost sessions. Weekday
    # counting is still an approximation — it treats every public holiday
    # as a gap — so holidays are expected in the small-gap bucket and only
    # the large ones are interesting.
    d = df["date"]
    gaps = []
    for i in range(1, len(d)):
        prev, cur = d.iloc[i - 1], d.iloc[i]
        missing = int(np.busday_count(prev.date(), cur.date())) - 1
        if missing >= 2:
            gaps.append((str(prev.date()), str(cur.date()), missing))
    gaps.sort(key=lambda g: -g[2])
    big = [g for g in gaps if g[2] > 5]

    print(f"ticker        : {args.ticker}")
    print(f"first bar     : {d.min().date()}")
    print(f"last bar      : {d.max().date()}")
    print(f"total bars    : {len(df):,}")
    print(f"columns       : {list(df.columns)}")
    print(f"adjusted      : yes (auto_adjust=True); raw close kept as close_raw")
    if "close_raw" in df.columns:
        ratio = float(df['close'].iloc[-1] / df['close_raw'].iloc[-1])
        print(f"adj/raw (last): {ratio:.4f}  (1.0 = no adjustment left at the end)")
    print(f"gaps > 5 trading days : {len(big)}   <- the spec's flag threshold")
    for a, b, n in big:
        print(f"  !! {a} -> {b}  ({n} missing sessions)")
    print(f"gaps >= 2 sessions    : {len(gaps)} (mostly public holidays)")
    for a, b, n in gaps[:8]:
        print(f"  {a} -> {b}  ({n} missing sessions)")
    print("  nothing is bridged or interpolated — bars are exactly what SEC/"
          "exchange history contains")
    print(f"-> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
