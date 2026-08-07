"""tests/test_exit_study.py — Addendum 2 metric and gate tests.

The metrics here decide whether a validated, live-traded exit gets
replaced, so they are tested on hand-built trades where the right answer
is arithmetic. The decomposition in particular earned its tests: the first
version multiplied a mean-of-ratios by a mean hold and reported that hold
length explained 2446% of a gap, which is not a number about anything.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from core.c_series_backtest import CTrade
from core.exit_study import (
    GATE_MIN_INSTRUMENTS_BEATEN, GATE_MIN_NEXT_OPEN_PF,
    GATE_RELATIVE_IMPROVEMENT, aggregate_per_day, clustered_t_stat,
    evaluate_replacement_gate, hold_length_decomposition, matched_entry_subset,
    per_day_stats, pick_winner, restrict_to, split_on, valve_firings,
    variant_summary, window_split_date,
)


def tr(pct, held, entry="2010-01-04", reason="SIGNAL"):
    return CTrade(variant="C2", entry_date=entry, exit_date=entry,
                  entry_index=0, exit_index=held, bars_held=held,
                  entry_price=100.0, exit_price=100.0 * (1 + pct / 100),
                  pct_return=pct, mae_pct=0.0, mfe_pct=0.0,
                  exit_reason=reason, year=2010)


# ─────────────────────────────────────────────────────────────────────
# The registered constants
# ─────────────────────────────────────────────────────────────────────

class TestRegistration:
    def test_gate_constants(self):
        assert GATE_RELATIVE_IMPROVEMENT == 1.10
        assert GATE_MIN_INSTRUMENTS_BEATEN == 8
        assert GATE_MIN_NEXT_OPEN_PF == 1.1


# ─────────────────────────────────────────────────────────────────────
# PRIMARY metric: return per day held
# ─────────────────────────────────────────────────────────────────────

class TestPerDay:
    def test_simple_average_of_ratios(self):
        # +4% over 4 days and +1% over 1 day are BOTH 1%/day.
        s = per_day_stats([tr(4.0, 4), tr(1.0, 1)])
        assert s["per_day_pct"] == pytest.approx(1.0)
        assert s["n"] == 2

    def test_it_is_the_mean_of_ratios_not_ratio_of_means(self):
        # mean(r/h) = (10/1 + 1/10)/2 = 5.05; mean(r)/mean(h) = 5.5/5.5 = 1.
        s = per_day_stats([tr(10.0, 1), tr(1.0, 10)])
        assert s["per_day_pct"] == pytest.approx(5.05)
        assert aggregate_per_day([tr(10.0, 1), tr(1.0, 10)]) == pytest.approx(1.0)

    def test_zero_day_trades_are_excluded_not_floored(self):
        # Flooring to 1 day would credit a whole return to a single session.
        s = per_day_stats([tr(4.0, 4), tr(9.9, 0)])
        assert s["n"] == 1
        assert s["dropped_zero_day"] == 1
        assert s["per_day_pct"] == pytest.approx(1.0)

    def test_empty_is_none_not_zero(self):
        s = per_day_stats([])
        assert s["n"] == 0 and s["per_day_pct"] is None

    def test_longer_holds_dilute_the_rate(self):
        fast = per_day_stats([tr(2.0, 2)])["per_day_pct"]
        slow = per_day_stats([tr(2.5, 5)])["per_day_pct"]
        assert fast > slow          # bigger total, worse rate

    def test_aggregate_is_none_when_no_days(self):
        assert aggregate_per_day([]) is None


# ─────────────────────────────────────────────────────────────────────
# The hold-length decomposition
# ─────────────────────────────────────────────────────────────────────

class TestDecomposition:
    def test_split_is_exact(self):
        a = [tr(1.0, 2), tr(2.0, 3)]
        b = [tr(3.0, 6), tr(1.0, 4)]
        d = hold_length_decomposition(a, b)
        assert d["exposure_effect_pct"] + d["rate_effect_pct"] == pytest.approx(
            d["actual_gap_pct"], abs=1e-6)

    def test_identity_reconstructs_the_observed_means(self):
        # The bug this pins: rate x hold must equal mean return, or the
        # whole decomposition is describing nothing.
        a = [tr(1.0, 2), tr(2.0, 3)]
        b = [tr(3.0, 6), tr(1.0, 4)]
        d = hold_length_decomposition(a, b)
        assert abs(d["identity_residual"]) < 1e-3

    def test_pure_exposure_gap_is_fully_attributed_to_hold(self):
        # Same rate (1%/day), different holds -> 100% exposure.
        a = [tr(2.0, 2)]
        b = [tr(6.0, 6)]
        d = hold_length_decomposition(a, b)
        assert d["pct_of_gap_from_hold"] == pytest.approx(100.0, abs=0.5)
        assert d["rate_effect_pct"] == pytest.approx(0.0, abs=1e-6)

    def test_pure_rate_gap_is_fully_attributed_to_rate(self):
        # Same hold, different rate -> 0% exposure.
        d = hold_length_decomposition([tr(2.0, 2)], [tr(4.0, 2)])
        assert d["exposure_effect_pct"] == pytest.approx(0.0, abs=1e-6)
        assert d["pct_of_gap_from_hold"] == pytest.approx(0.0, abs=0.5)

    def test_share_above_100pct_means_a_negative_rate_effect(self):
        # Longer hold, worse rate: exposure must over-explain the gap.
        a = [tr(2.0, 2)]                 # 1.0%/day
        b = [tr(2.4, 4)]                 # 0.6%/day, but bigger total
        d = hold_length_decomposition(a, b)
        assert d["rate_effect_pct"] < 0
        assert d["pct_of_gap_from_hold"] > 100

    def test_empty_inputs_return_empty(self):
        assert hold_length_decomposition([], [tr(1.0, 1)]) == {}


# ─────────────────────────────────────────────────────────────────────
# Clustered significance
# ─────────────────────────────────────────────────────────────────────

class TestClustering:
    def test_same_day_trades_collapse_to_one_observation(self):
        same = [tr(1.0, 2, "2010-01-04") for _ in range(20)]
        assert clustered_t_stat(same)["n_days"] == 1

    def test_clustering_reduces_the_t_stat(self):
        # 10 days x 4 correlated same-day trades.
        trades = []
        for d in range(10):
            for _ in range(4):
                trades.append(tr(1.0 + 0.1 * d, 2, f"2010-01-{d + 4:02d}"))
        naive = np.array([t.pct_return for t in trades])
        naive_t = naive.mean() / (naive.std(ddof=1) / np.sqrt(len(naive)))
        assert clustered_t_stat(trades)["clustered_t"] < naive_t

    def test_empty_is_handled(self):
        assert clustered_t_stat([])["clustered_t"] is None


# ─────────────────────────────────────────────────────────────────────
# Windowing and matching
# ─────────────────────────────────────────────────────────────────────

class TestWindows:
    def test_split_date_is_shared_and_calendar_based(self):
        idx = pd.date_range("2008-01-01", "2018-01-01", freq="B")
        bars = {"A": pd.DataFrame({"close": np.ones(len(idx))}, index=idx)}
        cut = window_split_date(bars, "2008-01-01", 0.70)
        assert "2015" in cut          # 70% of a 10-year span

    def test_split_on_partitions_without_loss(self):
        t = [tr(1.0, 1, "2010-01-04"), tr(1.0, 1, "2020-01-04")]
        a, b = split_on(t, "2015-01-01")
        assert len(a) == 1 and len(b) == 1

    def test_matched_subset_is_the_intersection(self):
        taken = {
            "E0": {"XLK": {"a", "b", "c"}},
            "E1": {"XLK": {"b", "c", "d"}},
            "E2": {"XLK": {"c", "d", "e"}},
        }
        assert matched_entry_subset(taken) == {"XLK": {"c"}}

    def test_matched_subset_handles_a_missing_instrument(self):
        taken = {"E0": {"XLK": {"a"}}, "E1": {}}
        assert matched_entry_subset(taken)["XLK"] == set()

    def test_restrict_to_filters_by_entry_date(self):
        t = [tr(1.0, 1, "2010-01-04"), tr(1.0, 1, "2011-01-04")]
        assert len(restrict_to(t, {"2010-01-04"})) == 1


class TestValve:
    def test_counts_only_valve_exits(self):
        t = [tr(1.0, 25, reason="TIME_VALVE"), tr(1.0, 3), tr(1.0, 25,
                                                             reason="TIME_VALVE")]
        assert valve_firings(t) == 2

    def test_summary_surfaces_valve_and_end_of_data(self):
        s = variant_summary([tr(1.0, 25, reason="TIME_VALVE"),
                             tr(1.0, 3, reason="END_OF_DATA")])
        assert s["valve_firings"] == 1 and s["end_of_data_exits"] == 1


# ─────────────────────────────────────────────────────────────────────
# The replacement gate
# ─────────────────────────────────────────────────────────────────────

def pack(per_day, mar, *, train_pd=None, test_pd=None, train_mar=None,
         test_mar=None, no_pd=None, no_pf=2.0, beat=13):
    inst = {f"T{i}": (per_day if i < beat else -1.0) for i in range(13)}
    return {"per_day": per_day, "mar": mar,
            "train_per_day": train_pd if train_pd is not None else per_day,
            "test_per_day": test_pd if test_pd is not None else per_day,
            "train_mar": train_mar if train_mar is not None else mar,
            "test_mar": test_mar if test_mar is not None else mar,
            "next_open_per_day": no_pd if no_pd is not None else per_day,
            "next_open_pf": no_pf, "instruments_per_day": inst}


class TestGate:
    def _e0(self):
        return pack(1.0, 0.20, beat=0)

    def test_a_clear_winner_replaces_e0(self):
        g = evaluate_replacement_gate("E1", e0=self._e0(),
                                      challenger=pack(2.0, 0.50, beat=13))
        assert g["verdict"] == "REPLACES E0"
        assert g["n_failed"] == 0

    def test_marginal_improvement_is_not_enough(self):
        # +5% beats E0 but misses the registered +10% bar. "Slightly better
        # in the pooled table" is exactly what the gate exists to reject.
        g = evaluate_replacement_gate("E1", e0=self._e0(),
                                      challenger=pack(1.05, 0.21, beat=13))
        assert g["verdict"] == "E0 STANDS"

    def test_exactly_ten_percent_passes(self):
        g = evaluate_replacement_gate("E1", e0=self._e0(),
                                      challenger=pack(1.10, 0.22, beat=13))
        assert g["verdict"] == "REPLACES E0"

    def test_failing_one_split_is_fatal(self):
        g = evaluate_replacement_gate(
            "E1", e0=self._e0(),
            challenger=pack(2.0, 0.50, test_pd=0.9, beat=13))
        assert g["verdict"] == "E0 STANDS"
        assert any(c["name"] == "test_per_day_x1.10" and not c["pass"]
                   for c in g["checks"])

    def test_seven_instruments_is_not_eight(self):
        g = evaluate_replacement_gate("E1", e0=self._e0(),
                                      challenger=pack(2.0, 0.50, beat=7))
        assert g["verdict"] == "E0 STANDS"
        assert g["n_instruments_beaten"] == 7

    def test_execution_fragility_is_fatal(self):
        g = evaluate_replacement_gate("E1", e0=self._e0(),
                                      challenger=pack(2.0, 0.50, no_pf=1.0,
                                                      beat=13))
        assert g["verdict"] == "E0 STANDS"

    def test_next_open_per_day_needs_only_parity_not_ten_percent(self):
        # Registered asymmetry: the fragility bar is ">= E0", not ">= 1.1x".
        g = evaluate_replacement_gate(
            "E1", e0=self._e0(),
            challenger=pack(2.0, 0.50, no_pd=1.0, beat=13))
        assert g["verdict"] == "REPLACES E0"

    def test_negative_e0_baseline_cannot_be_beaten_by_ratio(self):
        # Guard against the sign trap: with a negative baseline, x1.10 would
        # mean "more negative". The gate requires a positive baseline.
        g = evaluate_replacement_gate("E1", e0=pack(-1.0, -0.2, beat=0),
                                      challenger=pack(0.5, 0.3, beat=13))
        assert g["verdict"] == "E0 STANDS"


class TestTiebreak:
    def test_no_winner_returns_none(self):
        assert pick_winner({"E1": {"verdict": "E0 STANDS"}}, {}, {}) is None

    def test_higher_mar_wins(self):
        g = {"E1": {"verdict": "REPLACES E0"}, "E2": {"verdict": "REPLACES E0"}}
        assert pick_winner(g, {"E1": 0.3, "E2": 0.5}, {"E1": 4.0, "E2": 6.0}) == "E2"

    def test_mar_tie_goes_to_the_shorter_hold(self):
        g = {"E1": {"verdict": "REPLACES E0"}, "E2": {"verdict": "REPLACES E0"}}
        assert pick_winner(g, {"E1": 0.4, "E2": 0.4}, {"E1": 3.0, "E2": 6.0}) == "E1"
