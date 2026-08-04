"""strategies/panic_reversion.py — H1 entry strategy (edge-search protocol).

Hypothesis H1: *supply liquidity to forced sellers.*

Structurally the opposite of everything tested before it. Phase 1 and
Phase 1B both bought STRENGTH with a buy-STOP above the market, and both
needed follow-through — information — to pay. This buys WEAKNESS with a
LIMIT order below the market, and needs only reversion to normal:
mechanics, not information.

    Phase 1  (trend_pullback):  buy stop above a pause, close > SMA50
    Phase 1B (dip_in_uptrend):  buy stop above a turn-up, close < SMA50
    H1       (panic_reversion): buy LIMIT below a multi-day flush

Who loses to you (registered before any result existed): when a liquid
quality name drops hard over several sessions, part of the selling is
forced or emotional — margin calls, risk-limit liquidations, retail
panic. A seller who *must* sell pays for immediacy. The buyer standing
there supplying it collects that premium when price snaps back to normal.

The universe is the ORIGINAL 230-name quality list, deliberately. The
"calm quality" that made these names useless for breakout hunting is the
exact property mean reversion needs: companies that reliably survive
their own panics. A cheap stock that keeps falling is not mean-reverting,
it is repricing.

Registered as part of the hypothesis, and therefore NOT tunable here:
  - NO 2R structure requirement. Mean reversion takes many small wins;
    Phase 1's 2R minimum is structurally wrong for this shape and was
    pre-registered out, not dropped after seeing a result.
  - NO break-even ratchet. Same reason.
  - NO market-regime gate. Panic entries happen in soft tape by nature;
    gating on a healthy tape would gate away the premise. Registered OFF
    with no A/B, so there is no version of this run that could be
    selected after the fact.

Exits live in `core/h1_backtest.py` because they are a different ladder
from Phase 3's, and that difference is registered: SMA5 close exit,
2 x ATR hard stop, 7-bar time stop.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

import numpy as np
import pandas as pd

from strategies.filters import (
    average_volume, passes_avg_volume, passes_earnings_gate,
    passes_instrument_exclusions,
)

log = logging.getLogger("strategies.panic_reversion")

STRATEGY_NAME = "panic_reversion"
SETUP_TYPE    = "PANIC_REVERSION"


def _envf(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def _envi(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


# ── H1 registered constants. Final once the hypothesis started. ──────
PANIC_ATR_MULT   = _envf("H1_PANIC_ATR_MULT", 2.5)    # depth of the flush
PANIC_LOOKBACK   = _envi("H1_PANIC_LOOKBACK", 10)     # bars the flush spans
DOWN_DAYS_WINDOW = _envi("H1_DOWN_DAYS_WINDOW", 4)
DOWN_DAYS_MIN    = _envi("H1_DOWN_DAYS_MIN", 3)       # >= 3 of the last 4 down
LIMIT_ATR_MULT   = _envf("H1_LIMIT_ATR_MULT", 0.5)    # the discount we demand
STOP_ATR_MULT    = _envf("H1_STOP_ATR_MULT", 2.0)     # hard stop below FILL
TIME_STOP_BARS   = _envi("H1_TIME_STOP_BARS", 7)
SMA_EXIT_WINDOW  = _envi("H1_SMA_EXIT_WINDOW", 5)     # exit above SMA5
ATR_PERIOD       = _envi("H1_ATR_PERIOD", 14)
TREND_SMA        = _envi("H1_TREND_SMA", 200)

# Universe filters (H1 registration: "Liquidity >= 1M avg volume; price >= $10")
MIN_PRICE        = _envf("H1_MIN_PRICE", 10.0)
MIN_AVG_VOL      = _envf("H1_MIN_AVG_VOL", 1_000_000.0)

# Warmup: SMA200 needs 200 closes; ATR needs 15. 210 gives both room.
MIN_BARS = TREND_SMA + 10


def _c(bars: pd.DataFrame, name: str) -> Optional[str]:
    for c in bars.columns:
        if c.lower() == name:
            return c
    return None


def _sma(series: pd.Series, window: int) -> Optional[float]:
    if len(series) < window:
        return None
    v = float(series.iloc[-window:].mean())
    return v if np.isfinite(v) else None


def _wilder_atr(bars: pd.DataFrame, period: int = ATR_PERIOD) -> Optional[float]:
    """Wilder ATR — reuses Phase 1's implementation so no two strategies in
    this project can disagree about how volatility is measured."""
    try:
        from core.swing_setups import _wilder_atr as _phase1_atr
        return _phase1_atr(bars, period=period)
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────────────
# The signal
# ─────────────────────────────────────────────────────────────────────

def count_down_days(bars: pd.DataFrame, window: int = DOWN_DAYS_WINDOW) -> Optional[int]:
    """How many of the last `window` sessions closed below the prior close.

    Needs `window + 1` closes: the first of the window is judged against
    the bar before it, not against itself. Returns None when the history
    is too short, never 0 — "unknown" and "no down days" must not collapse
    into the same answer.
    """
    cc = _c(bars, "close")
    if cc is None:
        return None
    close = pd.to_numeric(bars[cc], errors="coerce")
    if len(close) < window + 1:
        return None
    tail = close.iloc[-(window + 1):]
    if tail.isna().any():
        return None
    diffs = tail.diff().iloc[1:]
    return int((diffs < 0).sum())


def is_panic_drop(
    bars: pd.DataFrame,
    *,
    atr_mult: float = PANIC_ATR_MULT,
    lookback: int = PANIC_LOOKBACK,
    down_days_min: int = DOWN_DAYS_MIN,
    down_days_window: int = DOWN_DAYS_WINDOW,
    trend_sma: int = TREND_SMA,
) -> bool:
    """H1's core signal, judged on the signal day's close.

    All three must hold:
        close > SMA200                        long-term trend intact
        close <= max(close, last N) - k*ATR   a real flush, not a wobble
        >= 3 of the last 4 closes were down   sustained, not a one-day gap

    The third condition is what separates this from "buy any big red day".
    A single -3% session is usually one piece of news being priced in
    correctly; four sessions of grinding lower is where forced selling
    lives, because margin clerks and risk desks act on cumulative loss,
    not on one bad print.

    The trend filter is doing real work: without `close > SMA200` this
    strategy buys structural decliners, where every bounce is sold and
    "mean reversion" means reverting to a downtrend. That is the known
    way this family of strategies dies.

    The lookback window INCLUDES the signal day. Excluding it would let a
    stock that already bounced hard today still register as panicked.

    Fails closed on missing or short data.
    """
    if bars is None or len(bars) < max(lookback, trend_sma, ATR_PERIOD + 1) + 1:
        return False
    cc = _c(bars, "close")
    if cc is None:
        return False
    close = pd.to_numeric(bars[cc], errors="coerce")
    last = float(close.iloc[-1])
    if not np.isfinite(last):
        return False

    sma = _sma(close, trend_sma)
    if sma is None or last <= sma:
        return False

    atr = _wilder_atr(bars)
    if atr is None or atr <= 0:
        return False

    window = close.iloc[-lookback:]
    if window.isna().any():
        return False
    peak = float(window.max())
    if not np.isfinite(peak):
        return False
    if last > peak - atr_mult * atr:
        return False

    down = count_down_days(bars, window=down_days_window)
    if down is None or down < down_days_min:
        return False
    return True


def build_plan(bars: pd.DataFrame, *, limit_atr_mult: float = LIMIT_ATR_MULT) -> Optional[dict]:
    """Turn the signal into the order to place tomorrow.

        limit = signal close - 0.5 * ATR(14)
        stop  = 2 * ATR(14) below the FILL   (see note)

    The limit IS the edge: we are paid for immediacy, so we only take the
    trade at a discount to where the panic closed. There is no chase and
    no fallback to market — an unfilled plan is not a trade, and must not
    enter the statistics as a scratch.

    `stop` here is the NOMINAL stop, anchored to the limit. The simulator
    re-anchors it to the actual fill, because a gap-down open fills better
    than the limit and the registered rule says "2 x ATR below fill". That
    keeps 1R identical (= 2 x ATR) on every trade regardless of fill, which
    is what makes the R-multiples comparable across trades.

    No target. Registered out — see the module docstring.
    """
    if bars is None or len(bars) < ATR_PERIOD + 1:
        return None
    cc = _c(bars, "close")
    if cc is None:
        return None
    close = pd.to_numeric(bars[cc], errors="coerce")
    last = float(close.iloc[-1])
    if not np.isfinite(last) or last <= 0:
        return None

    atr = _wilder_atr(bars)
    if atr is None or atr <= 0:
        return None

    limit = last - limit_atr_mult * atr
    if limit <= 0:
        return None
    stop = limit - STOP_ATR_MULT * atr
    if stop <= 0:
        # A stop below zero is not a risk plan. Only reachable on a
        # sub-$1 name with enormous ATR, which the price floor already
        # excludes — kept because "cannot happen" is how bugs enter.
        return None

    return {
        "signal_close":   round(last, 4),
        "entry":          round(limit, 4),
        "entry_limit":    round(limit, 4),
        "stop":           round(stop, 4),
        "risk_per_share": round(limit - stop, 4),
        "stop_pct":       (limit - stop) / limit,
        "atr14":          atr,
    }


# ─────────────────────────────────────────────────────────────────────
# The master gate
# ─────────────────────────────────────────────────────────────────────

def _reject(ticker: str, reason: str, **ctx):
    from core.swing_setups import SwingSetup
    fields = {
        "last_close": None, "last_high": None, "pause_high": None, "pause_low": None,
        "atr14": None, "atr_pct": None, "rsi14": None, "ma20": None, "ma50": None,
        "ma200": None, "return_5d": None, "return_20d": None, "rs_vs_spy_63d": None,
        "volume_ratio_20d": None, "pause_volume_ratio": None,
        "avg_volume_20d": None, "dollar_volume_20d": None,
    }
    fields.update({k: v for k, v in ctx.items() if k in fields})
    return SwingSetup(
        ticker=ticker, qualifies=False, setup_type="NONE", reject_reason=reason,
        entry=None, entry_limit=None, stop=None, target=None,
        time_stop_days=TIME_STOP_BARS, risk_per_share=None,
        reward_per_share=None, rr_ratio=None, stop_pct=None, **fields,
    )


def evaluate_bars(
    ticker: str,
    hist: pd.DataFrame,
    *,
    spy_hist: Optional[pd.DataFrame] = None,   # accepted, unused: regime is OFF
    regime_ok: Optional[bool] = None,          # accepted, unused
    min_price: float = MIN_PRICE,
    min_avg_vol: float = MIN_AVG_VOL,
    check_earnings: bool = True,
    get_earnings_days_fn=None,
    **_ignored,
):
    """The full H1 gate. Signature matches the other strategies so the
    backtest engine can call any of them behind a flag.

    Returns a `SwingSetup`. `qualifies=False` carries the reason, which
    the runner aggregates into a census — the census is how Phase 1B was
    diagnosed as a frequency problem rather than an edge problem, and it
    costs nothing to keep.

    `spy_hist` and `regime_ok` are accepted and ignored. H1 registers the
    regime gate as OFF; taking the argument keeps the common signature,
    and ignoring it means no caller can quietly switch the gate on.
    """
    from core.swing_setups import SwingSetup

    # ── 1. Instrument exclusions (cheapest) ──────────────────────────
    ok, why = passes_instrument_exclusions(ticker)
    if not ok:
        return _reject(ticker, why)

    if hist is None or len(hist) < MIN_BARS:
        return _reject(ticker, f"histórico insuficiente (< {MIN_BARS} barras)")
    cc, hc, lc = _c(hist, "close"), _c(hist, "high"), _c(hist, "low")
    if not (cc and hc and lc):
        return _reject(ticker, "OHLC incompleto")

    close = pd.to_numeric(hist[cc], errors="coerce")
    last_close = float(close.iloc[-1])
    if not np.isfinite(last_close) or last_close <= 0:
        return _reject(ticker, "preço inválido")

    ctx = {
        "last_close": last_close,
        "last_high":  float(pd.to_numeric(hist[hc], errors="coerce").iloc[-1]),
        "ma20":  _sma(close, 20),
        "ma50":  _sma(close, 50),
        "ma200": _sma(close, TREND_SMA),
        "avg_volume_20d": average_volume(hist),
    }

    # ── 2. Price floor ───────────────────────────────────────────────
    if last_close < min_price:
        return _reject(ticker, f"preço ${last_close:.2f} < mínimo ${min_price:.0f}", **ctx)

    # ── 3. Liquidity: >= 1M shares/day ───────────────────────────────
    if not passes_avg_volume(hist, min_avg_vol):
        av = ctx.get("avg_volume_20d")
        detail = f"{av/1e6:.2f}M" if av is not None else "indisponível"
        return _reject(
            ticker,
            f"volume médio {detail} < {min_avg_vol/1e6:.0f}M ações/dia",
            **ctx,
        )

    # ── 4. Long-term trend intact ────────────────────────────────────
    ma200 = ctx["ma200"]
    if ma200 is None:
        return _reject(ticker, "SMA200 indisponível", **ctx)
    if last_close <= ma200:
        return _reject(
            ticker,
            f"close {last_close:.2f} <= SMA200 {ma200:.2f} — "
            f"queda estrutural, não pânico",
            **ctx,
        )

    # ── 5. The panic itself ──────────────────────────────────────────
    atr = _wilder_atr(hist)
    ctx["atr14"] = atr
    if atr is None or atr <= 0:
        return _reject(ticker, "ATR indisponível", **ctx)

    if not is_panic_drop(hist):
        window = close.iloc[-PANIC_LOOKBACK:]
        peak = float(window.max()) if not window.isna().any() else float("nan")
        drop_atr = (peak - last_close) / atr if np.isfinite(peak) else float("nan")
        down = count_down_days(hist)
        if np.isfinite(drop_atr) and drop_atr < PANIC_ATR_MULT:
            why = (f"queda {drop_atr:.2f} ATR em {PANIC_LOOKBACK}d "
                   f"< {PANIC_ATR_MULT:.1f} ATR — sem pânico")
        else:
            why = (f"apenas {down if down is not None else '?'} de "
                   f"{DOWN_DAYS_WINDOW} dias em queda "
                   f"(mínimo {DOWN_DAYS_MIN}) — queda de um dia só")
        return _reject(ticker, why, **ctx)

    # ── 6. Earnings ──────────────────────────────────────────────────
    days_to_earnings = None
    if check_earnings:
        ok, days_to_earnings, why = passes_earnings_gate(
            ticker, time_stop_days=TIME_STOP_BARS,
            get_earnings_days_fn=get_earnings_days_fn,
        )
        if not ok:
            s = _reject(ticker, why, **ctx)
            s.days_to_earnings = days_to_earnings
            s.earnings_in_window = True
            return s

    # ── 7. The plan ──────────────────────────────────────────────────
    plan = build_plan(hist)
    if plan is None:
        return _reject(ticker, "plano inválido (ATR/preço)", **ctx)

    setup = SwingSetup(
        ticker=ticker, qualifies=True, setup_type=SETUP_TYPE, reject_reason="",
        entry=plan["entry"], entry_limit=plan["entry_limit"],
        stop=plan["stop"],
        target=None,                     # registered: no target for H1
        time_stop_days=TIME_STOP_BARS,
        risk_per_share=plan["risk_per_share"],
        reward_per_share=None, rr_ratio=None, stop_pct=plan["stop_pct"],
        atr_pct=(atr / last_close) if last_close else None,
        rsi14=None, return_5d=None, return_20d=None, rs_vs_spy_63d=None,
        pause_high=None, pause_low=None, pause_volume_ratio=None,
        volume_ratio_20d=None,
        dollar_volume_20d=((ctx.get("avg_volume_20d") or 0.0) * last_close) or None,
        days_to_earnings=days_to_earnings,
        **{k: ctx.get(k) for k in
           ("last_close", "last_high", "ma20", "ma50", "ma200",
            "atr14", "avg_volume_20d")},
    )
    peak = float(close.iloc[-PANIC_LOOKBACK:].max())
    setup.plan_lines = [
        f"pânico: close {last_close:.2f} está "
        f"{(peak - last_close) / atr:.2f} ATR abaixo do topo de "
        f"{PANIC_LOOKBACK}d ({peak:.2f}), "
        f"{count_down_days(hist)}/{DOWN_DAYS_WINDOW} dias em queda · "
        f"limite de compra {plan['entry']:.2f} (−{LIMIT_ATR_MULT} ATR)"
    ]
    return setup
