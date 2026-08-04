#!/usr/bin/env python3
"""scripts/fetch_earnings_dates.py — point-in-time earnings dates from EDGAR.

Builds `data/validation_cache/earnings_dates.parquet` (ticker, date) from
**8-K item 2.02** filings — "Results of Operations and Financial
Condition", the form a US issuer files when it releases quarterly
results. The filing date IS the announcement date, recorded by the SEC on
the day it happened, so this is a genuine point-in-time source and it is
free. No paid vendor, no reconstruction.

Why this script exists at all: H2's registered entry rules say "No
earnings within 10 days AND no earnings in the prior 2 days (exclude
earnings-gap events themselves — this isolates non-earnings strength;
earnings drift would be a different hypothesis)". Every earlier run in
this project disabled the earnings gate for want of a calendar, which was
tolerable when the gate merely removed some trades. For H2 it is not
tolerable: without it, half the signal is earnings gaps, and H2 would be
silently testing H3. So the gate gets implemented rather than skipped.

The existing `edgar_8k.parquet` was built for the 230-name spine and
covers only 93 of H2's 407 tickers. Applying an earnings filter to 23% of
a universe and not the rest biases the sample BY TICKER, which is worse
than applying it to none. Hence a uniform refetch.

KNOWN APPROXIMATION, stated because it is a real one: at signal time a
live trader knows the SCHEDULED earnings date (companies pre-announce it
weeks ahead). The backtest instead uses the date the report actually
landed. Those are almost always the same day, and where they differ the
effect is to remove a trade that live would also have removed. The
approximation can therefore only make the tested strategy MORE
restrictive than the live one — it cannot manufacture an edge.

Usage:
    python scripts/fetch_earnings_dates.py                  # 407 universe
    python scripts/fetch_earnings_dates.py --tickers A,B,C
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "validation_cache" / "earnings_dates.parquet"

# The window every backtest in this project runs over. Filings older than
# this are pure fetch cost — EDGAR carries some issuers back to 1994.
START_ISO = "2020-06-01"

log = logging.getLogger("earnings_dates")


def _universe() -> list[str]:
    data = json.loads((ROOT / "core_universe.json").read_text())
    return sorted({t["ticker"] if isinstance(t, dict) else t
                   for t in data["tickers"]})


def fetch_one(ticker: str, *, retries: int = 3) -> tuple[list[str], str]:
    """All 8-K item-2.02 filing dates for one ticker since START_ISO.

    Returns (dates, status). Retries with backoff: a transient EDGAR
    failure returning an empty list would look exactly like "this company
    never reported", and that silent-zero failure mode is what nearly
    closed Phase 1B on an API artifact.
    """
    from data.fundamental_signals import _edgar_submissions

    last_err = ""
    for attempt in range(retries):
        try:
            sub = _edgar_submissions(ticker) or {}
            recent = sub.get("filings", {}).get("recent", {})
            forms = recent.get("form", [])
            filed = recent.get("filingDate", [])
            items = recent.get("items", [])
            if not forms:
                last_err = "empty submissions"
                raise ValueError(last_err)

            out = []
            for j, form in enumerate(forms):
                if not str(form).startswith("8-K"):
                    continue
                d = (filed[j] or "")[:10] if j < len(filed) else ""
                if not d or d < START_ISO:
                    continue
                it = items[j] if j < len(items) else ""
                it = it if isinstance(it, str) else ",".join(map(str, it))
                if "2.02" in it:
                    out.append(d)
            return sorted(set(out)), "ok"
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            if attempt < retries - 1:
                time.sleep(1.5 * (attempt + 1))
    return [], last_err


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tickers", default="")
    ap.add_argument("--out", default=str(OUT))
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    import pandas as pd

    tickers = ([t.strip().upper() for t in args.tickers.split(",") if t.strip()]
               if args.tickers else _universe())

    rows, failed, empty = [], [], []
    t0 = time.time()
    for i, t in enumerate(tickers, 1):
        dates, status = fetch_one(t)
        if status != "ok":
            failed.append((t, status))
        elif not dates:
            empty.append(t)
        for d in dates:
            rows.append({"ticker": t, "date": d})
        if i % 25 == 0:
            log.info(f"  {i}/{len(tickers)} tickers, {len(rows)} filings, "
                     f"{len(failed)} failed ({time.time()-t0:.0f}s)")
        time.sleep(0.20)          # SEC ceiling is 10/s; we use 5/s

    df = pd.DataFrame(rows)
    if not df.empty:
        df["date"] = pd.to_datetime(df["date"])
        df = df.drop_duplicates().sort_values(["ticker", "date"])
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(args.out, index=False)

    covered = df["ticker"].nunique() if not df.empty else 0
    per = df.groupby("ticker").size() if not df.empty else pd.Series(dtype=int)
    log.info("")
    log.info(f"tickers requested : {len(tickers)}")
    log.info(f"tickers covered   : {covered} "
             f"({covered/max(1,len(tickers)):.1%})")
    log.info(f"filings           : {len(df)}")
    if len(per):
        log.info(f"filings/ticker    : median {per.median():.0f}, "
                 f"min {per.min()}, max {per.max()}")
        log.info(f"tickers with >=16 : {(per >= 16).sum()} "
                 f"(4y of quarters — full coverage)")
    if empty:
        log.info(f"no 2.02 filings   : {len(empty)} — {', '.join(empty[:20])}")
    if failed:
        log.info(f"FETCH FAILURES    : {len(failed)}")
        for t, why in failed[:20]:
            log.info(f"  {t}: {why}")
    log.info(f"-> {args.out}  ({time.time()-t0:.0f}s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
