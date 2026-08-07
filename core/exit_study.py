"""core/exit_study.py — Addendum 2 metrics and the replacement gate.

The addendum's metric hierarchy exists to defeat one specific way of being
fooled: the book compares avg %/trade across exits with DIFFERENT hold
lengths, which mechanically rewards whichever exit holds longest. A rule
that simply stays in the market longer collects more drift and looks
better without containing any more information.

So the primary metrics are:

  1. RETURN PER TRADING DAY HELD — per-trade return / days held, pooled.
     Neutralises the hold-length confound directly.
  2. PORTFOLIO MAR in the registered P-capped fixed-slots sim — because a
     slower exit occupies a slot longer, and slot occupancy is the real
     constraint on a 3-slot account. An exit can win per-day and still
     lose here by crowding out other trades.

Everything else is secondary and reported for context.

THE SPLIT DATE IS SHARED. Each variant takes a slightly different set of
entries (measured in Step 1), so letting each derive its own 70/30
boundary from its own trades would give the variants different test
windows. The cut is computed once from the POST-BOOK CALENDAR and applied
to all five.
"""
from __future__ import annotations

import logging
from typing import Optional, Sequence

import numpy as np
import pandas as pd

from core.c_series_backtest import CTrade, compute_c_stats

log = logging.getLogger("exit_study")

# Registered in c2_exit_study_addendum.md, before any challenger ran.
GATE_RELATIVE_IMPROVEMENT = 1.10      # per-day AND MAR, vs E0
GATE_MIN_INSTRUMENTS_BEATEN = 8       # of 13
GATE_MIN_NEXT_OPEN_PF = 1.1
SPLIT_FRACTION = 0.70


def window_split_date(bars_by_ticker: dict[str, pd.DataFrame],
                      start: str, frac: float = SPLIT_FRACTION) -> str:
    """The 70/30 boundary, computed from the CALENDAR, shared by all variants.

    Deriving it per-variant from that variant's own trades would hand each
    exit a different out-of-sample window — and the one with the luckiest
    boundary would look most robust.
    """
    lo = pd.Timestamp(start)
    hi = max(b.index[-1] for b in bars_by_ticker.values())
    return str((lo + (hi - lo) * frac).date())


def split_on(trades: Sequence[CTrade], cut: str) -> tuple[list, list]:
    return ([t for t in trades if t.entry_date <= cut],
            [t for t in trades if t.entry_date > cut])


def per_day_stats(trades: Sequence[CTrade]) -> dict:
    """PRIMARY METRIC: mean of (per-trade return / days held).

    Trades with zero held days are excluded rather than floored to 1: a
    zero-day trade is an END_OF_DATA artefact at the series edge, and
    dividing by a fabricated 1 would credit it with its entire return
    earned in a single day. The count is reported so the exclusion is
    visible.
    """
    vals, dropped = [], 0
    for t in trades:
        if t.bars_held >= 1:
            vals.append(t.pct_return / t.bars_held)
        else:
            dropped += 1
    if not vals:
        return {"n": 0, "per_day_pct": None, "dropped_zero_day": dropped}
    a = np.array(vals, dtype=float)
    return {
        "n": len(a),
        "per_day_pct": round(float(a.mean()), 5),
        "per_day_median": round(float(np.median(a)), 5),
        "per_day_t": (round(float(a.mean() / (a.std(ddof=1) / np.sqrt(len(a)))), 3)
                      if len(a) > 1 and a.std(ddof=1) > 0 else 0.0),
        "dropped_zero_day": dropped,
    }


def clustered_t_stat(trades: Sequence[CTrade]) -> dict:
    """t-stat on day-averaged returns.

    Same-day signals across correlated ETFs are one market event, not N
    independent draws. Addendum 1 measured the effect: pooled t fell from
    +6.44 to +3.81 once clustered by entry day. Reported per variant so a
    challenger cannot win on an inflated significance figure.
    """
    if not trades:
        return {"n_days": 0, "clustered_t": None}
    df = pd.DataFrame({"d": [t.entry_date for t in trades],
                       "r": [t.pct_return for t in trades]})
    day = df.groupby("d")["r"].mean()
    if len(day) < 2 or day.std(ddof=1) == 0:
        return {"n_days": int(len(day)), "clustered_t": None}
    t = float(day.mean() / (day.std(ddof=1) / np.sqrt(len(day))))
    return {"n_days": int(len(day)), "clustered_t": round(t, 3),
            "day_mean_pct": round(float(day.mean()), 4)}


def valve_firings(trades: Sequence[CTrade]) -> int:
    return sum(1 for t in trades if t.exit_reason == "TIME_VALVE")


def variant_summary(trades: Sequence[CTrade], *,
                    total_bars: Optional[int] = None) -> dict:
    """Primary + secondary metrics for one variant on one window."""
    s = compute_c_stats(trades, total_bars=total_bars)
    if not s.get("n_trades"):
        return {"n_trades": 0}
    s.update(per_day_stats(trades))
    s.update(clustered_t_stat(trades))
    s["valve_firings"] = valve_firings(trades)
    s["end_of_data_exits"] = sum(1 for t in trades
                                 if t.exit_reason == "END_OF_DATA")
    return s


def matched_entry_subset(taken: dict[str, dict[str, set]]) -> dict[str, set]:
    """Entry dates taken by EVERY variant, per instrument.

    The labelled diagnostic the user authorised: restricted to these
    entries, all variants trade the same opportunities, so any remaining
    difference is the exit and nothing else. It does NOT feed the gate —
    the gate judges the systems as they would actually be run, entry
    self-selection included.
    """
    tickers = set().union(*(set(d) for d in taken.values())) if taken else set()
    out = {}
    for t in tickers:
        sets = [d.get(t, set()) for d in taken.values()]
        out[t] = set.intersection(*sets) if sets else set()
    return out


def restrict_to(trades: Sequence[CTrade], keep: set) -> list[CTrade]:
    return [t for t in trades if t.entry_date in keep]


def aggregate_per_day(trades: Sequence[CTrade]) -> Optional[float]:
    """Total return / total days held — the RATIO OF SUMS.

    Distinct from the registered primary metric, which is the MEAN OF
    RATIOS (per-trade return / that trade's days). The two differ by
    Jensen's inequality and neither is wrong; they answer different
    questions. The registered metric weights every trade equally, which is
    what the gate wants. This one is the portfolio-level rate, and it is
    the only version that makes the decomposition below exact:

        mean_return = aggregate_rate x mean_hold

    holds identically, because sum(h)/n IS mean_hold. The mean-of-ratios
    version does not satisfy that identity, and using it here produced
    decomposition shares of 2446% and -5680% — a sign the arithmetic was
    describing nothing.
    """
    tot_r = sum(t.pct_return for t in trades)
    tot_h = sum(t.bars_held for t in trades)
    return round(tot_r / tot_h, 5) if tot_h > 0 else None


def hold_length_decomposition(e0: Sequence[CTrade],
                              challenger: Sequence[CTrade]) -> dict:
    """How much of the avg %/trade gap is just holding longer?

    Exact additive split, using the aggregate rate so that
    `mean_return = rate x mean_hold` holds identically for both variants:

        gap = rate_B * hold_B - rate_A * hold_A
            = rate_A * (hold_B - hold_A)      <- exposure effect
            + (rate_B - rate_A) * hold_B      <- rate effect

    The first term is what the challenger gains merely by staying in the
    market longer at the incumbent's own rate. This is the arithmetic
    behind the addendum's warning that longer-hold exits "will show higher
    avg %/trade almost automatically".
    """
    a, b = compute_c_stats(list(e0)), compute_c_stats(list(challenger))
    if not a.get("n_trades") or not b.get("n_trades"):
        return {}
    rate_a, rate_b = aggregate_per_day(e0), aggregate_per_day(challenger)
    hold_a, hold_b = a["avg_hold_days"], b["avg_hold_days"]
    if rate_a is None or rate_b is None:
        return {}

    exposure = rate_a * (hold_b - hold_a)
    rate_eff = (rate_b - rate_a) * hold_b
    actual_gap = exposure + rate_eff
    return {
        "e0_avg_pct": a["avg_pct"], "challenger_avg_pct": b["avg_pct"],
        "e0_aggregate_rate": rate_a, "challenger_aggregate_rate": rate_b,
        "e0_hold": hold_a, "challenger_hold": hold_b,
        "actual_gap_pct": round(actual_gap, 4),
        "exposure_effect_pct": round(exposure, 4),
        "rate_effect_pct": round(rate_eff, 4),
        "pct_of_gap_from_hold": (round(exposure / actual_gap * 100, 1)
                                 if abs(actual_gap) > 1e-9 else None),
        # Sanity: the identity must reconstruct the observed means.
        "identity_residual": round(
            (rate_b * hold_b - rate_a * hold_a) - (b["avg_pct"] - a["avg_pct"]),
            6),
    }


def evaluate_replacement_gate(
    exit_id: str,
    *,
    e0: dict,
    challenger: dict,
    gate_relative: float = GATE_RELATIVE_IMPROVEMENT,
    min_instruments: int = GATE_MIN_INSTRUMENTS_BEATEN,
    min_next_open_pf: float = GATE_MIN_NEXT_OPEN_PF,
) -> dict:
    """The registered replacement gate. ALL bars must hold to dethrone E0.

    `e0` and `challenger` are dicts carrying, for one variant:
        per_day, mar, train_per_day, test_per_day, train_mar, test_mar,
        next_open_per_day, next_open_pf, instruments_per_day (per ticker)
    """
    # A relative epsilon so binary floating point cannot decide the gate:
    # 0.20 * 1.10 evaluates to 0.22000000000000003, which would reject a
    # challenger sitting exactly on the registered bar. This is numerical
    # hygiene, not a threshold move — every real margin in this study is
    # orders of magnitude larger than 1e-12.
    eps = 1e-12

    def ratio_ok(c, b):
        return (c is not None and b is not None and b > 0
                and c >= b * gate_relative * (1.0 - eps))

    beaten = [t for t, v in challenger["instruments_per_day"].items()
              if v is not None
              and e0["instruments_per_day"].get(t) is not None
              and v > e0["instruments_per_day"][t]]

    checks = [
        ("per_day_x1.10", ratio_ok(challenger["per_day"], e0["per_day"]),
         f"per-day {challenger['per_day']:+.5f}% vs E0 {e0['per_day']:+.5f}% "
         f"(need >= {e0['per_day'] * gate_relative:+.5f}%)"),
        ("portfolio_mar_x1.10", ratio_ok(challenger["mar"], e0["mar"]),
         f"MAR {challenger['mar']} vs E0 {e0['mar']} "
         f"(need >= {round(e0['mar'] * gate_relative, 4) if e0['mar'] else None})"),
        ("train_per_day_x1.10",
         ratio_ok(challenger["train_per_day"], e0["train_per_day"]),
         f"train per-day {challenger['train_per_day']} vs E0 {e0['train_per_day']}"),
        ("test_per_day_x1.10",
         ratio_ok(challenger["test_per_day"], e0["test_per_day"]),
         f"test per-day {challenger['test_per_day']} vs E0 {e0['test_per_day']}"),
        ("train_mar_x1.10", ratio_ok(challenger["train_mar"], e0["train_mar"]),
         f"train MAR {challenger['train_mar']} vs E0 {e0['train_mar']}"),
        ("test_mar_x1.10", ratio_ok(challenger["test_mar"], e0["test_mar"]),
         f"test MAR {challenger['test_mar']} vs E0 {e0['test_mar']}"),
        ("beats_e0_on_8_of_13", len(beaten) >= min_instruments,
         f"beats E0 on per-day on {len(beaten)}/"
         f"{len(challenger['instruments_per_day'])} instruments "
         f"(need >= {min_instruments})"),
        ("next_open_pf", (challenger["next_open_pf"] or 0) >= min_next_open_pf,
         f"next-open PF {challenger['next_open_pf']} (need >= {min_next_open_pf})"),
        ("next_open_per_day_ge_e0",
         (challenger["next_open_per_day"] is not None
          and e0["next_open_per_day"] is not None
          and challenger["next_open_per_day"] >= e0["next_open_per_day"]),
         f"next-open per-day {challenger['next_open_per_day']} vs E0 "
         f"{e0['next_open_per_day']} (need >=, no 1.10x on this bar)"),
    ]
    passed = all(ok for _, ok, _ in checks)
    return {
        "exit_id": exit_id,
        "verdict": "REPLACES E0" if passed else "E0 STANDS",
        "checks": [{"name": n, "pass": ok, "detail": d} for n, ok, d in checks],
        "n_failed": sum(1 for _, ok, _ in checks if not ok),
        "instruments_beaten": sorted(beaten),
        "n_instruments_beaten": len(beaten),
    }


def pick_winner(gate_results: dict[str, dict],
                mar: dict[str, Optional[float]],
                hold: dict[str, Optional[float]]) -> Optional[str]:
    """Registered tiebreak: highest portfolio MAR; ties go to shorter hold."""
    winners = [e for e, g in gate_results.items() if g["verdict"] == "REPLACES E0"]
    if not winners:
        return None
    return sorted(winners,
                  key=lambda e: (-(mar.get(e) or 0.0), hold.get(e) or 1e9))[0]
