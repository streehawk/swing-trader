"""strategies/filters.py — Reusable entry filters (Phase 1B).

Standalone, side-effect-free predicates shared across strategy modules.
Each takes an OHLCV frame and returns a bool. All of them FAIL CLOSED:
when the data needed to answer the question is missing, the answer is
False, never True. A filter that guesses is worse than no filter.

Column names are matched case-insensitively so a caller can pass either
the Capitalised frames yfinance produces or the lowercase ones the
backtest cache uses.

Tickets: P1B-2 (average volume), P1B-3 (relative volume),
P1B-4 (earnings gate — delegates to the same primitives Phase 1 uses).
"""
from __future__ import annotations

import logging
from bisect import bisect_left
from typing import Optional

import numpy as np
import pandas as pd

log = logging.getLogger("strategies.filters")

# ── P1B-2 / P1B-3 tunables ───────────────────────────────────────────
MIN_AVG_VOL     = 200_000    # shares, 20-day mean (P1B-2)
AVG_VOL_WINDOW  = 20
MIN_RVOL        = 1.5        # today vs the prior-50-day mean (P1B-3)
RVOL_LOOKBACK   = 50


# ── Instrument exclusions (edge-search protocol guardrail) ───────────
#
# Pre-registered in the Phase 1B postmortem, not tuning: GAB and BDJ —
# closed-end funds — passed the fundamental funnel because a CEF reports
# fund-level net income and book value exactly like an operating company
# does. They are not businesses; a strategy premised on a company's
# operating recovery has no thesis on a leveraged bond portfolio's NAV
# discount. The leveraged/inverse ETF filter did not catch them because
# their tickers carry none of its hints.
#
# A name-list is honest about what it is: incomplete. It covers the CEFs
# that actually reached a screen in this project plus the largest of the
# class; a fund not on it can still slip through. The alternative — an
# `is_operating_company` flag — needs a fundamentals field free sources
# do not reliably carry, so it would fail open, which is worse.
_CLOSED_END_FUNDS = frozenset({
    # Reached the Phase 1B funnel:
    "GAB", "BDJ",
    # Gabelli / BlackRock / Eaton Vance / PIMCO / Cohen & Steers majors:
    "GGT", "GUT", "GDV", "GAM", "GLU", "GNT", "ECC", "OXLC",
    "BST", "BSTZ", "BME", "BMEZ", "BCAT", "BUI", "BOE", "BGY", "BGR",
    "BKT", "BTZ", "BLW", "BIT", "HYT", "MUI", "MUC", "DSU",
    "ETV", "ETY", "ETW", "ETB", "ETJ", "EXG", "EOS", "EOI", "EVT", "ETG",
    "PDI", "PDO", "PTY", "PCN", "PFN", "PFL", "PHK", "RCS", "PCM",
    "UTF", "UTG", "RQI", "RNP", "RFI", "PSF",
    "ADX", "CET", "CII", "STK", "GOF", "AGD", "AOD", "AWP", "IGD",
    "JCE", "JQC", "NIE", "DIAX", "QQQX", "SPXX", "BXMX", "HTD", "HTY",
    "TY", "SOR", "USA", "ASA", "GRX", "THQ", "THW", "RVT", "RMT",
})

# Leveraged / inverse ETFs — out of scope for every strategy here.
_LEVERAGED_HINTS = ("3X", "2X", "ULTRA", "INVERSE", "BEAR", "BULL")
_LEVERAGED_TICKERS = frozenset({
    "TQQQ", "SQQQ", "UPRO", "SPXU", "SPXL", "SPXS", "UDOW", "SDOW",
    "TNA", "TZA", "SOXL", "SOXS", "LABU", "LABD", "FAS", "FAZ",
    "YINN", "YANG", "NUGT", "DUST", "JNUG", "JDST", "UVXY", "SVXY",
    "TMF", "TMV", "QLD", "SSO", "SDS", "QID", "PSQ", "SH", "DOG",
})


def is_closed_end_fund(ticker: str) -> bool:
    """True for known closed-end funds. See `_CLOSED_END_FUNDS`."""
    return (ticker or "").strip().upper() in _CLOSED_END_FUNDS


def passes_instrument_exclusions(ticker: str) -> tuple[bool, str]:
    """Is this ticker an operating company we are willing to trade?

    Rejects, in order: the permanent user blacklist (PLTR), leveraged and
    inverse ETFs, and closed-end funds. Returns (ok, reason) so callers
    can report WHY rather than silently dropping a name — the Phase 1B
    postmortem's actual complaint was that GAB/BDJ passed without anyone
    being told.
    """
    t = (ticker or "").strip().upper()
    if not t:
        return False, "ticker vazio"
    try:
        from core.swing_setups import EXCLUDED_TICKERS
    except Exception:
        EXCLUDED_TICKERS = frozenset({"PLTR"})
    if t in EXCLUDED_TICKERS:
        return False, f"{t} está na lista de exclusão permanente"
    if t in _LEVERAGED_TICKERS or any(h in t for h in _LEVERAGED_HINTS):
        return False, f"{t} parece ETF alavancado/inverso — fora de escopo"
    if t in _CLOSED_END_FUNDS:
        return False, f"{t} é closed-end fund, não empresa operacional — fora de escopo"
    return True, ""


def _col(bars: pd.DataFrame, name: str) -> Optional[str]:
    """Resolve a column case-insensitively. None when absent."""
    if bars is None:
        return None
    for c in bars.columns:
        if c.lower() == name:
            return c
    return None


def passes_avg_volume(
    bars: pd.DataFrame,
    min_avg_vol: float = MIN_AVG_VOL,
    *,
    window: int = AVG_VOL_WINDOW,
) -> bool:
    """P1B-2 — 20-day average daily volume >= `min_avg_vol` shares.

    The 200k floor is deliberately looser than Phase 1's 1M because this
    strategy's candidates can be smaller names. That looseness is paid
    for in slippage, which is why P1B-7 requires the LARGER slippage
    assumption on this strategy — never skip it.

    Missing or short volume history returns False: a stop we cannot
    prove is fillable is not a risk plan.
    """
    vc = _col(bars, "volume")
    if vc is None:
        return False
    vol = pd.to_numeric(bars[vc], errors="coerce").dropna()
    if len(vol) < window:
        return False
    avg = float(vol.iloc[-window:].mean())
    if not np.isfinite(avg):
        return False
    return avg >= min_avg_vol


def average_volume(bars: pd.DataFrame, *, window: int = AVG_VOL_WINDOW) -> Optional[float]:
    """The value behind passes_avg_volume, for display. None when unknown."""
    vc = _col(bars, "volume")
    if vc is None:
        return None
    vol = pd.to_numeric(bars[vc], errors="coerce").dropna()
    if len(vol) < window:
        return None
    avg = float(vol.iloc[-window:].mean())
    return avg if np.isfinite(avg) else None


def relative_volume(
    bars: pd.DataFrame,
    *,
    lookback: int = RVOL_LOOKBACK,
) -> Optional[float]:
    """RVOL = today's volume / mean volume over the PRIOR `lookback` days.

    The denominator excludes today by construction — including it would
    dampen exactly the spike the filter is trying to detect. Returns None
    (not 1.0) when it cannot be computed, so callers can distinguish
    "average participation" from "unknown".
    """
    vc = _col(bars, "volume")
    if vc is None:
        return None
    vol = pd.to_numeric(bars[vc], errors="coerce")
    if len(vol) < lookback + 1:
        return None

    today = float(vol.iloc[-1])
    prior = vol.iloc[-(lookback + 1):-1]
    if prior.isna().any() or not np.isfinite(today):
        return None
    denom = float(prior.mean())
    if not np.isfinite(denom) or denom <= 0:
        return None          # divide-by-zero guard: a dead book is not a spike
    return today / denom


def passes_relative_volume(
    bars: pd.DataFrame,
    min_rvol: float = MIN_RVOL,
    lookback: int = RVOL_LOOKBACK,
) -> bool:
    """P1B-3 — the turn-up must arrive on above-average participation.

    A bounce on thin volume is drift; a bounce on 1.5x normal volume is
    someone actually buying. Short history or a zero denominator fails
    closed.

    Strict `>` per the ticket ("Require RVOL > 1.5"), so a day at exactly
    the threshold does not qualify.
    """
    rvol = relative_volume(bars, lookback=lookback)
    if rvol is None:
        return False
    return rvol > min_rvol


class EarningsCalendar:
    """Point-in-time earnings dates, loaded once and queried per bar.

    Backed by `data/validation_cache/earnings_dates.parquet`, built from
    EDGAR 8-K item 2.02 filings — see `scripts/fetch_earnings_dates.py`
    for why that source is legitimate and what its one approximation is.

    Every earlier backtest in this project ran with the earnings gate
    OFF because no calendar existed. That was disclosed each time, and
    for entries that merely hold THROUGH a report it only added trades a
    live run would skip. H2 is different: its registered rules exclude
    the earnings gap ITSELF, because "earnings drift would be a different
    hypothesis" (that is H3). Testing H2 without this gate would test a
    blend of H2 and H3 and the verdict would mean nothing.

    `covers()` is the honesty valve: a ticker absent from the calendar
    cannot be gated, and the caller must decide what to do about that
    rather than silently receive a pass.
    """

    def __init__(self, by_ticker: dict[str, list]):
        self._by = {k: sorted(v) for k, v in by_ticker.items()}

    @classmethod
    def load(cls, path=None) -> "EarningsCalendar":
        from pathlib import Path
        if path is None:
            path = (Path(__file__).resolve().parent.parent /
                    "data" / "validation_cache" / "earnings_dates.parquet")
        path = Path(path)
        if not path.exists():
            log.warning(f"[filters] no earnings calendar at {path}")
            return cls({})
        df = pd.read_parquet(path)
        df["date"] = pd.to_datetime(df["date"])
        return cls({t: list(g["date"]) for t, g in df.groupby("ticker")})

    def covers(self, ticker: str) -> bool:
        return bool(self._by.get((ticker or "").upper()))

    @property
    def n_tickers(self) -> int:
        return len(self._by)

    def in_blackout(
        self,
        ticker: str,
        signal_date,
        *,
        days_before: int = 2,
        days_after: int = 10,
    ) -> bool:
        """Is `signal_date` inside an earnings blackout?

        True when any report lands in
        [signal_date - days_before, signal_date + days_after], in CALENDAR
        days. The backward leg excludes the earnings gap itself (the move
        we must not mistake for spontaneous strength); the forward leg is
        the registered "no earnings within 10 days" hold rule.

        Fails CLOSED for a covered ticker with no data in range — no, it
        fails OPEN, and deliberately: a covered ticker genuinely has no
        report in that window most of the time, so treating "no match" as
        a rejection would reject nearly everything. Coverage is checked
        separately via `covers()`.
        """
        dates = self._by.get((ticker or "").upper())
        if not dates:
            return False
        d = pd.Timestamp(signal_date)
        lo, hi = d - pd.Timedelta(days=days_before), d + pd.Timedelta(days=days_after)
        i = bisect_left(dates, lo)
        return i < len(dates) and dates[i] <= hi


def passes_earnings_gate(
    ticker: str,
    *,
    time_stop_days: int = 15,
    min_days: Optional[int] = None,
    get_earnings_days_fn=None,
) -> tuple[bool, Optional[int], str]:
    """P1B-4 — no earnings inside the holding window.

    Deliberately NOT a reimplementation: it calls the same
    `data.market_client.get_earnings_days` primitive and the same
    trading->calendar conversion Phase 1 uses, and defaults `min_days`
    to Phase 1's own `EARNINGS_MIN_DAYS` constant. Phase 1's entry code
    is left untouched per the Phase 1B guardrail; this is the shared
    rule expressed as a standalone predicate so a second strategy can
    apply it without importing Phase 1's setup pipeline.

    Returns (passes, days_to_earnings, reason).
    An UNKNOWN earnings date passes — the calendar is frequently absent
    for legitimate reasons, and rejecting on absence would silently kill
    most candidates. This mirrors Phase 1 exactly.
    """
    if min_days is None:
        try:
            from core.swing_setups import EARNINGS_MIN_DAYS
            min_days = EARNINGS_MIN_DAYS
        except Exception:
            min_days = 10

    if get_earnings_days_fn is None:
        try:
            from data.market_client import get_earnings_days as get_earnings_days_fn
        except Exception as e:
            log.debug(f"[filters] earnings calendar unavailable: {e}")
            return True, None, "calendário indisponível — gate não aplicado"

    try:
        from core.swing_exits import trading_days_to_calendar
        window = max(int(min_days), trading_days_to_calendar(time_stop_days))
    except Exception:
        window = max(int(min_days), int(round(time_stop_days * 7 / 5)))

    try:
        days = get_earnings_days_fn(ticker, max_days=window + 20)
    except Exception as e:
        log.debug(f"[filters] earnings {ticker}: {e}")
        return True, None, "calendário indisponível — gate não aplicado"

    if days is None:
        return True, None, "sem data de earnings conhecida"
    if 0 <= days <= window:
        return False, days, (
            f"earnings em {days}d — dentro da janela de {window}d "
            f"(regra: não entrar a <{min_days}d nem segurar durante o report)"
        )
    return True, days, f"earnings em {days}d — fora da janela de {window}d"
