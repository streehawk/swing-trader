"""strategies/dip_in_uptrend.py — Phase 1B entry strategy.

A *competing hypothesis* to Phase 1, not a modification of it. Adapted
from Pezim, "How To Swing Trade" (p. 219): find a stock that has popped
back above its 20-day SMA while still below its 50-day, but still above
its 200-day — a name in a long-term uptrend that took a real correction
and is now turning back up.

    Phase 1  (trend_pullback):  close > SMA50 > SMA200   — buy leaders
    Phase 1B (dip_in_uptrend):  SMA200 < close < SMA50    — buy correctors
                                and close >= SMA20 * 1.05

A symbol cannot satisfy both at once (one needs close above SMA50, the
other below it). They are kept as separate strategies behind a flag for
exactly that reason: merged, the gates cancel and nothing is ever
emitted.

Deliberately NOT tested here: Pezim's deeper variant (below BOTH the 50
and the 200). Per the spec that version is out of scope.

Gate order (P1B-6) is cheapest-first and short-circuits:
    1. exclusions      price >= $10, not PLTR / leveraged / inverse
    2. avg volume      >= 200k shares (P1B-2)
    3. relative volume RVOL > 1.5      (P1B-3)
    4. dip-in-uptrend  the core signal  (P1B-1)
    5. earnings        none within the holding window (P1B-4)
    6. structure       supports >= 2R   (P1B-5)

Market regime: Phase 1 gates on SPY above its 50- AND 200-day SMA. For
this strategy that gate is philosophically ambiguous — dips often turn up
while the broad tape is still soft — so the BASE case runs without it and
the spec measures its effect as a separate run (B2) rather than assuming.

Sizing (Phase 2) and exits (Phase 3) are untouched: this module produces
an entry plan and nothing else, so any measured difference against
Phase 1 is attributable to the entry signal alone.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

import numpy as np
import pandas as pd

from strategies.filters import (
    MIN_AVG_VOL, MIN_RVOL, RVOL_LOOKBACK,
    _LEVERAGED_HINTS, _LEVERAGED_TICKERS,
    average_volume, passes_avg_volume, passes_earnings_gate,
    passes_instrument_exclusions, passes_relative_volume, relative_volume,
)

log = logging.getLogger("strategies.dip_in_uptrend")

STRATEGY_NAME = "dip_in_uptrend"
SETUP_TYPE    = "DIP_IN_UPTREND"


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


# ── P1B-1: the core signal ───────────────────────────────────────────
SMA20_POP_PCT   = _envf("P1B_SMA20_POP_PCT", 0.05)   # >= 5% above SMA20

# ── P1B-2 exclusions (shared with Phase 1) ───────────────────────────
MIN_PRICE = _envf("P1B_MIN_PRICE", 10.0)
EXCLUDED_TICKERS = frozenset(
    t.strip().upper()
    for t in os.environ.get("SWING_EXCLUDED_TICKERS", "PLTR").split(",")
    if t.strip()
)
# Leveraged / inverse ETF lists now live in strategies.filters alongside
# the closed-end fund blacklist; re-exported here so existing importers
# and tests keep working.

# ── P1B-5: structure ─────────────────────────────────────────────────
SWING_LOOKBACK      = _envi("P1B_SWING_LOOKBACK", 20)  # bars to locate the dip low
ATR_PERIOD          = _envi("P1B_ATR_PERIOD", 14)
ENTRY_BUFFER_PCT    = _envf("P1B_ENTRY_BUFFER_PCT", 0.001)
ENTRY_LIMIT_PCT     = _envf("P1B_ENTRY_LIMIT_PCT", 0.002)
STOP_BUFFER_PCT     = _envf("P1B_STOP_BUFFER_PCT", 0.001)
STOP_ATR_FLOOR_MULT = _envf("P1B_STOP_ATR_FLOOR", 1.0)
REWARD_MULTIPLE     = _envf("P1B_REWARD_MULTIPLE", 2.0)
MAX_TARGET_ATR      = _envf("P1B_MAX_TARGET_ATR", 4.0)
TIME_STOP_DAYS      = _envi("SWING_TIME_STOP_DAYS", 15)   # Phase 3 — held constant


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
    """Wilder ATR. Reuses Phase 1's implementation so the two strategies
    cannot drift apart on volatility measurement."""
    try:
        from core.swing_setups import _wilder_atr as _phase1_atr
        return _phase1_atr(bars, period=period)
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────────────
# P1B-1 — the core signal
# ─────────────────────────────────────────────────────────────────────

def is_dip_in_uptrend(
    bars: pd.DataFrame,
    *,
    pop_pct: float = SMA20_POP_PCT,
) -> bool:
    """P1B-1 — a dip inside a long-term uptrend that is turning back up.

    All three must hold on the signal day's close:
        close >= SMA20 * (1 + pop_pct)   the pop that signals the turn
        close <  SMA50                   it really did dip out of the
                                         short-term trend
        close >  SMA200                  the long-term uptrend is intact,
                                         so this is a correction and not
                                         a broken chart

    Needs 200 bars. Anything shorter fails closed rather than falling
    back to a shorter average, because a "200-day trend" computed from
    120 bars is a different claim than the one the rule makes.
    """
    if bars is None or len(bars) < 200:
        return False
    cc = _c(bars, "close")
    if cc is None:
        return False

    close = pd.to_numeric(bars[cc], errors="coerce")
    if close.isna().iloc[-1]:
        return False
    last = float(close.iloc[-1])
    if not np.isfinite(last) or last <= 0:
        return False

    sma20 = _sma(close, 20)
    sma50 = _sma(close, 50)
    sma200 = _sma(close, 200)
    if sma20 is None or sma50 is None or sma200 is None:
        return False

    return (
        last >= sma20 * (1.0 + pop_pct)
        and last < sma50
        and last > sma200
    )


def passes_exclusions(ticker: str, bars: pd.DataFrame) -> tuple[bool, str]:
    """P1B-6 step 1 — the cheapest checks, so they run first.

    The ticker-level checks were moved into
    `strategies.filters.passes_instrument_exclusions` so the closed-end
    fund blacklist (the Phase 1B postmortem's GAB/BDJ finding) is applied
    by every strategy from one list. Only the price floor is local, since
    that threshold differs per strategy.
    """
    ok, why = passes_instrument_exclusions(ticker)
    if not ok:
        return False, why

    cc = _c(bars, "close")
    if cc is None or len(bars) == 0:
        return False, "sem preço"
    last = float(pd.to_numeric(bars[cc], errors="coerce").iloc[-1])
    if not np.isfinite(last) or last < MIN_PRICE:
        return False, f"preço ${last:.2f} < mínimo ${MIN_PRICE:.0f}"
    return True, ""


# ─────────────────────────────────────────────────────────────────────
# P1B-5 — structure
# ─────────────────────────────────────────────────────────────────────

def build_structure(
    bars: pd.DataFrame,
    *,
    swing_lookback: int = SWING_LOOKBACK,
    reward_multiple: float = REWARD_MULTIPLE,
    max_target_atr: float = MAX_TARGET_ATR,
) -> Optional[dict]:
    """P1B-5 — turn the signal into a trade plan, or return None.

    The turn-up move is located structurally: find the lowest low of the
    recent correction, then take the highest high SINCE that low — that
    is the swing high the bounce has to clear to prove it is continuing
    rather than a one-day head-fake.

        entry  = swing high * 1.001        (buy stop)
        limit  = entry * 1.002             (slippage cap on a stop-limit)
        stop   = min(swing low * 0.999, entry - 1 x ATR)
        target = entry + 2 x risk

    The stop sits below BOTH the structure low and one ATR: below the low
    because that is where the thesis fails, and at least an ATR away so a
    normal day's noise cannot take it out.

    Returns None when a >= 2R target is not structurally realistic — a
    2R move that would need more than `max_target_atr` ATRs is not going
    to happen inside a 15-bar window, and the spec says reject rather
    than force it.
    """
    if bars is None or len(bars) < max(swing_lookback + 1, ATR_PERIOD + 1):
        return None
    hc, lc, cc = _c(bars, "high"), _c(bars, "low"), _c(bars, "close")
    if not (hc and lc and cc):
        return None

    high = pd.to_numeric(bars[hc], errors="coerce")
    low = pd.to_numeric(bars[lc], errors="coerce")
    if high.iloc[-swing_lookback:].isna().any() or low.iloc[-swing_lookback:].isna().any():
        return None

    atr = _wilder_atr(bars)
    if atr is None or atr <= 0:
        return None

    win = min(swing_lookback, len(low) - 1)
    low_slice = low.iloc[-win:]
    low_pos = int(np.argmin(low_slice.values))
    start = len(low) - win + low_pos

    swing_low = float(low.iloc[start:].min())
    swing_high = float(high.iloc[start:].max())
    if not (np.isfinite(swing_low) and np.isfinite(swing_high)):
        return None

    entry = swing_high * (1.0 + ENTRY_BUFFER_PCT)
    entry_limit = entry * (1.0 + ENTRY_LIMIT_PCT)
    stop = min(swing_low * (1.0 - STOP_BUFFER_PCT), entry - STOP_ATR_FLOOR_MULT * atr)

    risk = entry - stop
    if risk <= 0:
        return None

    target = entry + reward_multiple * risk
    reward = target - entry
    if reward > max_target_atr * atr:
        return None      # a 2R target is not structurally realistic here

    return {
        "entry":          round(entry, 2),
        "entry_limit":    round(entry_limit, 2),
        "stop":           round(stop, 2),
        "target":         round(target, 2),
        "risk_per_share": round(risk, 2),
        "rr_ratio":       round(reward / risk, 2),
        "stop_pct":       risk / entry,
        "swing_high":     round(swing_high, 2),
        "swing_low":      round(swing_low, 2),
        "atr14":          atr,
    }


# ─────────────────────────────────────────────────────────────────────
# P1B-6 — master gate
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
        time_stop_days=TIME_STOP_DAYS, risk_per_share=None,
        reward_per_share=None, rr_ratio=None, stop_pct=None, **fields,
    )


def market_regime_ok_spy200(spy_hist: Optional[pd.DataFrame]) -> tuple[bool, str]:
    """The B2 comparison gate — SPY above its 200-day SMA only.

    Deliberately weaker than Phase 1's (SPY above BOTH its 50d and 200d):
    the spec asks specifically for an SPY>200SMA gate here, because a
    dip-buying strategy that also demands a strong short-term tape would
    be gating away its own premise.
    """
    if spy_hist is None or len(spy_hist) < 200:
        return False, "regime desconhecido (sem histórico SPY) — fail closed"
    cc = _c(spy_hist, "close")
    if cc is None:
        return False, "regime desconhecido (SPY sem close) — fail closed"
    close = pd.to_numeric(spy_hist[cc], errors="coerce")
    last = float(close.iloc[-1])
    sma200 = _sma(close, 200)
    if sma200 is None or not np.isfinite(last):
        return False, "regime desconhecido (SMA200 SPY indisponível) — fail closed"
    if last < sma200:
        return False, f"SPY {last:.2f} < SMA200 {sma200:.2f} — sem novas entradas"
    return True, f"SPY {last:.2f} > SMA200 {sma200:.2f}"


def evaluate_bars(
    ticker: str,
    hist: pd.DataFrame,
    *,
    spy_hist: Optional[pd.DataFrame] = None,
    regime_ok: Optional[bool] = None,
    use_regime_gate: bool = False,
    min_avg_vol: float = MIN_AVG_VOL,
    min_rvol: float = MIN_RVOL,
    check_earnings: bool = True,
    get_earnings_days_fn=None,
):
    """P1B-6 — the full gate. Signature matches Phase 1's `evaluate_bars`
    so the backtest engine can call either behind a flag.

    Returns a `SwingSetup` (reused from Phase 1 so downstream sizing,
    exits and the fill simulator work unchanged). `qualifies=False`
    carries the reason, for diagnostics.

    `use_regime_gate` defaults to FALSE — the base case per P1B-6. The
    spec measures the gate's effect as a separate run rather than
    assuming it helps.
    """
    from core.swing_setups import SwingSetup

    # ── Regime (only when explicitly requested) ──────────────────────
    if use_regime_gate:
        if regime_ok is None:
            regime_ok, regime_msg = market_regime_ok_spy200(spy_hist)
        else:
            regime_msg = "regime pré-calculado pelo scan"
        if not regime_ok:
            return _reject(ticker, f"regime de mercado: {regime_msg}")

    if hist is None or len(hist) < 200:
        return _reject(ticker, "histórico insuficiente (< 200 barras para SMA200)")
    cc, hc, lc = _c(hist, "close"), _c(hist, "high"), _c(hist, "low")
    if not (cc and hc and lc):
        return _reject(ticker, "OHLC incompleto")

    close = pd.to_numeric(hist[cc], errors="coerce")
    last_close = float(close.iloc[-1]) if np.isfinite(close.iloc[-1]) else None
    if last_close is None or last_close <= 0:
        return _reject(ticker, "preço inválido")

    ctx = {
        "last_close": last_close,
        "last_high":  float(pd.to_numeric(hist[hc], errors="coerce").iloc[-1]),
        "ma20":  _sma(close, 20),
        "ma50":  _sma(close, 50),
        "ma200": _sma(close, 200),
        "avg_volume_20d": average_volume(hist),
    }
    _rv = relative_volume(hist)
    if _rv is not None:
        ctx["volume_ratio_20d"] = _rv      # display slot; RVOL is the 50d version

    # ── 1. Exclusions (cheapest) ─────────────────────────────────────
    ok, why = passes_exclusions(ticker, hist)
    if not ok:
        return _reject(ticker, why, **ctx)

    # ── 2. Average volume >= 200k ────────────────────────────────────
    if not passes_avg_volume(hist, min_avg_vol):
        av = ctx.get("avg_volume_20d")
        detail = f"{av/1e3:.0f}k" if av is not None else "indisponível"
        return _reject(
            ticker,
            f"volume médio {detail} < {min_avg_vol/1e3:.0f}k ações/dia",
            **ctx,
        )

    # ── 3. Relative volume > 1.5 ─────────────────────────────────────
    if not passes_relative_volume(hist, min_rvol, RVOL_LOOKBACK):
        detail = f"{_rv:.2f}×" if _rv is not None else "indisponível"
        return _reject(
            ticker,
            f"RVOL {detail} <= {min_rvol:.1f} — bounce sem participação",
            **ctx,
        )

    # ── 4. The core signal ───────────────────────────────────────────
    if not is_dip_in_uptrend(hist):
        s20, s50, s200 = ctx["ma20"], ctx["ma50"], ctx["ma200"]
        if s20 is None or s50 is None or s200 is None:
            return _reject(ticker, "médias móveis indisponíveis", **ctx)
        if last_close < s20 * (1 + SMA20_POP_PCT):
            why = (f"close {last_close:.2f} < SMA20 {s20:.2f} "
                   f"+{SMA20_POP_PCT*100:.0f}% — sem pop de reversão")
        elif last_close >= s50:
            why = f"close {last_close:.2f} >= SMA50 {s50:.2f} — não houve dip real"
        else:
            why = (f"close {last_close:.2f} <= SMA200 {s200:.2f} "
                   f"— uptrend de longo prazo quebrado")
        return _reject(ticker, why, **ctx)

    # ── 5. Earnings ──────────────────────────────────────────────────
    days_to_earnings = None
    if check_earnings:
        ok, days_to_earnings, why = passes_earnings_gate(
            ticker, time_stop_days=TIME_STOP_DAYS,
            get_earnings_days_fn=get_earnings_days_fn,
        )
        if not ok:
            s = _reject(ticker, why, **ctx)
            s.days_to_earnings = days_to_earnings
            s.earnings_in_window = True
            return s

    # ── 6. Structure supports >= 2R ──────────────────────────────────
    plan = build_structure(hist)
    if plan is None:
        return _reject(
            ticker,
            f"estrutura não suporta {REWARD_MULTIPLE:.0f}R "
            f"(swing/ATR inviável) — não forçar",
            **ctx,
        )

    atr = plan["atr14"]
    setup = SwingSetup(
        ticker=ticker, qualifies=True, setup_type=SETUP_TYPE, reject_reason="",
        entry=plan["entry"], entry_limit=plan["entry_limit"],
        stop=plan["stop"], target=plan["target"],
        time_stop_days=TIME_STOP_DAYS,
        risk_per_share=plan["risk_per_share"],
        reward_per_share=round(plan["target"] - plan["entry"], 2),
        rr_ratio=plan["rr_ratio"], stop_pct=plan["stop_pct"],
        pause_high=plan["swing_high"], pause_low=plan["swing_low"],
        atr14=atr, atr_pct=(atr / last_close) if last_close else None,
        rsi14=None, return_5d=None, return_20d=None, rs_vs_spy_63d=None,
        pause_volume_ratio=None,
        dollar_volume_20d=((ctx.get("avg_volume_20d") or 0.0) * last_close) or None,
        days_to_earnings=days_to_earnings,
        **{k: ctx.get(k) for k in
           ("last_close", "last_high", "ma20", "ma50", "ma200",
            "avg_volume_20d", "volume_ratio_20d")},
    )
    setup.plan_lines = [
        f"dip-in-uptrend: close {last_close:.2f} está "
        f"{(last_close / ctx['ma20'] - 1) * 100:.1f}% acima da SMA20, "
        f"abaixo da SMA50 {ctx['ma50']:.2f}, acima da SMA200 {ctx['ma200']:.2f}"
        + (f" · RVOL {_rv:.2f}×" if _rv is not None else "")
    ]
    return setup
