"""core/c_series_backtest.py — C-0c: the C-series replay engine.

Single instrument (SPY), no stops, results in PERCENT per trade. That is
three departures from every other backtest in this project, and each is
registered in `connors_rsi2_spec.md` rather than chosen here:

  - No stop-loss. It is the book's design. The consequence is that win
    rate is close to meaningless on its own and DRAWDOWN REPORTING IS
    MANDATORY, which is why `equity_curve_stats` and `worst_trades` are
    part of the metric set and not optional extras.
  - No R-multiples. There is no stop, so there is no 1R to divide by.
    Reporting R here would require inventing a denominator.
  - One instrument. No survivorship question (SPY has existed the whole
    period), no clustering/capacity problem, and a 2bps/side cost
    assumption that is actually defensible.

EXECUTION MODES (C-0c). Two, always reported side by side:

  "close"     — fill at the signal bar's close. The book's convention.
                Legitimate only because the signal uses no data beyond
                the close it fills at: RSI(2), SMA5 and SMA200 at bar i
                are all functions of closes[:i+1]. `confirm_no_lookahead`
                verifies this empirically per run rather than trusting
                the argument.
  "next_open" — fill at the NEXT bar's open, for BOTH entry and exit.
                The spec frames this as "a retail trader placing the
                order after the close", and such a trader is late on the
                way out as well as on the way in. Applying it to entries
                only would model a trader with impossible reflexes at
                exactly the moment it flatters the result.

The gate is judged on close-fill; the gap between the two measures how
much of the edge is execution-fragile.

TIME VALVE: if neither exit condition fires within 25 trading days, exit
at the close. Registered to stop a broken-trend position riding for
months underwater. Its firing count is reported — a valve that fires
often means the exit rule is not doing its job.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, asdict
from typing import Optional

import numpy as np
import pandas as pd

from strategies.rsi2 import Indicators, TIME_VALVE_BARS, Variant, get_variant

log = logging.getLogger("c_series")

# Registered in the spec: SPY's spread is ~1 cent on a ~$600 instrument.
SPY_SLIPPAGE_BPS = 2.0
COMMISSION_PER_SIDE = 0.0     # same commission model as prior phases


@dataclass
class CTrade:
    variant:      str
    entry_date:   str
    exit_date:    str
    entry_index:  int
    exit_index:   int
    bars_held:    int
    entry_price:  float       # after costs
    exit_price:   float       # after costs
    pct_return:   float       # % on the position, the headline number
    mae_pct:      float       # worst open-trade drawdown before exit
    mfe_pct:      float
    exit_reason:  str
    year:         int

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class CostModelPct:
    """Percentage-space costs. Symmetric, charged on both sides.

    Every C-series fill is a market order — a close-fill is
    market-on-close, a next-open fill is market-on-open — so unlike the
    swing engines there is no limit-order exemption to model. Both sides
    slip.
    """
    slippage_bps: float = SPY_SLIPPAGE_BPS
    commission_per_side: float = COMMISSION_PER_SIDE

    def buy(self, price: float) -> float:
        return price * (1.0 + self.slippage_bps / 10_000.0) + self.commission_per_side

    def sell(self, price: float) -> float:
        return price * (1.0 - self.slippage_bps / 10_000.0) - self.commission_per_side

    def describe(self) -> dict:
        return {
            "slippage_bps_per_side": self.slippage_bps,
            "commission_per_side": self.commission_per_side,
            "applies_to": "both sides; every C-series fill is a market order",
        }


FREE = CostModelPct(slippage_bps=0.0, commission_per_side=0.0)


def replay_variant(
    bars: pd.DataFrame,
    variant: str | Variant,
    *,
    fill_mode: str = "close",
    costs: Optional[CostModelPct] = None,
    start: Optional[str] = None,
    end: Optional[str] = None,
    time_valve_bars: int = TIME_VALVE_BARS,
) -> list[CTrade]:
    """Replay one variant over `bars` (DatetimeIndex, OHLC columns).

    Windowing is applied to SIGNALS, not to the price series: indicators
    are always computed on the full history so that a trade on the first
    day of the POST-BOOK window has the same SMA200 a real trader would
    have had. Slicing the prices first would give the 2008 window a
    200-day warm-up carved out of 2008 itself.

    One position at a time. A position opened before `end` is walked to
    its natural exit even if that lands past the boundary — truncating it
    would silently convert open trades into wins or losses at an
    arbitrary date.
    """
    v = variant if isinstance(variant, Variant) else get_variant(variant)
    if costs is None:
        costs = CostModelPct()
    if fill_mode not in ("close", "next_open"):
        raise ValueError(f"fill_mode must be 'close' or 'next_open', got {fill_mode!r}")

    idx = bars.index
    closes = pd.to_numeric(bars["close"], errors="coerce").to_numpy(dtype=float)
    opens = pd.to_numeric(bars["open"], errors="coerce").to_numpy(dtype=float)
    lows = pd.to_numeric(bars["low"], errors="coerce").to_numpy(dtype=float)
    highs = pd.to_numeric(bars["high"], errors="coerce").to_numpy(dtype=float)
    n = len(closes)

    ind = Indicators.build(closes)

    lo_i = 0 if start is None else int(np.searchsorted(idx.values,
                                                       np.datetime64(pd.Timestamp(start))))
    hi_i = n if end is None else int(np.searchsorted(idx.values,
                                                     np.datetime64(pd.Timestamp(end)),
                                                     side="right"))

    trades: list[CTrade] = []
    i = max(lo_i, 0)
    while i < min(hi_i, n):
        if not v.entry_fn(ind, i):
            i += 1
            continue

        # ── Entry ────────────────────────────────────────────────────
        if fill_mode == "close":
            entry_index = i
            raw_entry = closes[i]
        else:
            entry_index = i + 1
            if entry_index >= n:
                break                      # signal on the last bar: no fill
            raw_entry = opens[entry_index]
        entry_px = costs.buy(float(raw_entry))
        if not np.isfinite(entry_px) or entry_px <= 0:
            i += 1
            continue

        # ── Walk forward to the exit ─────────────────────────────────
        # The earliest possible exit is the bar AFTER the signal bar: the
        # exit rule is evaluated on a close, and on bar i we have already
        # committed to entering at that same close.
        exit_index, exit_reason, raw_exit = None, "END_OF_DATA", float("nan")
        mae = mfe = 0.0
        for j in range(entry_index, n):
            lo, hi = float(lows[j]), float(highs[j])
            if j == entry_index and fill_mode == "close":
                # We bought at THIS bar's close, so its low may have
                # printed hours before we were in. Only the close is
                # demonstrably ours. (Under next_open we bought at this
                # bar's open and did own its whole range.)
                lo = hi = float(closes[j])
            mae = min(mae, (lo - entry_px) / entry_px * 100.0)
            mfe = max(mfe, (hi - entry_px) / entry_px * 100.0)

            signal_bar = j
            held = signal_bar - i
            if signal_bar > i and v.exit_fn(ind, signal_bar):
                exit_index, exit_reason = signal_bar, "SIGNAL"
                break
            if held >= time_valve_bars:
                exit_index, exit_reason = signal_bar, "TIME_VALVE"
                break

        if exit_index is None:
            exit_index = n - 1
            exit_reason = "END_OF_DATA"

        if fill_mode == "close":
            raw_exit = closes[exit_index]
            exit_fill_index = exit_index
        else:
            exit_fill_index = min(exit_index + 1, n - 1)
            raw_exit = opens[exit_fill_index]
            if exit_fill_index == exit_index:      # no next bar available
                raw_exit = closes[exit_index]
        exit_px = costs.sell(float(raw_exit))

        pct = (exit_px - entry_px) / entry_px * 100.0
        entry_date = str(idx[entry_index].date())
        trades.append(CTrade(
            variant=v.name,
            entry_date=entry_date,
            exit_date=str(idx[exit_fill_index].date()),
            entry_index=entry_index,
            exit_index=exit_fill_index,
            bars_held=exit_fill_index - entry_index,
            entry_price=round(entry_px, 4),
            exit_price=round(exit_px, 4),
            pct_return=round(pct, 4),
            mae_pct=round(mae, 3),
            mfe_pct=round(mfe, 3),
            exit_reason=exit_reason,
            year=int(entry_date[:4]),
        ))
        i = max(exit_fill_index, exit_index) + 1
    return trades


# ─────────────────────────────────────────────────────────────────────
# Metrics
# ─────────────────────────────────────────────────────────────────────

def compute_c_stats(trades: list[CTrade], *, total_bars: Optional[int] = None) -> dict:
    """Per-trade statistics in percent.

    `profit_factor` is gross gain / gross loss, the same definition used
    everywhere else in the project so the numbers stay comparable.
    """
    if not trades:
        return {"n_trades": 0}

    r = np.array([t.pct_return for t in trades], dtype=float)
    wins, losses = r[r > 0], r[r <= 0]
    gross_win = float(wins.sum()) if len(wins) else 0.0
    gross_loss = float(-losses.sum()) if len(losses) else 0.0
    days_in = int(sum(t.bars_held for t in trades))

    return {
        "n_trades":      len(trades),
        "win_rate":      round(float(len(wins) / len(r)), 4),
        "avg_pct":       round(float(r.mean()), 4),
        "median_pct":    round(float(np.median(r)), 4),
        "avg_win_pct":   round(float(wins.mean()), 4) if len(wins) else 0.0,
        "avg_loss_pct":  round(float(losses.mean()), 4) if len(losses) else 0.0,
        "worst_trade_pct": round(float(r.min()), 4),
        "best_trade_pct":  round(float(r.max()), 4),
        "profit_factor": round(gross_win / gross_loss, 3) if gross_loss > 0 else float("inf"),
        "total_pct":     round(float(r.sum()), 2),
        "avg_hold_days": round(float(np.mean([t.bars_held for t in trades])), 2),
        "days_in_market": days_in,
        "pct_days_in_market": (round(days_in / total_bars * 100, 2)
                               if total_bars else None),
        "t_stat": (round(float(r.mean() / (r.std(ddof=1) / np.sqrt(len(r)))), 3)
                   if len(r) > 1 and r.std(ddof=1) > 0 else 0.0),
        "exit_breakdown": {
            k: int(sum(1 for t in trades if t.exit_reason == k))
            for k in sorted({t.exit_reason for t in trades})
        },
    }


def equity_curve_stats(trades: list[CTrade], *, allocation: float = 1.0) -> dict:
    """Compounded equity curve and its max drawdown.

    MANDATORY given the no-stop design. `allocation` = 1.0 is the book's
    100%-of-account convention; 0.1 gives the 10% overlay the spec also
    asks for. Trades are ordered by EXIT date, the order in which P&L is
    actually realised.
    """
    if not trades:
        return {"n_trades": 0}
    ordered = sorted(trades, key=lambda t: t.exit_date)
    eq = [1.0]
    for t in ordered:
        eq.append(eq[-1] * (1.0 + allocation * t.pct_return / 100.0))
    arr = np.array(eq)
    peak = np.maximum.accumulate(arr)
    dd = (arr - peak) / peak
    return {
        "allocation":       allocation,
        "final_multiple":   round(float(arr[-1]), 4),
        "total_return_pct": round(float(arr[-1] - 1.0) * 100, 2),
        "max_drawdown_pct": round(float(-dd.min()) * 100, 2),
    }


def buy_and_hold_stats(bars: pd.DataFrame, start: str, end: Optional[str] = None) -> dict:
    """SPY buy-and-hold over the same window — the gate's drawdown yardstick.

    A no-stop dip-buyer that drawdowns WORSE than simply holding SPY has
    negative value, which is precisely why the gate compares against this.
    """
    idx = bars.index
    lo = int(np.searchsorted(idx.values, np.datetime64(pd.Timestamp(start))))
    hi = (len(idx) if end is None
          else int(np.searchsorted(idx.values, np.datetime64(pd.Timestamp(end)),
                                   side="right")))
    c = pd.to_numeric(bars["close"], errors="coerce").to_numpy(dtype=float)[lo:hi]
    if len(c) < 2:
        return {}
    peak = np.maximum.accumulate(c)
    dd = (c - peak) / peak
    years = len(c) / 252.0
    return {
        "start": str(idx[lo].date()),
        "end": str(idx[min(hi, len(idx)) - 1].date()),
        "bars": len(c),
        "total_return_pct": round(float(c[-1] / c[0] - 1.0) * 100, 2),
        "cagr_pct": round((float(c[-1] / c[0]) ** (1 / years) - 1.0) * 100, 2) if years > 0 else None,
        "max_drawdown_pct": round(float(-dd.min()) * 100, 2),
    }


def worst_trades(trades: list[CTrade], k: int = 10) -> list[dict]:
    """The k worst trades with their max adverse excursion.

    Mandatory in the spec because a no-stop strategy's risk lives entirely
    in its tail: the average trade says nothing about the one that went
    -30% before recovering, or didn't recover.
    """
    return [
        {"entry": t.entry_date, "exit": t.exit_date, "pct": t.pct_return,
         "mae_pct": t.mae_pct, "bars": t.bars_held, "reason": t.exit_reason}
        for t in sorted(trades, key=lambda t: t.pct_return)[:k]
    ]


def window_slice(trades: list[CTrade], start: str, end: Optional[str] = None) -> list[CTrade]:
    """Trades whose ENTRY falls inside [start, end]."""
    return [t for t in trades
            if t.entry_date >= start and (end is None or t.entry_date <= end)]


def split_by_date(trades: list[CTrade], frac: float = 0.70) -> tuple[list, list, str]:
    """Split by DATE at `frac` of the span — same rule as the H-series."""
    if not trades:
        return [], [], "n/a"
    dates = sorted({t.entry_date for t in trades})
    first, last = pd.Timestamp(dates[0]), pd.Timestamp(dates[-1])
    cut = str((first + (last - first) * frac).date())
    return ([t for t in trades if t.entry_date <= cut],
            [t for t in trades if t.entry_date > cut], cut)


def trade_overlap(a: list[CTrade], b: list[CTrade]) -> dict:
    """Jaccard overlap of entry dates between two variants.

    The spec requires this: C1-C3 are siblings of one idea, and a pass by
    one is not "the RSI-2 concept confirmed three times". Showing the
    correlation keeps that honest.
    """
    da, db = {t.entry_date for t in a}, {t.entry_date for t in b}
    inter = da & db
    union = da | db
    return {
        "a_trades": len(da), "b_trades": len(db),
        "shared_entry_dates": len(inter),
        "jaccard": round(len(inter) / len(union), 4) if union else 0.0,
        "pct_of_a_shared": round(len(inter) / len(da), 4) if da else 0.0,
        "pct_of_b_shared": round(len(inter) / len(db), 4) if db else 0.0,
    }


# ─────────────────────────────────────────────────────────────────────
# The pre-registered gate (connors_rsi2_spec.md), judged on L2
# ─────────────────────────────────────────────────────────────────────
#
# Copied from the spec, written before any post-2007 result existed.
C_GATE = {
    "min_profit_factor":  1.3,
    "min_avg_pct":        0.20,   # % per trade, after costs
    "min_trades":         100,    # in POST-BOOK
    "max_dd_vs_buy_hold": 1.0,    # strategy DD must be <= buy-and-hold DD
    "min_l3_profit_factor": 1.1,  # below this = EXECUTION-FRAGILE
}

# Registered stress windows. "2008-09" is read as the 2008-2009 crisis
# (the neighbouring entries use explicit month ranges, so a bare pair of
# years is a year range); the interpretation is stated in the report so a
# reader can disagree with it explicitly rather than silently.
STRESS_WINDOWS = {
    "2008-09 crisis":  ("2008-01-01", "2009-12-31"),
    "2020 COVID crash": ("2020-02-01", "2020-03-31"),
    "2022 bear":       ("2022-01-01", "2022-12-31"),
}


def evaluate_c_gate(
    l2_stats: dict,
    l2_equity: dict,
    train_stats: dict,
    test_stats: dict,
    buy_hold: dict,
    l3_stats: Optional[dict] = None,
    gate: Optional[dict] = None,
) -> dict:
    """Apply the spec's gate to L2 (POST-BOOK, close-fill, costs on).

    Verdicts:
      FAIL      avg gain/trade <= 0 after costs
      MARGINAL  positive but misses any bar — "not tradable; do not tune;
                record and close"
      PASS      every bar met

    No survivorship caveat downgrades this one: SPY is a single
    continuously-listed ETF, so a PASS here is a real PASS on the data —
    the first fully-trustable verdict available to this project.
    """
    g = dict(C_GATE)
    if gate:
        g.update(gate)

    n = l2_stats.get("n_trades", 0)
    avg = l2_stats.get("avg_pct", 0.0)
    pf = l2_stats.get("profit_factor", 0.0)
    dd = l2_equity.get("max_drawdown_pct", float("inf"))
    bh_dd = buy_hold.get("max_drawdown_pct", 0.0)

    tr = train_stats.get("avg_pct") if train_stats.get("n_trades") else None
    te = test_stats.get("avg_pct") if test_stats.get("n_trades") else None
    sign_holds = (tr is not None and te is not None
                  and ((tr > 0 and te > 0) or (tr < 0 and te < 0)))

    l3_pf = (l3_stats or {}).get("profit_factor", 0.0)
    fragile = l3_pf < g["min_l3_profit_factor"]

    checks = [
        ("profit_factor", pf >= g["min_profit_factor"],
         f"profit factor {pf:.3f} (need >= {g['min_profit_factor']})"),
        ("avg_pct", avg >= g["min_avg_pct"],
         f"avg {avg:+.3f}%/trade after costs (need >= +{g['min_avg_pct']:.2f}%)"),
        ("n_trades", n >= g["min_trades"],
         f"{n} POST-BOOK trades (need >= {g['min_trades']})"),
        ("split_sign", sign_holds,
         (f"train {tr:+.3f}% vs test {te:+.3f}%"
          if (tr is not None and te is not None) else "one split empty")),
        ("drawdown_vs_buy_hold", dd <= bh_dd,
         f"max DD {dd:.1f}% vs SPY buy-and-hold {bh_dd:.1f}%"),
        ("not_execution_fragile", not fragile,
         f"L3 (next-open) profit factor {l3_pf:.3f} "
         f"(need >= {g['min_l3_profit_factor']})"),
    ]
    passed = all(ok for _, ok, _ in checks)

    if avg <= 0:
        decision = "FAIL"
    elif passed:
        decision = "PASS"
    else:
        decision = "MARGINAL"

    caveats = []
    if fragile and avg > 0:
        caveats.append(
            f"EXECUTION-FRAGILE: the edge depends on the close-fill "
            f"convention — at next-open the profit factor is {l3_pf:.3f}."
        )
    return {
        "decision": decision,
        "gate": g,
        "checks": [{"name": nm, "pass": ok, "detail": d} for nm, ok, d in checks],
        "reasons": [f"{'PASS' if ok else 'FAIL'}: {d}" for _, ok, d in checks],
        "caveats": caveats,
        "train_avg_pct": tr,
        "test_avg_pct": te,
    }


def confirm_no_lookahead(
    bars: pd.DataFrame,
    variant: str,
    *,
    fill_mode: str = "close",
    tail_bars: int = 40,
) -> dict:
    """VERIFY the close-fill convention does not peek, don't just argue it.

    Replay, then replay again with `tail_bars` of a fabricated 3x rally
    appended. Every trade that closed before the original data ended must
    be byte-identical. If RSI(2), either SMA, or the fill logic consulted
    a future bar, that appended future would move them.

    This is the check the spec's C-0c note requires before close-fill may
    be used at all.
    """
    base = replay_variant(bars, variant, fill_mode=fill_mode, costs=FREE)

    tail = pd.concat([bars.iloc[-1:]] * tail_bars)
    tail.index = pd.date_range(bars.index[-1] + pd.Timedelta(days=1),
                               periods=tail_bars, freq="B")
    tail = tail.copy()
    for col in ("open", "high", "low", "close"):
        if col in tail.columns:
            tail[col] = tail[col] * 3.0
    ext = replay_variant(pd.concat([bars, tail]), variant,
                         fill_mode=fill_mode, costs=FREE)

    cutoff = len(bars) - 1
    mismatches, compared = [], 0
    for a, b in zip(base, ext):
        if a.exit_index < cutoff:
            compared += 1
            if (a.pct_return != b.pct_return or a.exit_reason != b.exit_reason
                    or a.entry_price != b.entry_price):
                mismatches.append(
                    f"{a.entry_date}: {a.exit_reason}/{a.pct_return} "
                    f"-> {b.exit_reason}/{b.pct_return}")
    return {
        "variant": variant,
        "fill_mode": fill_mode,
        "passed": not mismatches,
        "trades_compared": compared,
        "mismatches": mismatches[:10],
        "method": (f"appended {tail_bars} bars of a fabricated 3x rally; every "
                   f"trade closing before the original data ended must be unchanged"),
        "note": None if compared else "INCONCLUSIVE — no closed trades to compare",
    }
