"""strategies/trend_pullback.py — Phase 1 adapter.

A pass-through to `core.swing_setups.evaluate_bars`. Phase 1's entry
logic is NOT copied, wrapped in new rules, or modified in any way — the
Phase 1B spec forbids touching it, and a comparison against a benchmark
that quietly drifted would be worthless.

This file exists only so both strategies are reachable through one
registry with one signature, letting the backtest engine swap entries
behind a flag while everything downstream stays identical.
"""
from __future__ import annotations

from typing import Optional

import pandas as pd

STRATEGY_NAME = "trend_pullback"


def evaluate_bars(
    ticker: str,
    hist: pd.DataFrame,
    *,
    spy_hist: Optional[pd.DataFrame] = None,
    regime_ok: Optional[bool] = None,
    **_ignored,
):
    """Phase 1's gate, untouched.

    `**_ignored` absorbs Phase 1B-only keyword arguments (min_avg_vol,
    min_rvol, use_regime_gate, ...) so a caller can pass one uniform
    kwarg set to either strategy. Phase 1 genuinely does not have those
    knobs, and silently ignoring them here is correct — inventing
    equivalents would change the benchmark.
    """
    from core.swing_setups import evaluate_bars as _phase1_evaluate_bars
    return _phase1_evaluate_bars(
        ticker, hist, spy_hist=spy_hist, regime_ok=regime_ok,
    )
