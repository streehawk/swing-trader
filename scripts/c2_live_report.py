#!/usr/bin/env python3
"""scripts/c2_live_report.py — the daily C2-live report. ANALYSIS ONLY.

Implements the appendix of `live_trading_protocol.md`. This script has no
brokerage connection, places no orders, and cannot move money. It reads
prices, reads a positions file you maintain from your own fills, and prints
what a person should do.

    # once, before the first trade
    python scripts/c2_live_report.py --init-positions

    # every evening after the 4pm ET close
    python scripts/c2_live_report.py

    # next morning, after the open, once you have recorded your fills
    python scripts/c2_live_report.py --reconcile

    # at the 3-month mark
    python scripts/c2_live_report.py --review

Data is cached to `data/live/prices.parquet` and refreshed on each run;
`--offline` reuses the cache without a network call.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np                                            # noqa: E402
import pandas as pd                                           # noqa: E402

from core.c2_live import (                                    # noqa: E402
    LIVE_INSTRUMENTS, build_report, load_positions, render_report,
    write_positions_template,
)
from core.c2_live_log import (                                # noqa: E402
    EQUITY_COLUMNS, LOG_COLUMNS, SKIP_COLUMNS, ensure_log, k2_status,
    k3_review, reconcile, render_review, render_validation, validate_log,
)

ROOT = Path(__file__).resolve().parent.parent
LIVE = ROOT / "data" / "live"
PRICES = LIVE / "prices.parquet"
POSITIONS = LIVE / "positions.json"
TRADE_LOG = LIVE / "trade_log.csv"
SKIP_LOG = LIVE / "skipped_signals.csv"
EQUITY_LOG = LIVE / "equity_weekly.csv"

FETCH_PERIOD = "3y"        # SMA200 needs 200 sessions; 3y is ample slack

log = logging.getLogger("c2_live")


def fetch_prices(tickers=LIVE_INSTRUMENTS, retries: int = 3) -> pd.DataFrame:
    """Daily adjusted bars for the 13 instruments.

    Same registered convention as every C-series cache: `auto_adjust=True`.
    An unadjusted ex-dividend day prints as a loss nobody took, and RSI(2)
    is computed on exactly two days of change — it would read that as a real
    signal.

    `open_raw` IS ALSO STORED, and it is what `--reconcile` compares your
    fill against. The two prices in a slippage calculation must be the same
    kind of price: your fill is what you actually paid, unadjusted, so the
    reference must be the unadjusted opening print.

    Adjusted and raw agree until a dividend goes ex, at which point
    yfinance retroactively scales every PRIOR bar down. Measured on this
    basket: XLE bars from six months ago differ by -1.36%, XLI by -0.53%,
    XLK by -0.24%. Reconciling an old fill against an adjusted open would
    therefore manufacture up to 1.4% of slippage — more than five times
    the 0.25% K2 limit — and pause live trading over a dividend.
    """
    import yfinance as yf

    frames, failures = [], []
    for t in tickers:
        last = ""
        for attempt in range(retries):
            try:
                df = yf.download(t, period=FETCH_PERIOD, auto_adjust=True,
                                 progress=False, actions=False)
                if df is None or len(df) == 0:
                    last = "empty frame"
                    raise ValueError(last)
                if isinstance(df.columns, pd.MultiIndex):
                    df.columns = df.columns.get_level_values(0)
                df = df.rename(columns=str.lower)[
                    ["open", "high", "low", "close", "volume"]].copy()
                df.index.name = "date"
                df = df.reset_index()
                df["date"] = pd.to_datetime(df["date"]).dt.tz_localize(None)

                raw = yf.download(t, period=FETCH_PERIOD, auto_adjust=False,
                                  progress=False, actions=False)
                if raw is not None and len(raw):
                    if isinstance(raw.columns, pd.MultiIndex):
                        raw.columns = raw.columns.get_level_values(0)
                    r = raw.rename(columns=str.lower)[["open"]].copy()
                    r.index.name = "date"
                    r = r.reset_index()
                    r["date"] = pd.to_datetime(r["date"]).dt.tz_localize(None)
                    df = df.merge(r.rename(columns={"open": "open_raw"}),
                                  on="date", how="left")

                df["ticker"] = t
                frames.append(df)
                break
            except Exception as e:                            # noqa: BLE001
                last = f"{type(e).__name__}: {e}"
                if attempt < retries - 1:
                    time.sleep(1.5 * (attempt + 1))
        else:
            failures.append((t, last))
        time.sleep(0.25)

    if failures:
        for t, e in failures:
            log.warning("!! %s fetch failed: %s", t, e)
    if not frames:
        raise RuntimeError("no price data fetched — cannot build a report")
    return pd.concat(frames, ignore_index=True)


def to_by_ticker(df: pd.DataFrame) -> dict[str, pd.DataFrame]:
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"])
    cols = [c for c in ("open", "high", "low", "close", "volume", "open_raw")
            if c in df.columns]
    return {str(t): g.sort_values("date").set_index("date")[cols].copy()
            for t, g in df.groupby("ticker", sort=True)}


def load_prices(offline: bool) -> dict[str, pd.DataFrame]:
    if offline:
        if not PRICES.exists():
            raise FileNotFoundError(f"{PRICES} not found — run once without "
                                    f"--offline first")
        log.info("using cached prices (--offline)")
        return to_by_ticker(pd.read_parquet(PRICES))
    df = fetch_prices()
    PRICES.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(PRICES, index=False)
    return to_by_ticker(df)


def cmd_init(args) -> int:
    p = write_positions_template(args.positions)
    ensure_log(args.log, LOG_COLUMNS)
    ensure_log(SKIP_LOG, SKIP_COLUMNS)
    ensure_log(EQUITY_LOG, EQUITY_COLUMNS)
    print(f"created {p}")
    print(f"created {args.log}          (protocol §7 — one row per FILL)")
    print(f"created {SKIP_LOG}   (§7 — skipped signals and why)")
    print(f"created {EQUITY_LOG}    (§7 — account equity every Friday close)")
    print()
    print("Next: open the positions file, delete the two example rows, and set")
    print("`account_equity` / `slot_dollars` / `equity_as_of` to your real values.")
    return 0


def cmd_report(args) -> int:
    bars = load_prices(args.offline)
    pos = load_positions(args.positions)
    r = build_report(bars, pos)
    print(render_report(r))

    k2 = k2_status(args.log)
    if k2["enough_data"]:
        flag = "  !! K2 BREACHED — pause and investigate" if k2["breached"] else ""
        print(f"\nK2 slippage (last {k2['window']} fills): "
              f"{k2['mean_slippage_pct']:+.3f}% vs limit "
              f"{k2['limit_pct']}%{flag}")
    elif k2["fills_with_slippage"]:
        print(f"\nK2 slippage: {k2['fills_in_window']}/{k2['window']} fills "
              f"logged so far — not enough to judge yet")

    if args.out:
        Path(args.out).write_text(json.dumps(r, indent=2, default=str))
        print(f"\n-> {args.out}")
    return 0


def cmd_reconcile(args) -> int:
    bars = load_prices(args.offline)
    res = reconcile(args.log, bars)
    print(f"trade log rows        : {res['rows']}")
    print(f"reconciled this run   : {res['updated']}")
    for u in res["unresolved"]:
        print(f"   !! {u}")
    k2 = k2_status(args.log)
    print(f"\nK2: {k2['fills_in_window']}/{k2['window']} fills in window, "
          f"mean slippage {k2['mean_slippage_pct']}% "
          f"(limit {k2['limit_pct']}%)")
    if k2["breached"]:
        print("!! K2 BREACHED — protocol §6: pause and investigate before "
              "continuing.")
    return 0


def cmd_check(args) -> int:
    """Validate the two hand-maintained files against each other."""
    try:
        pos = load_positions(args.positions)
    except (FileNotFoundError, ValueError) as e:
        print(f"positions.json: {e}")
        return 1
    try:
        bars = load_prices(offline=True)
    except FileNotFoundError:
        bars = None
    problems = validate_log(args.log, pos, bars)
    print(render_validation(problems))
    print()
    print(f"positions: {len(pos.open)} open, {len(pos.pending_entries)} pending "
          f"entr(y/ies), {len(pos.pending_exits)} pending exit(s) — "
          f"{pos.slots_used}/3 slots used")
    return 1 if any(p["severity"] == "ERROR" for p in problems) else 0


def cmd_review(args) -> int:
    pos = load_positions(args.positions)
    r = k3_review(args.log, pos.account_equity, start_date=args.start)
    print(render_review(r))
    if args.out:
        Path(args.out).write_text(json.dumps(r, indent=2, default=str))
        print(f"-> {args.out}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--init-positions", action="store_true",
                    help="write the positions-file template and empty logs")
    ap.add_argument("--reconcile", action="store_true",
                    help="fill in true slippage against the actual opens")
    ap.add_argument("--review", action="store_true", help="the K3 review table")
    ap.add_argument("--check", action="store_true",
                    help="validate positions.json and trade_log.csv")
    ap.add_argument("--offline", action="store_true",
                    help="reuse the cached prices, no network call")
    ap.add_argument("--positions", default=str(POSITIONS))
    ap.add_argument("--log", default=str(TRADE_LOG))
    ap.add_argument("--start", default="", help="live start date, for --review")
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if args.init_positions:
        return cmd_init(args)
    if args.check:
        return cmd_check(args)
    if args.reconcile:
        return cmd_reconcile(args)
    if args.review:
        return cmd_review(args)
    return cmd_report(args)


if __name__ == "__main__":
    raise SystemExit(main())
