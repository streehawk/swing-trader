"""
scripts/refresh_core_universe.py — Self-updating CORE quality screen.

Scans the full US universe (~3,500 tickers) and identifies quality compounders
that pass a systematic screen. Merges with the existing core_universe.json,
respecting force_include / force_exclude overrides.

Quality screen criteria (any company passing ALL of these is included):
  - market_cap       >= MIN_MARKET_CAP  (default $5B — smaller than DIP's $2B floor
                                         but we still want established companies)
  - gross_margin     >= MIN_GROSS_MARGIN (default 35%)
  - revenue_growth   >= MIN_REV_GROWTH   (5y CAGR, default 5%)
  OR is a Dividend Aristocrat (25+ consecutive years of dividend increases)

Additionally, force_include overrides always stay in.
force_exclude overrides always stay out (even if they pass the screen).

Usage:
  python scripts/refresh_core_universe.py                  # full screen
  python scripts/refresh_core_universe.py --dry-run        # show what would change, don't write
  python scripts/refresh_core_universe.py --min-cap 10     # set market cap floor to $10B
  python scripts/refresh_core_universe.py --tickers AAPL,MSFT  # test specific tickers

Triggered via:
  /admin_refresh_core_universe  (Telegram bot command)
  Monthly cron (optional — add to scheduler in main.py)
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

log = logging.getLogger("refresh_core_universe")

_UNIVERSE_FILE   = _REPO_ROOT / "core_universe.json"
_DEFAULT_MIN_CAP = 5_000_000_000   # $5B
_DEFAULT_GROSS_MARGIN = 35.0       # 35%
_DEFAULT_REV_GROWTH   =  5.0       # 5% 5y CAGR

# Known Dividend Aristocrats (S&P 500 members with 25+ consecutive years of dividend growth)
# Source: S&P Dividend Aristocrats Index. Updated 2026-05.
_DIVIDEND_ARISTOCRATS = {
    "MMM", "AOS", "ABT", "ABBV", "ACN", "ADM", "APD", "ALB", "AMCR", "ATO",
    "ADP", "BDX", "BRO", "CAH", "CAT", "CVX", "CB", "CHD", "CINF", "CTAS",
    "CLX", "KO", "CL", "ED", "GPC", "GWW", "ETN", "FRT", "GD", "HON",
    "HRL", "ITW", "JKHY", "KVUE", "KMB", "LOW", "MCD", "MDT", "MKC",
    "NDSN", "NTRS", "O", "OTIS", "PBCT", "PNR", "PEP", "PG", "PH",
    "PPG", "PPL", "ROP", "SHW", "SWK", "SYY", "TROW", "TGT", "GIS",
    "UPS", "VFC", "WMT", "EXPD", "ESS", "WST", "WM", "RSG", "ECL",
    "EMR", "AMGN", "AXP", "BDX", "BLK", "DOV", "IBM", "ITW", "MCO",
    "SPGI", "TXN",
}


def _load_universe() -> dict:
    if _UNIVERSE_FILE.exists():
        return json.loads(_UNIVERSE_FILE.read_text(encoding="utf-8"))
    return {"tickers": [], "overrides": {"force_include": [], "force_exclude": []}}


# ─────────────────────────────────────────────────────────────────────────────
# FUNNEL SCREEN (2026-07-30) — staged, dry-run-only universe rehearsal.
#
# A second, independent screen from the legacy quality screen above. It does
# NOT replace it and does NOT write core_universe.json: `run_funnel()` has no
# write path at all, so a rehearsal cannot clobber the live universe.
#
# Filters are applied in a fixed order, cheapest first, reporting the survivor
# count after each. Steps 1-5 and 7-9 come from a single yfinance `.info`
# call; step 6 (5-year revenue growth) needs the financial statements, which
# is a separate and much slower request, so it runs ONLY on names that already
# survived step 5.
# ─────────────────────────────────────────────────────────────────────────────

FUNNEL_MIN_CAP        = 2_000_000_000    # $2B
FUNNEL_MIN_PRICE      = 5.0              # Pezim's level (was 10.0)
FUNNEL_MIN_AVG_VOL    = 200_000          # shares
FUNNEL_MIN_ROE        = 0.10             # 10%
FUNNEL_MAX_DEBT_EQUITY = 1.0             # x (yfinance reports this as a percent)

# Sectors where a debt-to-equity test is meaningless: banks and insurers are
# leveraged by design. Per the brief, SKIP the test for them — do not exclude
# the sector.
_DE_EXEMPT_SECTORS = {"Financial Services", "Financials", "Financial"}

_BANNED_TICKERS = {"PLTR"}

# Leveraged / inverse products. Matched three ways because no single signal is
# reliable: an explicit ticker list, ticker-name patterns, and fund-name
# keywords from yfinance.
_LEVERAGED_TICKERS = {
    "TQQQ", "SQQQ", "UPRO", "SPXU", "SPXL", "SPXS", "UDOW", "SDOW",
    "TNA", "TZA", "SOXL", "SOXS", "LABU", "LABD", "FAS", "FAZ",
    "YINN", "YANG", "NUGT", "DUST", "JNUG", "JDST", "UVXY", "SVXY",
    "TMF", "TMV", "QLD", "SSO", "SDS", "QID", "PSQ", "SH", "DOG",
    "BOIL", "KOLD", "UCO", "SCO", "AGQ", "ZSL", "ERX", "ERY",
}
_LEVERAGED_NAME_KEYWORDS = (
    "ultra", "ultrapro", "3x", "2x", "-1x", "inverse", "short ",
    "bear ", "bull ", "leveraged", "daily target",
)


def _f(v) -> Optional[float]:
    """Coerce to float, or None. yfinance mixes None, '', and numerics."""
    try:
        if v is None or v == "":
            return None
        out = float(v)
        return out if out == out else None      # NaN check
    except (TypeError, ValueError):
        return None


def _fetch_funnel_metrics(ticker: str, *, retries: int = 2) -> Optional[dict]:
    """One `.info` call (plus `fast_info` fallbacks) per ticker.

    Fallbacks exist because yfinance omits fields unpredictably — `marketCap`
    and `debtToEquity` come back empty for large banks (JPM, BAC) and even for
    XOM, while `fast_info.market_cap` returns a value for all three. Treating
    those omissions as "fails the screen" would silently delete megacaps, so
    every field that has a second source uses it.
    """
    import time as _t
    for _attempt in range(retries):
      try:
        import yfinance as yf
        t = yf.Ticker(ticker)
        info = t.info or {}

        market_cap = _f(info.get("marketCap"))
        price = _f(info.get("currentPrice")) or _f(info.get("regularMarketPrice"))
        try:
            fi = t.fast_info
            if market_cap is None:
                market_cap = _f(getattr(fi, "market_cap", None))
            if price is None:
                price = _f(getattr(fi, "last_price", None))
        except Exception:
            pass

        # yfinance exposes a 10-day and a 3-month average, but no 20-day.
        # The 10-day is the closer proxy; the deviation is reported.
        avg_vol = (_f(info.get("averageVolume10days"))
                   or _f(info.get("averageVolume")))

        de_raw = _f(info.get("debtToEquity"))     # reported as a percent
        return {
            "ticker":      ticker,
            "name":        info.get("longName") or info.get("shortName") or ticker,
            "sector":      info.get("sector") or "",
            "quote_type":  info.get("quoteType") or "",
            "market_cap":  market_cap,
            "price":       price,
            "avg_volume":  avg_vol,
            "net_income":  _f(info.get("netIncomeToCommon")),
            "debt_equity": (de_raw / 100.0) if de_raw is not None else None,
            "roe":         _f(info.get("returnOnEquity")),
            "book_value":  _f(info.get("bookValue")),
        }
      except Exception as e:
        log.debug(f"  {ticker}: funnel metrics attempt {_attempt+1} failed — {e}")
        if _attempt < retries - 1:
            _t.sleep(1.0 * (_attempt + 1))
    return None


def _fetch_rev_growth_5y(ticker: str, *, retries: int = 3) -> Optional[float]:
    """5-year revenue CAGR, as a percent, from the income statement.

    Separate function because `.financials` is a distinct — and much slower —
    request than `.info`. It runs only on step-5 survivors.

    RETRIES ARE NOT OPTIONAL HERE. On a long run yfinance starts returning
    empty frames under throttling, which is indistinguishable from "this
    company has no income statement" at the call site. A 2026-07-31 run
    lost ALL 95 candidates at this stage and reported a 0-survivor universe;
    re-testing the same tickers minutes later returned full statements for
    every one. Without backoff, rate limiting silently masquerades as a
    screening result.
    """
    import time as _t
    for attempt in range(retries):
        try:
            import yfinance as yf
            fin = yf.Ticker(ticker).financials
            if fin is None or fin.empty or "Total Revenue" not in fin.index:
                if attempt < retries - 1:
                    _t.sleep(1.5 * (attempt + 1))
                    continue
                return None
            row = fin.loc["Total Revenue"].dropna()
            if len(row) < 2:
                return None
            oldest, newest = float(row.iloc[-1]), float(row.iloc[0])
            n_years = len(row) - 1
            if oldest <= 0 or newest <= 0 or n_years <= 0:
                return None
            return ((newest / oldest) ** (1 / n_years) - 1) * 100
        except Exception as e:
            log.debug(f"  {ticker}: rev growth attempt {attempt+1} failed — {e}")
            if attempt < retries - 1:
                _t.sleep(1.5 * (attempt + 1))
    return None


def _fetch_rev_growth_recent(ticker: str, *, retries: int = 3) -> tuple[Optional[float], str]:
    """MOST RECENT ANNUAL revenue growth, as a percent.

    Book fidelity: Pezim asks for sales that are growing *now*, not a
    5-year average — a company whose revenue fell last year still passes a
    positive 5-year CAGR, which is the opposite of what the screen means.
    So this compares the newest fiscal year against the one before it.

    Returns (growth_pct, source) where source is:
      "annual"    — from the income statement, newest FY vs prior FY
      "quarterly" — fallback to yfinance's `revenueGrowth`, which is
                    QUARTERLY year-over-year. Used only when the annual
                    statement is unavailable; counted and reported.
      "none"      — no data.
    Same retry/backoff as the 5y version: without it, throttling reads as
    "no revenue data" and silently empties the universe.
    """
    import time as _t
    for attempt in range(retries):
        try:
            import yfinance as yf
            t = yf.Ticker(ticker)
            fin = t.financials
            if fin is not None and not fin.empty and "Total Revenue" in fin.index:
                row = fin.loc["Total Revenue"].dropna()
                if len(row) >= 2:
                    newest, prior = float(row.iloc[0]), float(row.iloc[1])
                    if prior > 0 and newest > 0:
                        return (newest / prior - 1) * 100, "annual"
            # Annual statement unusable — fall back to quarterly YoY.
            q = _f((t.info or {}).get("revenueGrowth"))
            if q is not None:
                return q * 100, "quarterly"
            if attempt < retries - 1:
                _t.sleep(1.5 * (attempt + 1))
                continue
            return None, "none"
        except Exception as e:
            log.debug(f"  {ticker}: recent rev growth attempt {attempt+1} — {e}")
            if attempt < retries - 1:
                _t.sleep(1.5 * (attempt + 1))
    return None, "none"


def _is_leveraged_or_inverse(m: dict) -> bool:
    t = (m.get("ticker") or "").upper()
    if t in _LEVERAGED_TICKERS:
        return True
    name = (m.get("name") or "").lower()
    if m.get("quote_type") == "ETF" and any(k in name for k in _LEVERAGED_NAME_KEYWORDS):
        return True
    return any(k in name for k in ("ultrapro", "leveraged", " 3x", " 2x", "inverse"))


def _fetch_quality_metrics(ticker: str) -> Optional[dict]:
    """Fetch fundamental quality metrics for a single ticker via yfinance."""
    try:
        import yfinance as yf
        t = yf.Ticker(ticker)
        info = t.info or {}

        market_cap   = float(info.get("marketCap") or 0)
        gross_margin = float(info.get("grossMargins") or 0) * 100  # yfinance returns 0-1
        rev_growth   = float(info.get("revenueGrowth") or 0) * 100  # trailing 1y

        # 5y revenue CAGR approximation from financials
        try:
            fin = t.financials
            if fin is not None and not fin.empty and "Total Revenue" in fin.index:
                rev_row = fin.loc["Total Revenue"].dropna()
                if len(rev_row) >= 4:
                    oldest = float(rev_row.iloc[-1])
                    newest = float(rev_row.iloc[0])
                    n_years = len(rev_row) - 1
                    if oldest > 0 and newest > 0:
                        rev_growth_5y = ((newest / oldest) ** (1 / n_years) - 1) * 100
                    else:
                        rev_growth_5y = rev_growth
                else:
                    rev_growth_5y = rev_growth
            else:
                rev_growth_5y = rev_growth
        except Exception:
            rev_growth_5y = rev_growth

        # Dividend growth streak from dividends history
        div_streak = 0
        try:
            divs = t.dividends
            if divs is not None and not divs.empty:
                annual = divs.resample("Y").sum()
                streak = 0
                for i in range(len(annual) - 1, 0, -1):
                    if annual.iloc[i] > annual.iloc[i - 1]:
                        streak += 1
                    else:
                        break
                div_streak = streak
        except Exception:
            pass

        # Current dividend yield
        div_yield = float(info.get("dividendYield") or 0) * 100

        return {
            "ticker":         ticker,
            "market_cap":     market_cap,
            "gross_margin":   round(gross_margin, 2),
            "rev_growth_5y":  round(rev_growth_5y, 2),
            "div_streak":     div_streak,
            "div_yield":      round(div_yield, 2),
            "sector":         info.get("sector", ""),
            "name":           info.get("longName", ticker),
        }
    except Exception as e:
        log.debug(f"  {ticker}: metrics fetch failed — {e}")
        return None


def _passes_screen(
    m: dict,
    min_cap: float,
    min_gross_margin: float,
    min_rev_growth: float,
) -> tuple[bool, str]:
    """Return (passes, reason_string)."""
    ticker = m["ticker"]

    if m["market_cap"] < min_cap:
        return False, f"market_cap ${m['market_cap']/1e9:.1f}B < ${min_cap/1e9:.0f}B floor"

    # Dividend Aristocrat bypass — if 25+ years of consecutive increases, include regardless
    if ticker in _DIVIDEND_ARISTOCRATS or m["div_streak"] >= 25:
        return True, f"Dividend Aristocrat ({m['div_streak']} years)"

    # Quality screen
    if m["gross_margin"] >= min_gross_margin and m["rev_growth_5y"] >= min_rev_growth:
        return True, f"quality screen (GM={m['gross_margin']:.0f}%, rev_growth={m['rev_growth_5y']:.1f}%)"

    return False, (
        f"screen failed: GM={m['gross_margin']:.0f}% (need {min_gross_margin:.0f}%), "
        f"rev_growth={m['rev_growth_5y']:.1f}% (need {min_rev_growth:.0f}%)"
    )


def run_funnel(
    tickers_override: Optional[list[str]] = None,
    *,
    min_cap: float = FUNNEL_MIN_CAP,
    max_cap: Optional[float] = None,
    min_price: float = FUNNEL_MIN_PRICE,
    min_avg_vol: float = FUNNEL_MIN_AVG_VOL,
    min_roe: float = FUNNEL_MIN_ROE,
    max_de: float = FUNNEL_MAX_DEBT_EQUITY,
    limit: int = 0,
    progress_every: int = 100,
) -> dict:
    """Staged universe screen. DRY-RUN ONLY — never writes to disk.

    Missing data FAILS the filter rather than passing it. A screen that lets
    unknowns through is not a screen, and the alternative — silently keeping
    names we cannot verify — is how a "quality universe" fills up with
    companies nobody checked. The cost is that thin yfinance coverage
    understates the survivor count, which is reported per filter as
    `n_missing` so the reader can see where data, not fundamentals, did the
    cutting.
    """
    if tickers_override:
        pool = list(tickers_override)
    else:
        from core.universe import get_ml_universe
        pool = get_ml_universe()
    if limit:
        pool = pool[:limit]

    stages = [
        ("0. starting pool",                    len(pool)),
    ]
    log.info(f"[funnel] starting pool: {len(pool)} tickers")

    # ── Single cheap pass: one .info call per ticker ─────────────────────
    metrics: dict[str, dict] = {}
    n_fetch_fail = 0
    for i, t in enumerate(pool, 1):
        m = _fetch_funnel_metrics(t)
        if m is None:
            n_fetch_fail += 1
        else:
            metrics[t] = m
        if progress_every and i % progress_every == 0:
            log.info(f"[funnel]   fetched {i}/{len(pool)} ({n_fetch_fail} failed)")

    survivors = [t for t in pool if t in metrics]
    stages.append(("   data fetched", len(survivors)))

    # Band population BEFORE any filtering. Reported separately because a
    # cap-banded screen can only be as good as the pool's coverage of that
    # band: if the constituent list is skewed to megacaps, a thin result
    # says nothing about the strategy, only about the source data.
    band_pop = None
    if max_cap is not None:
        caps = [metrics[t].get("market_cap") for t in survivors]
        band_pop = {
            "in_band":     sum(1 for c in caps if c is not None and min_cap <= c < max_cap),
            "below_band":  sum(1 for c in caps if c is not None and c < min_cap),
            "above_band":  sum(1 for c in caps if c is not None and c >= max_cap),
            "cap_unknown": sum(1 for c in caps if c is None),
        }
        log.info(f"[funnel] cap band population (pre-filter): {band_pop}")

    missing: dict[str, int] = {}

    def _step(label: str, key: Optional[str], predicate) -> None:
        """Apply one filter to `survivors`, recording drops and data gaps."""
        nonlocal survivors
        kept, n_missing = [], 0
        for t in survivors:
            m = metrics[t]
            if key is not None and m.get(key) is None:
                n_missing += 1
                continue                      # missing data fails the filter
            if predicate(m):
                kept.append(t)
        missing[label] = n_missing
        survivors = kept
        stages.append((label, len(survivors)))
        log.info(f"[funnel] {label}: {len(survivors)} remain ({n_missing} dropped for missing data)")

    # 1. Market cap — a floor, or a BAND when max_cap is supplied.
    if max_cap is None:
        _step(f"1. market cap >= ${min_cap/1e9:.1f}B", "market_cap",
              lambda m: m["market_cap"] >= min_cap)
    else:
        _step(f"1. market cap ${min_cap/1e6:.0f}M-${max_cap/1e9:.0f}B", "market_cap",
              lambda m: min_cap <= m["market_cap"] < max_cap)
    # 2. Price
    _step(f"2. price >= ${min_price:.0f}", "price",
          lambda m: m["price"] >= min_price)
    # 3. Average volume
    _step(f"3. avg volume >= {min_avg_vol/1e3:.0f}k", "avg_volume",
          lambda m: m["avg_volume"] >= min_avg_vol)
    # 4. Exclusions (no data needed — a name we cannot identify is not banned)
    _step("4. exclusions (lev/inverse, PLTR)", None,
          lambda m: (m["ticker"].upper() not in _BANNED_TICKERS
                     and not _is_leveraged_or_inverse(m)))
    # 5. Profitable (TTM net income)
    _step("5. net income > 0 (TTM)", "net_income",
          lambda m: m["net_income"] > 0)

    # 6. Revenue growth — MOST RECENT ANNUAL, not a 5-year average.
    # The expensive stage: runs on step-5 survivors only.
    log.info(f"[funnel] fetching most-recent-annual revenue growth "
             f"for {len(survivors)} survivors...")
    n_missing_rev = 0
    n_quarterly_fallback = 0
    kept = []
    for i, t in enumerate(survivors, 1):
        g, src = _fetch_rev_growth_recent(t)
        metrics[t]["rev_growth_recent"] = g
        metrics[t]["rev_growth_source"] = src
        if src == "quarterly":
            n_quarterly_fallback += 1
        if g is None:
            n_missing_rev += 1
        elif g > 0:
            kept.append(t)
        if progress_every and i % progress_every == 0:
            log.info(f"[funnel]   revenue {i}/{len(survivors)}")
    label6 = "6. recent annual revenue growth > 0"
    missing[label6] = n_missing_rev
    survivors = kept
    stages.append((label6, len(survivors)))
    log.info(f"[funnel] revenue growth: {len(survivors)} remain "
             f"({n_missing_rev} missing, {n_quarterly_fallback} used quarterly YoY fallback)")

    # 7. Debt/equity — SKIPPED for financial-sector names, not excluded.
    kept, n_missing_de, n_exempt = [], 0, 0
    for t in survivors:
        m = metrics[t]
        if m.get("sector") in _DE_EXEMPT_SECTORS:
            n_exempt += 1
            kept.append(t)                    # exempt, not excluded
            continue
        if m.get("debt_equity") is None:
            n_missing_de += 1
            continue
        if m["debt_equity"] < max_de:
            kept.append(t)
    missing[f"7. debt/equity < {max_de:.0f} (fin. exempt)"] = n_missing_de
    survivors = kept
    stages.append((f"7. debt/equity < {max_de:.0f} (fin. exempt)", len(survivors)))
    log.info(f"[funnel] debt/equity: {len(survivors)} remain "
             f"({n_exempt} financials exempted, {n_missing_de} missing)")

    # 8. ROE
    _step(f"8. ROE > {min_roe*100:.0f}%", "roe", lambda m: m["roe"] > min_roe)
    # 9. Positive shareholder equity (book value per share is the cheap proxy)
    _step("9. shareholder equity > 0", "book_value", lambda m: m["book_value"] > 0)

    return {
        "stages":       stages,
        "missing":      missing,
        "survivors":    survivors,
        "metrics":      {t: metrics[t] for t in survivors},
        "n_fetch_fail": n_fetch_fail,
        "n_de_exempt":  n_exempt,
        "band_pop":     band_pop,
        "n_quarterly_fallback": n_quarterly_fallback,
        "thresholds": {
            "min_cap": min_cap, "min_price": min_price,
            "min_avg_vol": min_avg_vol, "min_roe": min_roe, "max_de": max_de,
        },
    }


def run_refresh(
    tickers_override: Optional[list[str]] = None,
    dry_run: bool = False,
    min_cap: float = _DEFAULT_MIN_CAP,
    min_gross_margin: float = _DEFAULT_GROSS_MARGIN,
    min_rev_growth: float = _DEFAULT_REV_GROWTH,
) -> dict:
    """
    Run the CORE quality screen and update core_universe.json.

    Returns summary dict:
      {added: [...], removed: [...], kept: int, skipped: int, errors: int, dry_run: bool}
    """
    data = _load_universe()
    existing = {t["ticker"]: t for t in data.get("tickers", [])}
    overrides = data.get("overrides", {})
    force_include = set(overrides.get("force_include", []))
    force_exclude = set(overrides.get("force_exclude", []))

    # Determine which tickers to screen
    if tickers_override:
        to_screen = list(tickers_override)
    else:
        # Full universe from universe.py (same ~3,500 as DIP/Momentum)
        try:
            from core.universe import get_ml_universe
            to_screen = get_ml_universe()
            log.info(f"Full universe: {len(to_screen)} tickers to screen")
        except Exception as e:
            log.warning(f"Could not load universe module ({e}) — using existing CORE list only")
            to_screen = list(existing.keys())

    n_total  = len(to_screen)
    passed   = {}   # ticker -> (metrics, reason)
    skipped  = 0
    errors   = 0

    for i, ticker in enumerate(to_screen):
        if i % 50 == 0:
            log.info(f"  [{i+1}/{n_total}] screening… (passed so far: {len(passed)})")

        if ticker in force_exclude:
            log.debug(f"  {ticker}: force_excluded")
            continue

        m = _fetch_quality_metrics(ticker)
        if m is None:
            errors += 1
            continue

        ok, reason = _passes_screen(m, min_cap, min_gross_margin, min_rev_growth)
        if ok:
            passed[ticker] = (m, reason)
            log.debug(f"  {ticker}: PASS — {reason}")
        else:
            log.debug(f"  {ticker}: FAIL — {reason}")
            skipped += 1

        time.sleep(0.05)  # gentle rate limiting

    # Add force_includes that weren't in the screen results
    for ticker in force_include:
        if ticker not in passed:
            if ticker in existing:
                passed[ticker] = (None, "force_include override")
            else:
                m = _fetch_quality_metrics(ticker)
                passed[ticker] = (m, "force_include override")

    # Compute diff
    added   = [t for t in passed if t not in existing]
    removed = [t for t in existing if t not in passed and t not in force_include]

    log.info(f"Screen complete: {len(passed)} pass, {skipped} fail, {errors} errors")
    log.info(f"Changes: +{len(added)} added, -{len(removed)} removed")

    if dry_run:
        log.info("[dry-run] Not writing — pass --no-dry-run to apply")
        return {
            "added":   added,
            "removed": removed,
            "kept":    len(passed) - len(added),
            "skipped": skipped,
            "errors":  errors,
            "dry_run": True,
        }

    # Build updated tickers list
    new_tickers = []
    for ticker, (m, reason) in passed.items():
        if ticker in existing:
            # Preserve existing entry but update yield if we have fresh data
            entry = dict(existing[ticker])
            if m and m.get("div_yield"):
                entry["annual_yield_pct"] = round(m["div_yield"], 2)
            new_tickers.append(entry)
        else:
            # New entry from screen
            div_yield = m["div_yield"] if m else 0.0
            name = m["name"] if m else ticker
            gm   = m["gross_margin"] if m else 0
            new_tickers.append({
                "ticker":           ticker,
                "annual_yield_pct": round(div_yield, 2),
                "note":             f"Auto-added by quality screen — {name} (GM {gm:.0f}%)",
            })

    new_tickers.sort(key=lambda x: x["ticker"])

    data["tickers"] = new_tickers
    data["_meta"]["generated"]   = __import__("datetime").date.today().isoformat()
    data["_meta"]["count"]       = len(new_tickers)
    data["_meta"]["source"]      = (
        f"Quality screen: gross_margin>={min_gross_margin:.0f}%, "
        f"rev_growth>={min_rev_growth:.0f}%, market_cap>=${min_cap/1e9:.0f}B. "
        f"Auto-refreshed {data['_meta']['generated']}."
    )

    _UNIVERSE_FILE.write_text(
        json.dumps(data, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    log.info(f"Written {len(new_tickers)} tickers to {_UNIVERSE_FILE}")

    return {
        "added":   added,
        "removed": removed,
        "kept":    len(passed) - len(added),
        "skipped": skipped,
        "errors":  errors,
        "dry_run": False,
    }


def _run_funnel_cli(args, tickers) -> None:
    """Render the funnel table. Writes no universe file under any argument."""
    res = run_funnel(
        tickers_override=tickers,
        min_cap=args.funnel_min_cap * 1e9,
        max_cap=(args.funnel_max_cap * 1e9) if args.funnel_max_cap else None,
        min_price=args.funnel_min_price,
        min_avg_vol=args.funnel_min_vol,
        min_roe=args.funnel_min_roe / 100.0,
        max_de=args.funnel_max_de,
        limit=args.limit,
    )

    stages, missing = res["stages"], res["missing"]
    start = stages[0][1] or 1

    if res.get("band_pop"):
        bp = res["band_pop"]
        print("\n" + "=" * 78)
        print("CAP BAND POPULATION (before any filtering)")
        print("=" * 78)
        tot = sum(bp.values()) or 1
        for k, v in bp.items():
            print(f"  {k:12s} {v:5d}  ({v/tot:5.1%})")

    print("\n" + "=" * 78)
    print("UNIVERSE DRY-RUN FUNNEL")
    print("SURVIVOR / CURRENT-FUNDAMENTALS / UPPER-BOUND-ONLY")
    print("=" * 78)
    print("\n| # | Filter | Remaining | Dropped | % of pool | Missing data |")
    print("|---|---|---|---|---|---|")
    prev = start
    for label, n in stages:
        drop = "" if label.startswith("0.") else str(prev - n)
        miss = missing.get(label)
        print(f"| | {label} | {n} | {drop} | {n/start:.1%} | {miss if miss else ''} |")
        prev = n

    print(f"\nFinal universe: {len(res['survivors'])} tickers "
          f"({len(res['survivors'])/start:.1%} of the starting pool)")
    print(f"Fetch failures : {res['n_fetch_fail']} (counted as dropped)")
    print(f"Financials exempted from debt/equity: {res['n_de_exempt']}")
    if res.get("n_quarterly_fallback"):
        print(f"Revenue growth via QUARTERLY YoY fallback (annual statement "
              f"unavailable): {res['n_quarterly_fallback']}")

    # Sample, sorted by market cap so the character of the list is visible.
    surv = sorted(res["metrics"].values(),
                  key=lambda m: -(m.get("market_cap") or 0))
    print("\n20 sample tickers (largest first):")
    print(f"  {'TICKER':8s} {'CAP':>9s} {'PRICE':>9s} {'ROE':>7s} {'D/E':>7s}  SECTOR")
    for m in surv[:20]:
        cap = f"${m['market_cap']/1e9:.0f}B" if m.get("market_cap") else "n/a"
        # Financials are exempted from the D/E test, so label them that way
        # even when yfinance happens to report a value — showing the number
        # would imply it was tested when it was not.
        if m.get("sector") in _DE_EXEMPT_SECTORS:
            de = "exempt"
        elif m.get("debt_equity") is not None:
            de = f"{m['debt_equity']:.2f}"
        else:
            de = "n/a"
        print(f"  {m['ticker']:8s} {cap:>9s} ${m['price']:>8.2f} "
              f"{(m['roe'] or 0)*100:>6.0f}% {de:>7s}  {m['sector'][:24]}")

    if args.json_out:
        Path(args.json_out).write_text(json.dumps({
            "generated":  datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "label":      "SURVIVOR / CURRENT-FUNDAMENTALS / UPPER-BOUND-ONLY",
            "dry_run":    True,
            "thresholds": res["thresholds"],
            "stages":     [{"filter": s, "remaining": n} for s, n in stages],
            "missing_data_drops": missing,
            "n_fetch_fail": res["n_fetch_fail"],
            "survivors":  res["survivors"],
        }, indent=2))
        print(f"\nJSON → {args.json_out}")

    print("\n[DRY RUN] core_universe.json was NOT modified.")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%H:%M:%S",
    )

    parser = argparse.ArgumentParser(description="Refresh CORE quality universe")
    parser.add_argument("--dry-run",      action="store_true", help="Show changes without writing")
    parser.add_argument("--tickers",      help="Comma-separated list to screen (test mode)")
    parser.add_argument("--tickers-file", default="",
                        help="JSON array of tickers to screen (for pools too large for --tickers)")
    parser.add_argument("--min-cap",      type=float, default=5.0, help="Min market cap in $B (default 5)")
    parser.add_argument("--min-gm",       type=float, default=35.0, help="Min gross margin %% (default 35)")
    parser.add_argument("--min-rev",      type=float, default=5.0,  help="Min 5y revenue growth %% (default 5)")
    # ── Funnel mode (dry-run only; never writes core_universe.json) ──────
    parser.add_argument("--funnel",       action="store_true",
                        help="Staged funnel screen. DRY RUN ONLY — never writes.")
    parser.add_argument("--funnel-min-cap",   type=float, default=2.0,  help="$B (default 2)")
    parser.add_argument("--funnel-max-cap",   type=float, default=0.0,
                        help="$B upper bound; 0 = no ceiling (floor-only screen)")
    parser.add_argument("--funnel-min-price", type=float, default=5.0)
    parser.add_argument("--funnel-min-vol",   type=float, default=200_000.0)
    parser.add_argument("--funnel-min-roe",   type=float, default=10.0, help="%% (default 10)")
    parser.add_argument("--funnel-max-de",    type=float, default=1.0,  help="x (default 1)")
    parser.add_argument("--limit",        type=int, default=0, help="Cap pool size (testing)")
    parser.add_argument("--json-out",     default="", help="Write funnel result JSON here")
    args = parser.parse_args()

    tickers = [t.strip().upper() for t in args.tickers.split(",")] if args.tickers else None
    if args.tickers_file:
        tickers = [str(t).strip().upper() for t in json.loads(Path(args.tickers_file).read_text())]

    if args.funnel:
        _run_funnel_cli(args, tickers)
        sys.exit(0)

    result = run_refresh(
        tickers_override=tickers,
        dry_run=args.dry_run,
        min_cap=args.min_cap * 1e9,
        min_gross_margin=args.min_gm,
        min_rev_growth=args.min_rev,
    )

    print(f"\nResult:")
    print(f"  Added:   {len(result['added'])} — {result['added'][:20]}")
    print(f"  Removed: {len(result['removed'])} — {result['removed'][:20]}")
    print(f"  Kept:    {result['kept']}")
    print(f"  Skipped: {result['skipped']} (failed screen)")
    print(f"  Errors:  {result['errors']} (data fetch failed)")
    if result["dry_run"]:
        print("\n  [DRY RUN] — run without --dry-run to apply changes")
