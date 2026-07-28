"""core/swing_setups.py — Swing entry filter, v2 (Stock Swing Trading Plan §4).

Implements the seven-gate entry checklist from the trading plan, in the
order the plan specifies. Any failure = no trade. Gates are ordered
cheap-to-expensive so the universe sweep stays fast.

  1. MARKET REGIME   SPY > its own SMA50 AND SMA200.
                     A hostile market overrides any setup, however clean.
                     Fails CLOSED: unknown regime = no trading.
  2. UNIVERSE        price >= $10, avg volume >= 1M shares, not excluded.
  3. EARNINGS        no report inside the holding window (also satisfies
                     the plan's "not within 10 calendar days" rule).
  4. UPTREND         close > SMA50 > SMA200, SMA50 rising vs 20 bars ago.
  5. RELATIVE STR.   stock's 63-bar return > SPY's. Buy leaders, not laggards.
  6. VALID PAUSE     pullback 1-3x ATR(14) off the 20-day high, on DECLINING
                     volume, never closing below SMA50.
  7. STRUCTURE >=2R  entry above the pause high, stop below the pause low
                     (or entry - 1 ATR), target at 2R — and that 2R move
                     must be plausible within the holding window.

WHAT CHANGED FROM v1 AND WHY
  The v1 engine failed its Phase 4 gate: 4,193 replayed trades, expectancy
  -0.017R, win rate 34.5% against a 35.1% breakeven. It was a coin flip
  that paid costs. v1 had no regime filter (so it bought into 2022's
  downtrend), no relative-strength filter (so it bought laggards), and a
  1.5:1 reward:risk floor (so it needed a 40% hit rate it never had).

  BREAKOUT setups are REMOVED. Per the plan's journal entry (AAPL,
  2026-07-07): "a breakout chased the day after a strong run is prone to
  shakeout. Fix now codified in Section 4: wait for the pullback entry
  instead of chasing the high." Only PULLBACK survives.

  Entry/stop are now STRUCTURAL (pause high / pause low) rather than pure
  ATR offsets, matching how the OTOCO bracket is actually placed.

Cost profile: gates 1-2 and 4-7 read only OHLCV bars plus one shared SPY
series. The earnings lookup (gate 3) runs only on names that already
passed everything else. No LLM is invoked in swing mode at all.

NOT VALIDATED at v2 until scripts/run_swing_validation.py says otherwise.
Per the plan's own guardrail: "No tuning rules mid-stream to chase a
result." These thresholds come from the written spec, not from fitting.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, asdict, field
from typing import Optional

import numpy as np
import pandas as pd

log = logging.getLogger("swing")


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


# ── Gate 1: market regime (plan rule 11) ─────────────────────────────
REGIME_FAST_SMA     = _envi("SWING_REGIME_FAST_SMA", 50)
REGIME_SLOW_SMA     = _envi("SWING_REGIME_SLOW_SMA", 200)
REGIME_GATE_ENABLED = os.environ.get("SWING_REGIME_GATE", "1").strip() != "0"

# ── Gate 2: universe (plan rule 7) ───────────────────────────────────
MIN_PRICE           = _envf("SWING_MIN_PRICE", 10.0)
MIN_AVG_VOLUME      = _envf("SWING_MIN_AVG_VOLUME", 1_000_000.0)   # SHARES
# Permanent exclusions. PLTR is named in the plan; the set is the hook
# for any name the user decides they will never trade again.
EXCLUDED_TICKERS = frozenset(
    t.strip().upper()
    for t in os.environ.get("SWING_EXCLUDED_TICKERS", "PLTR").split(",")
    if t.strip()
)

# ── Gate 3: earnings (plan rule 10) ──────────────────────────────────
EXCLUDE_EARNINGS      = os.environ.get("SWING_EXCLUDE_EARNINGS", "1").strip() != "0"
EARNINGS_MIN_DAYS     = _envi("SWING_EARNINGS_MIN_DAYS", 10)   # calendar

# ── Gate 5: relative strength (plan §4) ──────────────────────────────
RS_LOOKBACK_BARS      = _envi("SWING_RS_LOOKBACK", 63)         # ~3 months

# ── Gate 6: the pause ────────────────────────────────────────────────
ATR_PERIOD            = _envi("SWING_ATR_PERIOD", 14)
PULLBACK_MIN_ATR      = _envf("SWING_PULLBACK_MIN_ATR", 1.0)
PULLBACK_MAX_ATR      = _envf("SWING_PULLBACK_MAX_ATR", 3.0)   # was 4.0 in v1
PAUSE_MAX_BARS        = _envi("SWING_PAUSE_MAX_BARS", 10)
VOLUME_DECLINE_RATIO  = _envf("SWING_VOLUME_DECLINE_RATIO", 1.0)

# ── Gate 7: structure + risk plan ────────────────────────────────────
ENTRY_BUFFER_PCT      = _envf("SWING_ENTRY_BUFFER_PCT", 0.001)  # trigger over pause high
ENTRY_LIMIT_PCT       = _envf("SWING_ENTRY_LIMIT_PCT", 0.002)   # slippage cap on the limit
STOP_BUFFER_PCT       = _envf("SWING_STOP_BUFFER_PCT", 0.001)   # under pause low
STOP_ATR_FLOOR_MULT   = _envf("SWING_STOP_ATR_FLOOR", 1.0)      # stop at least 1 ATR away
TARGET_R_MULT         = _envf("SWING_TARGET_R_MULT", 2.0)       # plan: target >= 2R
MIN_RR                = _envf("SWING_MIN_RR", 2.0)              # was 1.5 in v1
MAX_TARGET_ATR        = _envf("SWING_MAX_TARGET_ATR", 4.0)      # 2R must be reachable
MAX_STOP_PCT          = _envf("SWING_MAX_STOP_PCT", 0.08)
TIME_STOP_DAYS        = _envi("SWING_TIME_STOP_DAYS", 15)

_last_run_stats: dict = {}


def get_last_run_stats() -> dict:
    """Per-gate breakdown of the most recent get_swing_setups() call.

    When a scan returns nothing the UI answers "why?" with counts, instead
    of leaving the user guessing between "no setups today" and "it broke".
    """
    return dict(_last_run_stats)


# ─────────────────────────────────────────────────────────────────────
# Indicators — computed from OHLCV bars, no network beyond the bars
# ─────────────────────────────────────────────────────────────────────

def _wilder_atr(hist: pd.DataFrame, period: int = ATR_PERIOD) -> Optional[float]:
    """Wilder-smoothed ATR. None when High/Low are missing or too short.

    True Range uses the previous close, so a gap counts as range — the
    point for a stop-distance model, since overnight gaps are the risk a
    swing trader is actually exposed to.
    """
    if hist is None or len(hist) < period + 1:
        return None
    cols = {c.lower(): c for c in hist.columns}
    if not {"high", "low", "close"}.issubset(cols):
        return None
    high = hist[cols["high"]].astype(float)
    low = hist[cols["low"]].astype(float)
    prev_close = hist[cols["close"]].astype(float).shift(1)

    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1).dropna()
    if len(tr) < period:
        return None
    atr = tr.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    val = float(atr.iloc[-1])
    return val if np.isfinite(val) and val > 0 else None


def _rsi(close: pd.Series, period: int = 14) -> float:
    """Wilder RSI. Returns a neutral 50.0 when the series is too short (a
    missing RSI must not read as oversold-and-therefore-attractive)."""
    delta = close.astype(float).diff().dropna()
    if len(delta) < period:
        return 50.0
    gain = delta.clip(lower=0).ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    if rsi.empty or not np.isfinite(rsi.iloc[-1]):
        return 50.0
    return float(rsi.iloc[-1])


def _sma(close: pd.Series, window: int) -> Optional[float]:
    if len(close) < window:
        return None
    val = float(close.astype(float).iloc[-window:].mean())
    return val if np.isfinite(val) else None


def _pct_return(close: pd.Series, bars: int) -> Optional[float]:
    if len(close) < bars + 1:
        return None
    a, b = float(close.iloc[-bars - 1]), float(close.iloc[-1])
    if a <= 0:
        return None
    return b / a - 1.0


def market_regime_ok(spy_hist: Optional[pd.DataFrame]) -> tuple[bool, str]:
    """Gate 1 — plan rule 11. SPY above BOTH its 50d and 200d SMA.

    Fails CLOSED on missing data. The plan calls this a hard gate; a
    regime we cannot verify is not a regime we may trade into.
    """
    if not REGIME_GATE_ENABLED:
        return True, "regime gate desactivado (SWING_REGIME_GATE=0)"
    if spy_hist is None or len(spy_hist) < REGIME_SLOW_SMA:
        return False, "regime desconhecido (sem histórico SPY suficiente) — fail closed"

    cols = {c.lower(): c for c in spy_hist.columns}
    close = spy_hist[cols["close"]].astype(float)
    last = float(close.iloc[-1])
    fast = _sma(close, REGIME_FAST_SMA)
    slow = _sma(close, REGIME_SLOW_SMA)
    if fast is None or slow is None:
        return False, "regime desconhecido (SMA SPY indisponível) — fail closed"

    if last < fast:
        return False, (f"SPY {last:.2f} < SMA{REGIME_FAST_SMA} {fast:.2f} "
                       f"— gate rápido: sem novas entradas long")
    if last < slow:
        return False, (f"SPY {last:.2f} < SMA{REGIME_SLOW_SMA} {slow:.2f} "
                       f"— gate duro: sem novas entradas long")
    return True, f"SPY {last:.2f} > SMA50 {fast:.2f} > /  SMA200 {slow:.2f}"


# ─────────────────────────────────────────────────────────────────────
# Setup record
# ─────────────────────────────────────────────────────────────────────

@dataclass
class SwingSetup:
    """A complete trade plan, or an explicit rejection with its reason.

    `qualifies=False` entries are still returned by evaluate_swing() so
    /analisar can show WHY a name is not tradeable today.
    """
    ticker:            str
    qualifies:         bool
    setup_type:        str               # "PULLBACK" | "NONE"
    reject_reason:     str

    # ── The plan ──
    entry:             Optional[float]   # buy-stop trigger
    entry_limit:       Optional[float]   # stop-limit cap (slippage guard)
    stop:              Optional[float]
    target:            Optional[float]
    time_stop_days:    int
    risk_per_share:    Optional[float]   # entry - stop  (1R, in $)
    reward_per_share:  Optional[float]
    rr_ratio:          Optional[float]
    stop_pct:          Optional[float]

    # ── Context that produced it ──
    last_close:        Optional[float]
    last_high:         Optional[float]
    pause_high:        Optional[float]
    pause_low:         Optional[float]
    atr14:             Optional[float]
    atr_pct:           Optional[float]
    rsi14:             Optional[float]
    ma20:              Optional[float]
    ma50:              Optional[float]
    ma200:             Optional[float]
    return_5d:         Optional[float]
    return_20d:        Optional[float]
    rs_vs_spy_63d:     Optional[float]   # stock 63d return - SPY 63d return
    volume_ratio_20d:  Optional[float]
    pause_volume_ratio: Optional[float]  # pause avg vol / prior avg vol
    avg_volume_20d:    Optional[float]   # shares
    dollar_volume_20d: Optional[float]

    # ── Risk flags (populated only for names that passed the trigger) ──
    days_to_earnings:  Optional[int] = None
    earnings_in_window: bool = False
    sector:            str = ""
    sector_relative_20d: Optional[float] = None

    summary_line:      str = ""
    plan_lines:        list = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    def otoco(self, shares: float) -> dict:
        """The OTOCO bracket from plan §6, ready to place.

        Order A (entry, stop-limit) triggers B (take-profit limit) and
        C (stop-loss market) as an OCO pair. Returning the whole bracket
        as data means the Telegram renderer and any future broker
        integration cannot disagree about the levels.
        """
        return {
            "A_entry": {
                "action": "BUY", "type": "Stop-Limit", "qty": shares,
                "stop_trigger": self.entry, "limit": self.entry_limit,
            },
            "B_take_profit": {
                "action": "SELL", "type": "Limit", "qty": shares,
                "limit": self.target,
            },
            "C_stop_loss": {
                "action": "SELL", "type": "Stop (market)", "qty": shares,
                "stop": self.stop,
            },
        }


def _reject(ticker: str, reason: str, **ctx) -> SwingSetup:
    """Non-qualifying setup carrying whatever context was computed before
    the gate failed. Keeps /analisar informative on rejections."""
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


# ─────────────────────────────────────────────────────────────────────
# The engine
# ─────────────────────────────────────────────────────────────────────

def evaluate_bars(
    ticker: str,
    hist: pd.DataFrame,
    *,
    spy_hist: Optional[pd.DataFrame] = None,
    regime_ok: Optional[bool] = None,
) -> SwingSetup:
    """Price-only evaluation — gates 1, 2, 4, 5, 6, 7. NO network calls.

    `spy_hist` must be aligned to `hist` (same trailing date) — the caller
    slices both to the same as-of bar. `regime_ok` lets a universe sweep
    compute the regime once and pass the answer down, instead of
    recomputing SPY's SMAs several hundred times.

    Gate 3 (earnings) needs a calendar lookup and lives in _enrich(),
    which runs only on names that survive everything here.
    """
    # ── Gate 1: market regime (before any per-ticker work) ───────────
    if regime_ok is None:
        regime_ok, regime_msg = market_regime_ok(spy_hist)
    else:
        regime_msg = "regime pré-calculado pelo scan"
    if not regime_ok:
        return _reject(ticker, f"regime de mercado: {regime_msg}")

    if hist is None or len(hist) < 210:
        return _reject(ticker, "histórico insuficiente (< 210 barras para SMA200)")

    cols = {c.lower(): c for c in hist.columns}
    if not {"high", "low", "close"}.issubset(cols):
        return _reject(ticker, "OHLC incompleto")

    close = hist[cols["close"]].astype(float)
    high = hist[cols["high"]].astype(float)
    low = hist[cols["low"]].astype(float)
    volume = hist[cols["volume"]].astype(float) if "volume" in cols else None

    last_close = float(close.iloc[-1])
    last_high = float(high.iloc[-1])
    last_low = float(low.iloc[-1])
    if not np.isfinite(last_close) or last_close <= 0:
        return _reject(ticker, "preço inválido")

    atr = _wilder_atr(hist)
    if atr is None:
        return _reject(ticker, "ATR indisponível", last_close=last_close)
    atr_pct = atr / last_close

    ma20 = _sma(close, 20)
    ma50 = _sma(close, 50)
    ma200 = _sma(close, 200)
    rsi = _rsi(close)
    ret_5d = _pct_return(close, 5)
    ret_20d = _pct_return(close, 20)

    if volume is not None and len(volume) >= 20:
        avg_vol20 = float(volume.iloc[-20:].mean())
        vol_ratio = float(volume.iloc[-1] / avg_vol20) if avg_vol20 > 0 else None
        dollar_vol = avg_vol20 * last_close
    else:
        avg_vol20, vol_ratio, dollar_vol = None, None, None

    ctx = dict(
        last_close=last_close, last_high=last_high, atr14=atr, atr_pct=atr_pct,
        rsi14=rsi, ma20=ma20, ma50=ma50, ma200=ma200,
        return_5d=ret_5d, return_20d=ret_20d,
        volume_ratio_20d=vol_ratio, avg_volume_20d=avg_vol20,
        dollar_volume_20d=dollar_vol,
    )

    # ── Gate 2: universe (plan rule 7) ───────────────────────────────
    if ticker.upper() in EXCLUDED_TICKERS:
        return _reject(ticker, f"{ticker} está na lista de exclusão permanente", **ctx)
    if last_close < MIN_PRICE:
        return _reject(ticker, f"preço ${last_close:.2f} < mínimo ${MIN_PRICE:.0f}", **ctx)
    # Rejecting on UNKNOWN volume is deliberate: a stop we cannot prove is
    # fillable is not a risk plan. Fail closed, not open.
    if avg_vol20 is None:
        return _reject(ticker, "volume indisponível — liquidez não verificável", **ctx)
    if avg_vol20 < MIN_AVG_VOLUME:
        return _reject(
            ticker,
            f"volume médio {avg_vol20/1e6:.2f}M ações/dia < {MIN_AVG_VOLUME/1e6:.1f}M",
            **ctx,
        )

    # ── Gate 4: genuine uptrend ──────────────────────────────────────
    if ma20 is None or ma50 is None or ma200 is None:
        return _reject(ticker, "médias móveis indisponíveis", **ctx)
    if not (last_close > ma50 > ma200):
        return _reject(
            ticker,
            f"sem uptrend: close {last_close:.2f} / SMA50 {ma50:.2f} / SMA200 {ma200:.2f} "
            f"(precisa close > SMA50 > SMA200)",
            **ctx,
        )
    ma50_prev = _sma(close.iloc[:-20], 50) if len(close) >= 70 else None
    if ma50_prev is not None and ma50 <= ma50_prev:
        return _reject(ticker, "SMA50 não está a subir (vs 20 barras atrás)", **ctx)

    # ── Gate 5: relative strength vs SPY (buy leaders, not laggards) ──
    rs = None
    if spy_hist is not None:
        scols = {c.lower(): c for c in spy_hist.columns}
        spy_close = spy_hist[scols["close"]].astype(float)
        stock_ret = _pct_return(close, RS_LOOKBACK_BARS)
        spy_ret = _pct_return(spy_close, RS_LOOKBACK_BARS)
        if stock_ret is not None and spy_ret is not None:
            rs = stock_ret - spy_ret
            ctx["rs_vs_spy_63d"] = rs
            if rs <= 0:
                return _reject(
                    ticker,
                    f"força relativa {rs*100:+.1f}pp vs SPY em {RS_LOOKBACK_BARS}d "
                    f"— laggard, não líder",
                    **ctx,
                )
    if rs is None:
        # The plan lists relative strength as a required gate. Without SPY
        # we cannot evaluate it, and skipping a required gate silently is
        # how unvalidated trades get taken.
        return _reject(ticker, "força relativa não avaliável (sem série SPY)", **ctx)

    # ── Gate 6: valid pause, not a breakdown ─────────────────────────
    high_20d = float(high.iloc[-20:].max())
    pullback_atr = (high_20d - last_close) / atr

    if not (PULLBACK_MIN_ATR <= pullback_atr <= PULLBACK_MAX_ATR):
        return _reject(
            ticker,
            f"recuo {pullback_atr:.1f}×ATR fora da janela "
            f"[{PULLBACK_MIN_ATR:.1f}, {PULLBACK_MAX_ATR:.1f}]",
            **ctx,
        )

    # Locate the pause: from the lowest low of the recent window to now.
    win = min(PAUSE_MAX_BARS, len(low) - 1)
    recent_low_pos = int(np.argmin(low.iloc[-win:].values))
    pause_start = len(low) - win + recent_low_pos
    pause_low = float(low.iloc[pause_start:].min())
    pause_high = float(high.iloc[pause_start:].max())
    ctx["pause_high"], ctx["pause_low"] = pause_high, pause_low

    # Never closed below SMA50 during the pause — that is a breakdown,
    # not a pause. Checked on CLOSES, so an intraday wick is forgiven.
    pause_closes = close.iloc[pause_start:]
    if float(pause_closes.min()) < ma50:
        return _reject(
            ticker,
            f"fechou abaixo da SMA50 durante a pausa "
            f"({float(pause_closes.min()):.2f} < {ma50:.2f}) — breakdown, não pausa",
            **ctx,
        )

    # Declining volume through the pause: selling pressure drying up.
    pause_vol_ratio = None
    if volume is not None and pause_start > 20:
        pause_vol = float(volume.iloc[pause_start:].mean())
        prior_vol = float(volume.iloc[pause_start - 20:pause_start].mean())
        if prior_vol > 0:
            pause_vol_ratio = pause_vol / prior_vol
            ctx["pause_volume_ratio"] = pause_vol_ratio
            if pause_vol_ratio > VOLUME_DECLINE_RATIO:
                return _reject(
                    ticker,
                    f"volume da pausa {pause_vol_ratio:.2f}× o anterior "
                    f"(precisa <= {VOLUME_DECLINE_RATIO:.2f}) — distribuição, não pausa",
                    **ctx,
                )

    # ── Gate 7: structure must support >= 2R ─────────────────────────
    # Entry above the pause high (buy stop-limit: pay for confirmation).
    entry = pause_high * (1 + ENTRY_BUFFER_PCT)
    entry_limit = entry * (1 + ENTRY_LIMIT_PCT)

    # Stop below the pause low AND at least 1 ATR away — whichever is
    # lower. A stop inside the noise band is not a stop.
    stop = min(pause_low * (1 - STOP_BUFFER_PCT), entry - STOP_ATR_FLOOR_MULT * atr)

    risk = entry - stop
    if risk <= 0:
        return _reject(ticker, "risco por acção não-positivo (dados inconsistentes)", **ctx)

    stop_pct = risk / entry
    if stop_pct > MAX_STOP_PCT:
        return _reject(
            ticker,
            f"stop {stop_pct*100:.1f}% > cap {MAX_STOP_PCT*100:.0f}% — "
            f"volátil demais para o modelo de risco",
            **ctx,
        )

    target = entry + TARGET_R_MULT * risk
    reward = target - entry
    rr = reward / risk

    # "If a 2:1 target isn't structurally realistic, skip the trade."
    # A 2R move must be plausible within the holding window: when the stop
    # is wide, 2R is a fantasy move, and this is the gate that says so.
    if reward > MAX_TARGET_ATR * atr:
        return _reject(
            ticker,
            f"alvo 2R = {reward/atr:.1f}×ATR > máximo {MAX_TARGET_ATR:.1f}×ATR "
            f"— movimento improvável em {TIME_STOP_DAYS} sessões",
            **ctx,
        )
    # 1e-9 tolerance: target is built as entry + 2.0*risk, so rr is exactly
    # MIN_RR up to float error. Without this, a valid setup is rejected with
    # the nonsensical message "R:R 2.00 < mínimo 2.00".
    if rr < MIN_RR - 1e-9:
        return _reject(ticker, f"R:R {rr:.2f} < mínimo {MIN_RR:.2f}", **ctx)

    setup = SwingSetup(
        ticker=ticker, qualifies=True, setup_type="PULLBACK", reject_reason="",
        entry=round(entry, 2), entry_limit=round(entry_limit, 2),
        stop=round(stop, 2), target=round(target, 2),
        time_stop_days=TIME_STOP_DAYS,
        risk_per_share=round(risk, 2), reward_per_share=round(reward, 2),
        rr_ratio=round(rr, 2), stop_pct=stop_pct,
        **ctx,
    )
    setup.plan_lines = [
        f"recuo {pullback_atr:.1f}×ATR do topo 20d, RSI {rsi:.0f}, "
        f"força relativa {rs*100:+.1f}pp vs SPY"
        + (f", volume da pausa {pause_vol_ratio:.2f}×" if pause_vol_ratio else "")
    ]
    return setup


def evaluate_swing(
    ticker: str,
    hist: Optional[pd.DataFrame] = None,
    *,
    spy_hist: Optional[pd.DataFrame] = None,
    enrich: bool = True,
) -> SwingSetup:
    """Full single-ticker evaluation: gates 1-2, 4-7, then the earnings check.

    Fetches history (and SPY) when not supplied — used by /analisar.
    `enrich=False` skips the earnings/sector lookups; the scan path uses
    that to keep the universe sweep price-only, then enriches survivors.
    """
    if hist is None:
        try:
            from core.core_radar import _fetch_hist
            hist = _fetch_hist(ticker, lookback_days=500)
        except Exception as e:
            log.debug(f"[swing] fetch {ticker}: {e}")
            hist = None
    if spy_hist is None:
        spy_hist = fetch_spy_history()

    setup = evaluate_bars(ticker, hist, spy_hist=spy_hist)
    if setup.qualifies and enrich:
        _enrich(setup)
    setup.summary_line = format_setup_line(setup)
    return setup


def fetch_spy_history() -> Optional[pd.DataFrame]:
    """SPY bars for the regime + relative-strength gates. None on failure,
    which makes both gates fail closed (no new longs)."""
    try:
        from core.core_radar import _fetch_hist
        return _fetch_hist("SPY", lookback_days=500)
    except Exception as e:
        log.warning(f"[swing] SPY history unavailable ({e}) — regime gate fails closed")
        return None


def _enrich(setup: SwingSetup) -> None:
    """Gate 3 (earnings) + display context. ONLY for names that passed
    every price gate.

    The plan states two earnings rules: do not enter within 10 calendar
    days of a report, and never hold through one. The holding window
    (15 trading days ~= 21 calendar) is the stricter of the two and
    subsumes the 10-day rule, so rejecting on the window satisfies both.
    """
    if EXCLUDE_EARNINGS:
        try:
            from core.swing_exits import trading_days_to_calendar
            from data.market_client import get_earnings_days
            window = max(EARNINGS_MIN_DAYS,
                         trading_days_to_calendar(setup.time_stop_days))
            days = get_earnings_days(setup.ticker, max_days=window + 20)
            setup.days_to_earnings = days
            if days is not None and 0 <= days <= window:
                setup.earnings_in_window = True
                setup.qualifies = False
                setup.reject_reason = (
                    f"earnings em {days}d — dentro da janela de {window}d "
                    f"(regra: não entrar a <{EARNINGS_MIN_DAYS}d nem segurar durante o report)"
                )
                return
        except Exception as e:
            log.debug(f"[swing] earnings {setup.ticker}: {e}")

    try:
        from utils.sector_cache import get_sector
        setup.sector = get_sector(setup.ticker) or ""
    except Exception as e:
        log.debug(f"[swing] sector {setup.ticker}: {e}")

    if setup.sector:
        try:
            from data.sector_relative import sector_relative_return
            setup.sector_relative_20d = sector_relative_return(
                setup.ticker, setup.sector, window_days=20
            )
        except Exception as e:
            log.debug(f"[swing] sector_rel {setup.ticker}: {e}")


# ─────────────────────────────────────────────────────────────────────
# Universe scan
# ─────────────────────────────────────────────────────────────────────

def _universe_for(source: str) -> list[str]:
    """Resolve the ticker list for a scan source.

    "core"    — the curated names in core_universe.json
    "default" — core + tickers the DIP scanner flagged in the last 14d
    "full"    — the full ~1500-name universe (slow; use deliberately)
    """
    tickers: list[str] = []
    try:
        from core.core_radar import _load_universe
        tickers = [e["ticker"] for e in (_load_universe() or []) if e.get("ticker")]
    except Exception as e:
        log.warning(f"[swing] CORE universe load failed: {e}")

    if source == "full":
        try:
            from core.universe import get_full_universe
            seen = set(tickers)
            tickers += [t for t in get_full_universe() if t not in seen]
        except Exception as e:
            log.warning(f"[swing] full universe failed: {e}")
    elif source == "default":
        try:
            from scanners.alert_db import get_recent_alert_tickers
            seen = set(tickers)
            tickers += [t for t in get_recent_alert_tickers(lookback_days=14) if t not in seen]
        except Exception as e:
            log.debug(f"[swing] DIP alert union skipped: {e}")
    return [t for t in tickers if t.upper() not in EXCLUDED_TICKERS]


def get_swing_setups(
    *,
    source: str = "default",
    max_setups: int = 0,
) -> list[SwingSetup]:
    """Scan the universe and return complete, tradeable setups.

    The market-regime gate is evaluated ONCE up front: when SPY is below
    its 50d or 200d SMA there are no new longs today, so the whole sweep
    is skipped rather than run to produce a list nobody may act on.

    Sorted by R:R descending. That ordering is arithmetic on the plan,
    not a claim about which name will win.
    """
    global _last_run_stats
    from datetime import datetime as _dt

    spy = fetch_spy_history()
    regime_ok, regime_msg = market_regime_ok(spy)
    log.info(f"[swing] regime: {'OK' if regime_ok else 'BLOCKED'} — {regime_msg}")

    if not regime_ok:
        _last_run_stats = {
            "regime_ok": False, "regime_msg": regime_msg, "n_universe": 0,
            "n_setups": 0, "source": source,
            "fetched_at_iso": _dt.now().isoformat(timespec="seconds"),
        }
        return []

    tickers = _universe_for(source)
    if not tickers:
        log.warning("[swing] universe empty")
        _last_run_stats = {"regime_ok": True, "n_universe": 0, "n_setups": 0,
                           "fetched_at_iso": _dt.now().isoformat(timespec="seconds")}
        return []

    log.info(f"[swing] scanning {len(tickers)} tickers (source={source}) — price gates")

    try:
        from core.core_radar import _fetch_hist
    except Exception as e:
        log.error(f"[swing] _fetch_hist import failed: {e}")
        return []

    passed: list[SwingSetup] = []
    counts = {"no_data": 0, "universe": 0, "trend": 0, "rs": 0,
              "pause": 0, "structure": 0, "other": 0}

    for t in tickers:
        try:
            hist = _fetch_hist(t, lookback_days=500)
        except Exception as e:
            log.debug(f"[swing] fetch {t}: {e}")
            hist = None
        if hist is None or hist.empty:
            counts["no_data"] += 1
            continue

        s = evaluate_bars(t, hist, spy_hist=spy, regime_ok=True)
        if s.qualifies:
            passed.append(s)
            continue
        counts[_bucket(s.reject_reason)] += 1

    log.info(
        f"[swing] price gates: {len(passed)} passed from {len(tickers)} — {counts}. "
        f"Now checking earnings on {len(passed)}."
    )

    setups: list[SwingSetup] = []
    n_earnings = 0
    for s in passed:
        _enrich(s)
        s.summary_line = format_setup_line(s)
        if s.qualifies:
            setups.append(s)
        elif s.earnings_in_window:
            n_earnings += 1

    setups.sort(key=lambda x: -(x.rr_ratio or 0.0))
    if max_setups > 0:
        setups = setups[:max_setups]

    log.info(f"[swing] {len(setups)} setups tradeable ({n_earnings} cut by earnings)")

    _last_run_stats = {
        "regime_ok":        True,
        "regime_msg":       regime_msg,
        "n_universe":       len(tickers),
        "n_no_data":        counts["no_data"],
        "n_universe_fail":  counts["universe"],
        "n_trend_fail":     counts["trend"],
        "n_rs_fail":        counts["rs"],
        "n_pause_fail":     counts["pause"],
        "n_structure_fail": counts["structure"],
        "n_other":          counts["other"],
        "n_passed_price":   len(passed),
        "n_earnings_fail":  n_earnings,
        "n_setups":         len(setups),
        "source":           source,
        "fetched_at_iso":   _dt.now().isoformat(timespec="seconds"),
    }
    return setups


def _bucket(reason: str) -> str:
    """Map a reject reason to a stats bucket, for the empty-scan diagnostic."""
    r = reason.lower()
    if any(k in r for k in ("histórico", "ohlc", "inválido", "atr indisponível")):
        return "no_data"
    if any(k in r for k in ("exclusão", "preço $", "volume médio", "volume indisponível")):
        return "universe"
    if any(k in r for k in ("uptrend", "sma50 não", "médias")):
        return "trend"
    if "força relativa" in r:
        return "rs"
    if any(k in r for k in ("recuo", "pausa", "breakdown")):
        return "pause"
    if any(k in r for k in ("stop ", "alvo", "r:r", "risco por")):
        return "structure"
    return "other"


# ─────────────────────────────────────────────────────────────────────
# Sizing — plan §5
# ─────────────────────────────────────────────────────────────────────

def position_size(
    setup: SwingSetup,
    equity_eur: float,
    *,
    risk_pct: Optional[float] = None,
    usd_eur: float = 1.0,
) -> dict:
    """Shares so that a stop-out costs exactly `risk_pct` of the pool.

    Plan §5: Risk $ = pool x risk_pct; Shares = Risk $ / stop distance,
    ROUNDED DOWN. Rounding down matters — rounding up would breach the
    1% rule on every trade, which is the plan's most important rule.

    `equity_eur` is the dedicated swing POOL, not total net worth.
    """
    if risk_pct is None:
        risk_pct = _envf("SWING_RISK_PCT", 0.01)   # plan rule 1: 1% live
    if not setup.qualifies or not setup.risk_per_share or setup.risk_per_share <= 0:
        return {"shares": 0.0, "notional_eur": 0.0, "risk_eur": 0.0, "risk_pct": risk_pct}
    if equity_eur <= 0:
        return {"shares": 0.0, "notional_eur": 0.0, "risk_eur": 0.0, "risk_pct": risk_pct}

    risk_eur = equity_eur * risk_pct
    risk_usd = risk_eur / usd_eur if usd_eur > 0 else risk_eur
    shares = float(np.floor(risk_usd / setup.risk_per_share))
    if shares < 1:
        return {
            "shares": 0.0, "notional_eur": 0.0, "risk_eur": 0.0, "risk_pct": risk_pct,
            "note": (f"risco €{risk_eur:.0f} não cobre 1 acção "
                     f"(stop ${setup.risk_per_share:.2f}/acção)"),
        }

    notional_eur = shares * (setup.entry or 0.0) * usd_eur
    actual_risk_eur = shares * setup.risk_per_share * usd_eur
    return {
        "shares":       shares,
        "notional_eur": round(notional_eur, 2),
        "risk_eur":     round(actual_risk_eur, 2),
        "risk_pct":     risk_pct,
    }


# ─────────────────────────────────────────────────────────────────────
# Rendering
# ─────────────────────────────────────────────────────────────────────

def format_setup_line(s: SwingSetup) -> str:
    if not s.qualifies:
        return f"{s.ticker:6s} — {s.reject_reason}"
    return (
        f"{s.ticker:6s} {s.setup_type:8s} "
        f"entry {s.entry:>8.2f}  stop {s.stop:>8.2f} ({s.stop_pct*100:+.1f}%)  "
        f"target {s.target:>8.2f}  R:R {s.rr_ratio:.1f}"
    )


def format_otoco_telegram(s: SwingSetup, shares: float) -> list[str]:
    """The plan §6 bracket, rendered so it can be typed into a broker."""
    return [
        f"`A ENTRY   BUY  Stop-Limit  {shares:g} sh`",
        f"`          trigger {s.entry:.2f}  ·  limit {s.entry_limit:.2f}`",
        f"`B TARGET  SELL Limit       {shares:g} sh  @ {s.target:.2f}`",
        f"`C STOP    SELL Stop-mkt    {shares:g} sh  @ {s.stop:.2f}`",
    ]


def format_swing_telegram(
    setups: list[SwingSetup],
    *,
    header: str = "⚡ *Swing Setups*",
    equity_eur: float = 0.0,
    usd_eur: float = 1.0,
    include_disclaimer: bool = True,
) -> str:
    """Render setups as a Telegram message with the OTOCO bracket."""
    st = get_last_run_stats()

    if not setups:
        lines = [header, ""]
        if st and st.get("regime_ok") is False:
            lines += [
                "🛑 *Mercado hostil — sem novas entradas long.*",
                "",
                f"_{st.get('regime_msg', '')}_",
                "",
                "_Regra 11: SPY abaixo da SMA50 (gate rápido) ou SMA200 (gate duro) "
                "trava qualquer setup, por mais limpo que seja. "
                "Posições abertas continuam geridas pelos seus stops._",
            ]
            return "\n".join(lines)

        lines.append("⚪ *Sem setups hoje.*")
        if st:
            lines += [
                "",
                "📊 *Onde caíram:*",
                f"  • Universo: *{st.get('n_universe', '?')}*",
                f"  • Sem dados: *{st.get('n_no_data', 0)}*",
                f"  • Universo (preço/volume/exclusão): *{st.get('n_universe_fail', 0)}*",
                f"  • Sem uptrend: *{st.get('n_trend_fail', 0)}*",
                f"  • Força relativa (laggards): *{st.get('n_rs_fail', 0)}*",
                f"  • Pausa inválida: *{st.get('n_pause_fail', 0)}*",
                f"  • Estrutura < 2R: *{st.get('n_structure_fail', 0)}*",
                f"  • Earnings na janela: *{st.get('n_earnings_fail', 0)}*",
                "",
                "_Sem setup é a posição correcta na maioria dos dias. "
                "Falhar uma trade não custa nada; uma trade má custa 1%._",
            ]
        return "\n".join(lines)

    lines = [
        header,
        "",
        f"_{len(setups)} setup(s) · todos os 7 gates passados · alvo 2R · "
        f"time stop {TIME_STOP_DAYS} sessões._",
    ]
    if st.get("regime_msg"):
        lines.append(f"_Regime: {st['regime_msg']}_")
    lines.append("")

    for s in setups:
        lines.append(f"*{s.ticker}* · {s.setup_type} · R:R *{s.rr_ratio:.1f}*")
        sz = position_size(s, equity_eur, usd_eur=usd_eur) if equity_eur > 0 else None
        if sz and sz["shares"] > 0:
            lines += format_otoco_telegram(s, sz["shares"])
            lines.append(
                f"`  risco €{sz['risk_eur']:.0f} "
                f"({sz['risk_pct']*100:.1f}%)  ·  posição ≈ €{sz['notional_eur']:,.0f}`"
            )
        else:
            lines.append(f"`  entry {s.entry:.2f} (lim {s.entry_limit:.2f})`")
            lines.append(f"`  stop  {s.stop:.2f}  ({s.stop_pct*100:+.1f}% = 1R)`")
            lines.append(f"`  target {s.target:.2f}`")
            if sz and sz.get("note"):
                lines.append(f"  ⚠️ _{sz['note']}_")

        _ctx = []
        if s.rs_vs_spy_63d is not None:
            _ctx.append(f"RS {s.rs_vs_spy_63d*100:+.0f}pp vs SPY")
        if s.rsi14 is not None:
            _ctx.append(f"RSI {s.rsi14:.0f}")
        if s.atr_pct is not None:
            _ctx.append(f"ATR {s.atr_pct*100:.1f}%")
        if s.days_to_earnings is not None:
            _ctx.append(f"earnings {s.days_to_earnings}d")
        if _ctx:
            lines.append(f"  _{' · '.join(_ctx)}_")
        lines.append("")

    if include_disclaimer:
        lines += [
            "_Coloca as 3 ordens juntas (OTOCO). Nunca segures a posição sem a ordem C viva._",
            f"_Break-even ratchet a +1R · time stop {TIME_STOP_DAYS} sessões · máx 2 posições._",
        ]
    return "\n".join(lines)
