"""strategies/gap_continuation.py — H2 entry strategy (edge-search protocol).

Hypothesis H2: *the trades our limit cap refused.*

This is the only hypothesis in the project generated from the project's
own data. In Phase 1, the 35 trades that gapped beyond the stop-limit —
the ones the system declined to chase — won 68.6% at +0.43R average,
while the 731 it accepted netted zero. The registered interpretation: a
gap violent enough to blow through a chase-protection cap reflects real
news or real flow, and institutions cannot complete a position in one
session, so the move continues for days.

That observation is EVIDENCE FOR TESTING, NOT EVIDENCE. It was noticed
after the fact on 35 trades, which is a sample small enough to be pure
noise and selected on the outcome we then admired. The protocol is
explicit that the sample is spent and proves nothing by itself. This
module re-tests the idea on a fresh, pre-registered configuration.

    Phase 1  (trend_pullback):  buy a pause, cap the price paid
    H1       (panic_reversion): buy a flush, demand a discount
    H2       (gap_continuation): buy extraordinary strength AT MARKET,
                                 accept the bad fill as the signal's cost

Registered rules, final once the hypothesis started:
  - Signal (day i): open gaps >= 1.0 x ATR(14) above the prior close,
    the open clears the highest high of the prior 20 sessions, and the
    day closes with RVOL(50) >= 2.0.
  - Entry: next day's OPEN, at market. Deliberately one day late — it
    tests CONTINUATION rather than the gap itself, uses only completed
    bars, and removes any intraday look-ahead.
  - Context: prior close > SMA50 > SMA200. Regime gate ON (SPY > SMA200).
  - Earnings: none within 10 days AND none in the prior 2 days. The
    second half is what keeps this from silently becoming H3 — an
    earnings gap is a different hypothesis with a different counterparty.

Exits live in `core/h2_backtest.py`: stop at entry - 1.5 x ATR, NO fixed
target, trail out at the first close below SMA10, 15-bar time stop,
break-even ratchet at +1R. Registered before results because strength
strategies die by capping their winners, and a target is exactly that cap.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

import numpy as np
import pandas as pd

from strategies.filters import (
    average_volume, passes_avg_volume, passes_instrument_exclusions,
    relative_volume,
)

log = logging.getLogger("strategies.gap_continuation")

STRATEGY_NAME = "gap_continuation"
SETUP_TYPE    = "GAP_CONTINUATION"


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


# ── H2 registered constants ──────────────────────────────────────────
GAP_ATR_MULT     = _envf("H2_GAP_ATR_MULT", 1.0)     # gap size, in ATRs
HIGH_LOOKBACK    = _envi("H2_HIGH_LOOKBACK", 20)     # bars the open must clear
MIN_RVOL         = _envf("H2_MIN_RVOL", 2.0)         # >= 2.0, at the close
RVOL_LOOKBACK    = _envi("H2_RVOL_LOOKBACK", 50)
STOP_ATR_MULT    = _envf("H2_STOP_ATR_MULT", 1.5)
TRAIL_SMA        = _envi("H2_TRAIL_SMA", 10)         # exit below SMA10
TIME_STOP_BARS   = _envi("H2_TIME_STOP_BARS", 15)
BREAKEVEN_AT_R   = _envf("H2_BREAKEVEN_AT_R", 1.0)
ATR_PERIOD       = _envi("H2_ATR_PERIOD", 14)
EARNINGS_BEFORE  = _envi("H2_EARNINGS_BEFORE", 2)    # calendar days
EARNINGS_AFTER   = _envi("H2_EARNINGS_AFTER", 10)

# Universe hygiene. The 407-name list is already screened for $2B+ cap and
# profitability; these are the per-bar floors the backtest still needs.
MIN_PRICE        = _envf("H2_MIN_PRICE", 10.0)
MIN_AVG_VOL      = _envf("H2_MIN_AVG_VOL", 200_000.0)

MIN_BARS = 210      # SMA200 + ATR headroom


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
    """Wilder ATR — Phase 1's implementation, so volatility is measured
    identically across every strategy in the project."""
    try:
        from core.swing_setups import _wilder_atr as _phase1_atr
        return _phase1_atr(bars, period=period)
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────────────
# The signal
# ─────────────────────────────────────────────────────────────────────

def gap_atr(bars: pd.DataFrame) -> Optional[float]:
    """Today's opening gap, expressed in ATRs.

    The ATR used is the one computed through YESTERDAY — the last
    completed bar before the open being measured. Using an ATR that
    includes today's own enormous range would make a bigger gap register
    as a smaller number of ATRs, which inverts the thing the filter is
    trying to detect.
    """
    if bars is None or len(bars) < ATR_PERIOD + 2:
        return None
    oc, cc = _c(bars, "open"), _c(bars, "close")
    if not (oc and cc):
        return None
    op = float(pd.to_numeric(bars[oc], errors="coerce").iloc[-1])
    prev_close = float(pd.to_numeric(bars[cc], errors="coerce").iloc[-2])
    if not (np.isfinite(op) and np.isfinite(prev_close)):
        return None
    atr_prior = _wilder_atr(bars.iloc[:-1])
    if atr_prior is None or atr_prior <= 0:
        return None
    return (op - prev_close) / atr_prior


def clears_prior_high(bars: pd.DataFrame, *, lookback: int = HIGH_LOOKBACK) -> Optional[bool]:
    """Did today's OPEN clear the highest high of the prior `lookback` bars?

    The window excludes today by construction. Including it would compare
    the open against a high that the open itself helped set, and since a
    bar's high is always >= its open the test could essentially never pass.
    """
    if bars is None or len(bars) < lookback + 1:
        return None
    oc, hc = _c(bars, "open"), _c(bars, "high")
    if not (oc and hc):
        return None
    op = float(pd.to_numeric(bars[oc], errors="coerce").iloc[-1])
    prior = pd.to_numeric(bars[hc], errors="coerce").iloc[-(lookback + 1):-1]
    if prior.isna().any() or not np.isfinite(op):
        return None
    return bool(op > float(prior.max()))


def is_strong_gap(
    bars: pd.DataFrame,
    *,
    gap_mult: float = GAP_ATR_MULT,
    lookback: int = HIGH_LOOKBACK,
    min_rvol: float = MIN_RVOL,
) -> bool:
    """H2's core signal, judged on the signal day's completed bar.

    All three must hold:
        gap >= 1.0 x ATR(14)        the move is large relative to normal
        open > 20-day high          it is a genuine breakout, not a bounce
                                    inside a range
        RVOL(50) >= 2.0             real participation, not a thin print

    The RVOL leg is measured at the CLOSE, which is why the entry is the
    NEXT day's open: by the time we know the volume, the session is over.
    That one-day delay is the registered design, not a limitation —
    it is what makes the test a test of continuation.

    Fails closed on missing or short data.
    """
    g = gap_atr(bars)
    if g is None or g < gap_mult:
        return False
    if not clears_prior_high(bars, lookback=lookback):
        return False
    rv = relative_volume(bars, lookback=RVOL_LOOKBACK)
    if rv is None or rv < min_rvol:
        return False
    return True


def trend_context_ok(bars: pd.DataFrame) -> bool:
    """Registered context: YESTERDAY's close > SMA50 > SMA200.

    Everything is evaluated on `bars[:-1]` — the day before the gap. The
    gap day itself is deliberately excluded from the trend test so that
    one enormous session cannot drag the stock's own moving averages into
    qualifying it.
    """
    if bars is None or len(bars) < 202:
        return False
    prior = bars.iloc[:-1]
    cc = _c(prior, "close")
    if cc is None:
        return False
    close = pd.to_numeric(prior[cc], errors="coerce")
    last = float(close.iloc[-1])
    s50, s200 = _sma(close, 50), _sma(close, 200)
    if s50 is None or s200 is None or not np.isfinite(last):
        return False
    return last > s50 > s200


# ─────────────────────────────────────────────────────────────────────
# The plan
# ─────────────────────────────────────────────────────────────────────

def build_plan(bars: pd.DataFrame) -> Optional[dict]:
    """The order to place tomorrow: buy at the open, at market.

    `entry` is recorded as the signal day's close purely as a reference
    price for display — the simulator ignores it and fills at the next
    bar's actual open, because that is what "at market" means. There is
    no entry_limit: H2's whole premise is that refusing to chase was the
    mistake, so a cap would test the opposite hypothesis.

    `stop` is nominal, anchored to that reference; the simulator
    re-anchors it to the real fill (entry - 1.5 x ATR), which keeps
    1R == 1.5 x ATR on every trade and the R-multiples comparable.
    """
    if bars is None or len(bars) < ATR_PERIOD + 1:
        return None
    cc = _c(bars, "close")
    if cc is None:
        return None
    ref = float(pd.to_numeric(bars[cc], errors="coerce").iloc[-1])
    if not np.isfinite(ref) or ref <= 0:
        return None

    atr = _wilder_atr(bars)
    if atr is None or atr <= 0:
        return None

    stop = ref - STOP_ATR_MULT * atr
    if stop <= 0:
        return None

    return {
        "entry":          round(ref, 4),      # reference only — fill is next open
        "stop":           round(stop, 4),
        "risk_per_share": round(ref - stop, 4),
        "stop_pct":       (ref - stop) / ref,
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
    spy_hist: Optional[pd.DataFrame] = None,
    regime_ok: Optional[bool] = None,
    use_regime_gate: bool = True,          # registered ON, no A/B
    earnings_calendar=None,
    require_earnings_coverage: bool = True,
    min_price: float = MIN_PRICE,
    min_avg_vol: float = MIN_AVG_VOL,
    **_ignored,
):
    """The full H2 gate. Common signature, so the engine can call any
    strategy behind a flag.

    Gate order is cheapest-first and short-circuits, so the census counts
    the FIRST reason a bar failed, which is what makes it diagnostic.

    `require_earnings_coverage=True` rejects a ticker the calendar does
    not cover, rather than waving it through. Ungated names would enter
    the sample carrying exactly the earnings gaps H2 is registered to
    exclude, and they would do so non-randomly — by ticker. Better to
    trade a smaller, honest universe.
    """
    from core.swing_setups import SwingSetup
    from strategies.dip_in_uptrend import market_regime_ok_spy200

    # ── 1. Regime (registered ON) ────────────────────────────────────
    if use_regime_gate:
        if regime_ok is None:
            regime_ok, regime_msg = market_regime_ok_spy200(spy_hist)
        else:
            regime_msg = "regime pré-calculado pelo scan"
        if not regime_ok:
            return _reject(ticker, f"regime de mercado: {regime_msg}")

    # ── 2. Instrument exclusions ─────────────────────────────────────
    ok, why = passes_instrument_exclusions(ticker)
    if not ok:
        return _reject(ticker, why)

    if hist is None or len(hist) < MIN_BARS:
        return _reject(ticker, f"histórico insuficiente (< {MIN_BARS} barras)")
    cc, hc, oc = _c(hist, "close"), _c(hist, "high"), _c(hist, "open")
    if not (cc and hc and oc):
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
        "ma200": _sma(close, 200),
        "avg_volume_20d": average_volume(hist),
    }

    # ── 3. Price / liquidity floors ──────────────────────────────────
    if last_close < min_price:
        return _reject(ticker, f"preço ${last_close:.2f} < mínimo ${min_price:.0f}", **ctx)
    if not passes_avg_volume(hist, min_avg_vol):
        av = ctx.get("avg_volume_20d")
        detail = f"{av/1e3:.0f}k" if av is not None else "indisponível"
        return _reject(ticker, f"volume médio {detail} < {min_avg_vol/1e3:.0f}k ações/dia", **ctx)

    # ── 4. Trend context (measured on YESTERDAY) ─────────────────────
    if not trend_context_ok(hist):
        return _reject(
            ticker,
            "contexto: close anterior não está acima de SMA50 > SMA200",
            **ctx,
        )

    # ── 5. The gap itself ────────────────────────────────────────────
    g = gap_atr(hist)
    if g is None:
        return _reject(ticker, "gap indisponível (ATR/dados)", **ctx)
    if g < GAP_ATR_MULT:
        return _reject(ticker, f"gap {g:.2f} ATR < {GAP_ATR_MULT:.1f} ATR", **ctx)

    if not clears_prior_high(hist, lookback=HIGH_LOOKBACK):
        return _reject(
            ticker,
            f"abertura não superou a máxima de {HIGH_LOOKBACK}d — "
            f"gap dentro do range",
            **ctx,
        )

    rv = relative_volume(hist, lookback=RVOL_LOOKBACK)
    ctx["volume_ratio_20d"] = rv
    if rv is None or rv < MIN_RVOL:
        detail = f"{rv:.2f}×" if rv is not None else "indisponível"
        return _reject(ticker, f"RVOL {detail} < {MIN_RVOL:.1f} — gap sem volume", **ctx)

    # ── 6. Earnings (the leg that keeps H2 from becoming H3) ─────────
    if earnings_calendar is not None:
        if not earnings_calendar.covers(ticker):
            if require_earnings_coverage:
                return _reject(ticker, "sem cobertura de earnings — não gatável", **ctx)
        elif earnings_calendar.in_blackout(
            ticker, hist.index[-1],
            days_before=EARNINGS_BEFORE, days_after=EARNINGS_AFTER,
        ):
            s = _reject(
                ticker,
                f"earnings dentro de [-{EARNINGS_BEFORE}d, +{EARNINGS_AFTER}d] — "
                f"gap de resultados é outra hipótese (H3)",
                **ctx,
            )
            s.earnings_in_window = True
            return s

    # ── 7. The plan ──────────────────────────────────────────────────
    plan = build_plan(hist)
    if plan is None:
        return _reject(ticker, "plano inválido (ATR/preço)", **ctx)

    atr = plan["atr14"]
    setup = SwingSetup(
        ticker=ticker, qualifies=True, setup_type=SETUP_TYPE, reject_reason="",
        entry=plan["entry"],
        entry_limit=None,                # registered: no cap, buy at market
        stop=plan["stop"],
        target=None,                     # registered: no fixed target
        time_stop_days=TIME_STOP_BARS,
        risk_per_share=plan["risk_per_share"],
        reward_per_share=None, rr_ratio=None, stop_pct=plan["stop_pct"],
        atr14=atr, atr_pct=(atr / last_close) if last_close else None,
        rsi14=None, return_5d=None, return_20d=None, rs_vs_spy_63d=None,
        pause_high=None, pause_low=None, pause_volume_ratio=None,
        dollar_volume_20d=((ctx.get("avg_volume_20d") or 0.0) * last_close) or None,
        **{k: ctx.get(k) for k in
           ("last_close", "last_high", "ma20", "ma50", "ma200",
            "avg_volume_20d", "volume_ratio_20d")},
    )
    # Carried for the trade record so the backtest can report the gap-size
    # distribution without recomputing it. Not a SwingSetup field — that
    # dataclass is shared with three other strategies and does not need an
    # H2-specific column.
    setup._gap_atr = g
    setup.plan_lines = [
        f"gap: abertura {g:.2f} ATR acima do fecho anterior, "
        f"acima da máxima de {HIGH_LOOKBACK}d"
        + (f" · RVOL {rv:.2f}×" if rv is not None else "")
        + " · compra à abertura seguinte, a mercado"
    ]
    return setup
