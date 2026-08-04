"""strategies/rsi2.py — C-0b/C-series: RSI(2) and the C1-C3 variants.

Connors & Alvarez, *Short Term Trading Strategies That Work*. Three
pre-registered variants of one idea — buy short-term oversold inside a
long-term uptrend — implemented exactly as `connors_rsi2_spec.md`
registers them. Nothing here is tunable: X and Y are fixed at the
registered values and the spec forbids sweeps.

WILDER'S SMOOTHING, not the simple average. The two diverge, the book
uses Wilder, and at period=2 the divergence is large: the simple variant
re-weights only the last two bars while Wilder carries an exponentially
decaying memory of every prior bar. Using the wrong one would make L0
disagree with the book's published win rates and send the debugging in
entirely the wrong direction.

NO LOOK-AHEAD BY CONSTRUCTION. Every series function computes index i
from `closes[:i+1]` alone — Wilder's recursion is causal, and the SMAs
are trailing windows. `test_rsi2.py` asserts this the hard way, by
recomputing each value from a truncated prefix and demanding equality.

The C-series does NOT produce a `SwingSetup`: these variants have no
stop, so there is no R to define and results are reported in % per trade.
That is the book's convention and the spec's, and it is why the C-series
has its own backtest (`core/c_series_backtest.py`) rather than reusing
the swing machinery.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

import numpy as np

# ── Registered constants. Final. The spec forbids sweeps. ────────────
RSI_PERIOD      = 2
TREND_SMA       = 200      # SPY close > SMA200 gates every variant
C1_RSI_ENTRY    = 5.0      # "Holy Grail": RSI(2) below 5
C1_EXIT_SMA     = 5        # exit when close > SMA5
CUM_RSI_DAYS    = 2        # X = 2 for C2 and C3
C2_CUM_ENTRY    = 35.0     # Y = 35, tight
C3_CUM_ENTRY    = 50.0     # Y = 50, loose
CUM_RSI_EXIT    = 65.0     # C2/C3 exit when RSI(2) closes above 65
TIME_VALVE_BARS = 25       # registered safety valve


# ─────────────────────────────────────────────────────────────────────
# C-0b — RSI(2), Wilder's method
# ─────────────────────────────────────────────────────────────────────

def rsi_series(closes: Sequence[float], period: int = RSI_PERIOD) -> np.ndarray:
    """Wilder RSI for every bar. NaN where undefined.

    Wilder's method:
        seed:  avg_gain = mean(first `period` gains)
               avg_loss = mean(first `period` losses)
        step:  avg = (prev_avg * (period - 1) + current) / period

    The first defined value lands at index `period` (it needs `period`
    price changes, hence `period + 1` closes).

    Edge cases, defined here rather than left to float luck:
        avg_loss == 0 and avg_gain  > 0  -> 100  (pure gains)
        avg_gain == 0 and avg_loss  > 0  ->   0  (pure losses)
        both zero (a flat series)        ->  50  (no information either way)
    """
    c = np.asarray(closes, dtype=float)
    n = len(c)
    out = np.full(n, np.nan)
    if n < period + 1 or period < 1:
        return out

    diff = np.diff(c)
    gains = np.where(diff > 0, diff, 0.0)
    losses = np.where(diff < 0, -diff, 0.0)

    avg_gain = float(gains[:period].mean())
    avg_loss = float(losses[:period].mean())
    out[period] = _rsi_from(avg_gain, avg_loss)

    for k in range(period, len(diff)):
        avg_gain = (avg_gain * (period - 1) + gains[k]) / period
        avg_loss = (avg_loss * (period - 1) + losses[k]) / period
        out[k + 1] = _rsi_from(avg_gain, avg_loss)
    return out


def _rsi_from(avg_gain: float, avg_loss: float) -> float:
    if avg_loss <= 0.0:
        return 100.0 if avg_gain > 0.0 else 50.0
    if avg_gain <= 0.0:
        return 0.0
    return 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)


def rsi(closes: Sequence[float], period: int = RSI_PERIOD) -> Optional[float]:
    """RSI(period) for the LAST close. None when history is insufficient.

    Fails closed: `None`, never a neutral 50, so a caller cannot mistake
    "not enough data" for "not oversold".
    """
    c = np.asarray(closes, dtype=float)
    if len(c) < period + 1:
        return None
    v = rsi_series(c, period)[-1]
    return None if not np.isfinite(v) else float(v)


def cumulative_rsi_series(
    closes: Sequence[float],
    period: int = RSI_PERIOD,
    x_days: int = CUM_RSI_DAYS,
) -> np.ndarray:
    """Rolling sum of the last `x_days` RSI values. NaN until defined."""
    r = rsi_series(closes, period)
    n = len(r)
    out = np.full(n, np.nan)
    if x_days < 1:
        return out
    for i in range(x_days - 1, n):
        w = r[i - x_days + 1: i + 1]
        if np.isfinite(w).all():
            out[i] = float(w.sum())
    return out


def cumulative_rsi(
    closes: Sequence[float],
    period: int = RSI_PERIOD,
    x_days: int = CUM_RSI_DAYS,
) -> Optional[float]:
    """Sum of the last `x_days` RSI(period) values. None when undefined."""
    v = cumulative_rsi_series(closes, period, x_days)
    if len(v) == 0 or not np.isfinite(v[-1]):
        return None
    return float(v[-1])


def sma_series(closes: Sequence[float], window: int) -> np.ndarray:
    """Trailing simple moving average. NaN for the first `window - 1` bars."""
    c = np.asarray(closes, dtype=float)
    n = len(c)
    out = np.full(n, np.nan)
    if window < 1 or n < window:
        return out
    csum = np.cumsum(np.insert(c, 0, 0.0))
    out[window - 1:] = (csum[window:] - csum[:-window]) / window
    return out


# ─────────────────────────────────────────────────────────────────────
# Precomputed indicators — one pass per price series
# ─────────────────────────────────────────────────────────────────────

@dataclass
class Indicators:
    """Every series a C-variant needs, computed once.

    The H2 postmortem is the reason this exists: recomputing an indicator
    inside the per-bar loop turned a 20-minute backtest into a 5-hour one.
    Precomputing is safe here precisely because every series is causal —
    `rsi2[i]` is the value a trader had at bar i's close, no more.
    """
    closes:  np.ndarray
    rsi2:    np.ndarray
    cum_rsi: np.ndarray
    sma5:    np.ndarray
    sma200:  np.ndarray

    @classmethod
    def build(cls, closes: Sequence[float]) -> "Indicators":
        c = np.asarray(closes, dtype=float)
        return cls(
            closes=c,
            rsi2=rsi_series(c, RSI_PERIOD),
            cum_rsi=cumulative_rsi_series(c, RSI_PERIOD, CUM_RSI_DAYS),
            sma5=sma_series(c, C1_EXIT_SMA),
            sma200=sma_series(c, TREND_SMA),
        )

    def ready(self, i: int) -> bool:
        """Is every series defined at bar i? Warm-up guard, fails closed."""
        return bool(
            0 <= i < len(self.closes)
            and np.isfinite(self.rsi2[i]) and np.isfinite(self.cum_rsi[i])
            and np.isfinite(self.sma5[i]) and np.isfinite(self.sma200[i])
        )


def uptrend(ind: Indicators, i: int) -> bool:
    """The shared trend filter: SPY close > SMA200. No new entries otherwise."""
    if not (0 <= i < len(ind.closes)) or not np.isfinite(ind.sma200[i]):
        return False
    return bool(ind.closes[i] > ind.sma200[i])


# ─────────────────────────────────────────────────────────────────────
# The three registered variants
# ─────────────────────────────────────────────────────────────────────

def c1_entry(ind: Indicators, i: int) -> bool:
    """C1 "Holy Grail" (book p.55): uptrend AND RSI(2) < 5."""
    if not ind.ready(i) or not uptrend(ind, i):
        return False
    return bool(ind.rsi2[i] < C1_RSI_ENTRY)


def c1_exit(ind: Indicators, i: int) -> bool:
    """C1 exit: close above SMA5."""
    if not (0 <= i < len(ind.closes)) or not np.isfinite(ind.sma5[i]):
        return False
    return bool(ind.closes[i] > ind.sma5[i])


def c2_entry(ind: Indicators, i: int) -> bool:
    """C2: uptrend AND cumulative RSI(2) over 2 days < 35."""
    if not ind.ready(i) or not uptrend(ind, i):
        return False
    return bool(ind.cum_rsi[i] < C2_CUM_ENTRY)


def c3_entry(ind: Indicators, i: int) -> bool:
    """C3: same as C2 with the looser Y = 50."""
    if not ind.ready(i) or not uptrend(ind, i):
        return False
    return bool(ind.cum_rsi[i] < C3_CUM_ENTRY)


def cum_exit(ind: Indicators, i: int) -> bool:
    """C2/C3 exit: RSI(2) closes above 65."""
    if not (0 <= i < len(ind.closes)) or not np.isfinite(ind.rsi2[i]):
        return False
    return bool(ind.rsi2[i] > CUM_RSI_EXIT)


@dataclass(frozen=True)
class Variant:
    name: str
    description: str
    entry_fn: object
    exit_fn: object
    book_win_rate: Optional[float]   # the published figure, for the L0 check


VARIANTS = {
    "C1": Variant(
        "C1", "Holy Grail — RSI(2) < 5, exit above SMA5",
        c1_entry, c1_exit, 83.6,
    ),
    "C2": Variant(
        "C2", "Cumulative RSI tight — X=2, Y=35, exit RSI(2) > 65",
        c2_entry, cum_exit, 88.0,
    ),
    "C3": Variant(
        "C3", "Cumulative RSI loose — X=2, Y=50, exit RSI(2) > 65",
        c3_entry, cum_exit, 85.5,
    ),
}


def get_variant(name: str) -> Variant:
    key = (name or "").strip().upper()
    if key not in VARIANTS:
        raise ValueError(f"unknown variant {name!r} — choose from {', '.join(VARIANTS)}")
    return VARIANTS[key]
