"""strategies/ — pluggable entry strategies behind a --strategy flag.

Each strategy exposes ONE function with a common signature:

    evaluate_bars(ticker, hist, *, spy_hist=None, regime_ok=None, **kw)
        -> SwingSetup

`SwingSetup` is reused from core.swing_setups for every strategy, so the
downstream machinery — sizing (Phase 2), the exit ladder (Phase 3) and
the fill simulator (Phase 4) — works unchanged whichever entry produced
the plan. That is what makes a strategy comparison meaningful: only the
entry differs.

Registered strategies:
    trend_pullback  — Phase 1. Buys leaders: close > SMA50 > SMA200,
                      pulling back 1-3 ATR on declining volume.
    dip_in_uptrend  — Phase 1B. Buys correctors: SMA200 < close < SMA50,
                      popped >= 5% above SMA20 on RVOL > 1.5.
    panic_reversion — H1. Buys forced sellers: close > SMA200, flushed
                      >= 2.5 ATR over 10 days, LIMIT entry below.
    gap_continuation— H2. Buys extraordinary strength: open gaps >= 1 ATR
                      over the 20-day high on RVOL >= 2, MARKET entry.

The first two are mutually exclusive by construction (one needs close
ABOVE SMA50, the other BELOW it). They must never be merged into one
gate chain or the conditions cancel and nothing is ever emitted.

`panic_reversion` and `gap_continuation` differ in kind, not just in
gates: one enters on a LIMIT below the market and the other at MARKET on
the next open, and each has its own registered exit ladder. Neither runs
through `core.swing_backtest`'s `simulate_trade` — see
`core/h1_backtest.py` and `core/h2_backtest.py`.
"""
from __future__ import annotations

from typing import Callable

DEFAULT_STRATEGY = "trend_pullback"
STRATEGY_NAMES = ("trend_pullback", "dip_in_uptrend", "panic_reversion",
                  "gap_continuation")


def get_strategy(name: str = DEFAULT_STRATEGY) -> Callable:
    """Resolve a strategy name to its `evaluate_bars` callable.

    Imports lazily so that loading this package does not drag in pandas,
    yfinance or the Phase 1 module when a caller only wants the names.
    """
    key = (name or DEFAULT_STRATEGY).strip().lower()
    if key == "trend_pullback":
        from strategies.trend_pullback import evaluate_bars
        return evaluate_bars
    if key == "dip_in_uptrend":
        from strategies.dip_in_uptrend import evaluate_bars
        return evaluate_bars
    if key == "panic_reversion":
        from strategies.panic_reversion import evaluate_bars
        return evaluate_bars
    if key == "gap_continuation":
        from strategies.gap_continuation import evaluate_bars
        return evaluate_bars
    raise ValueError(
        f"unknown strategy {name!r} — choose from {', '.join(STRATEGY_NAMES)}"
    )


def describe(name: str) -> str:
    return {
        "trend_pullback": "Phase 1 — trend pullback (buy leaders above SMA50)",
        "dip_in_uptrend": "Phase 1B — dip in a long-term uptrend (buy correctors below SMA50)",
        "panic_reversion": "H1 — panic mean reversion (buy a multi-day flush on a limit)",
        "gap_continuation": "H2 — strong-gap continuation (buy the next open, at market)",
    }.get((name or "").strip().lower(), name)
