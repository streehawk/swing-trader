"""tests/test_c2_exits.py — Addendum 2 acceptance tests.

Each registered exit is unit-tested on hand-built series where the correct
answer is arithmetic, and E0 is regression-tested against the incumbent
`cum_exit` that the live system trades today.

The failure this file is really guarding against: an exit that never fires.
A silently-dead exit does not crash — it turns its variant into a pure
25-day-valve strategy, and the study would then report a rule nobody
proposed, under the name of one they did.
"""
from __future__ import annotations

import numpy as np
import pytest

from strategies.c2_exits import (
    E0_RSI_EXIT, E1_RSI_EXIT, E2_RSI_EXIT, E4_SMA_WINDOW, EXIT_IDS, EXITS,
    INCUMBENT, MA_FAMILY, RSI_FAMILY, ExitSpec, exit_variant, get_exit,
    make_rsi_exit, raw_entry_signals, sma10_exit,
)
from strategies.rsi2 import (
    CUM_RSI_EXIT, EXIT_SMA_10, Indicators, c1_exit, c2_entry, cum_exit,
    sma_series,
)


def ind_from(closes):
    return Indicators.build(np.asarray(closes, dtype=float))


def rising(n=300, rate=0.0015):
    return list(100 * ((1 + rate) ** np.arange(n)))


def rising_then_dip(n=300, dip=0.06):
    base = rising(n - 2)
    last = base[-1]
    return base + [last * (1 - dip / 2), last * (1 - dip)]


# ─────────────────────────────────────────────────────────────────────
# Registration
# ─────────────────────────────────────────────────────────────────────

class TestRegistration:
    def test_exactly_five_contenders(self):
        assert len(EXIT_IDS) == 5
        assert EXIT_IDS == ("E0", "E1", "E2", "E3", "E4")

    def test_e0_is_the_incumbent(self):
        assert INCUMBENT == "E0"
        assert "INCUMBENT" in EXITS["E0"].status

    def test_e0_threshold_is_imported_not_retyped(self):
        # If live's constant ever moves, E0 must move with it or the study
        # is benchmarking against something that is not live.
        assert E0_RSI_EXIT == CUM_RSI_EXIT == 65.0

    def test_registered_thresholds(self):
        assert (E1_RSI_EXIT, E2_RSI_EXIT) == (70.0, 75.0)
        assert E4_SMA_WINDOW == EXIT_SMA_10 == 10

    def test_families_partition_the_set(self):
        assert set(RSI_FAMILY) | set(MA_FAMILY) == set(EXIT_IDS)
        assert not set(RSI_FAMILY) & set(MA_FAMILY)
        assert RSI_FAMILY == ("E0", "E1", "E2")
        assert MA_FAMILY == ("E3", "E4")

    def test_book_ranking_is_recorded_before_results(self):
        # RSI>75 >~ RSI>70 ~= MA10 >~ RSI>65 ~= MA5
        ranks = {k: EXITS[k].book_rank for k in EXIT_IDS}
        assert ranks == {"E0": 4, "E1": 2, "E2": 1, "E3": 5, "E4": 3}

    def test_unknown_exit_raises(self):
        with pytest.raises(ValueError, match="forbids additions"):
            get_exit("E5")

    def test_get_exit_is_case_insensitive(self):
        assert get_exit("e2").exit_id == "E2"


class TestVariantWiring:
    def test_every_variant_uses_the_frozen_entry(self):
        for eid in EXIT_IDS:
            assert exit_variant(eid).entry_fn is c2_entry

    def test_variant_uses_its_own_exit(self):
        for eid in EXIT_IDS:
            assert exit_variant(eid).exit_fn is EXITS[eid].exit_fn

    def test_e0_variant_exit_is_the_live_function_itself(self):
        assert exit_variant("E0").exit_fn is cum_exit

    def test_e3_reuses_c1_exit_rather_than_redefining_sma5(self):
        assert exit_variant("E3").exit_fn is c1_exit

    def test_no_book_win_rate_is_attached(self):
        # The published figure belongs to a different entry and window.
        for eid in EXIT_IDS:
            assert exit_variant(eid).book_win_rate is None

    def test_variant_names_are_distinct(self):
        names = {exit_variant(e).name for e in EXIT_IDS}
        assert len(names) == 5


# ─────────────────────────────────────────────────────────────────────
# The RSI-threshold exits
# ─────────────────────────────────────────────────────────────────────

class TestRsiExits:
    def _ind(self, rsi_value):
        f = lambda v: np.array([float(v)])          # noqa: E731
        return Indicators(closes=f(100), rsi2=f(rsi_value), cum_rsi=f(0),
                          sma5=f(100), sma200=f(100))

    def test_fires_strictly_above_threshold(self):
        e = make_rsi_exit(70.0)
        assert e(self._ind(70.01), 0) is True

    def test_does_not_fire_at_the_threshold(self):
        # `>=` would exit a bar early on an exact touch — a different rule.
        assert make_rsi_exit(70.0)(self._ind(70.0), 0) is False

    def test_does_not_fire_below(self):
        assert make_rsi_exit(70.0)(self._ind(69.99), 0) is False

    def test_nan_rsi_does_not_fire(self):
        assert make_rsi_exit(70.0)(self._ind(np.nan), 0) is False

    def test_out_of_range_index_does_not_fire(self):
        e = make_rsi_exit(70.0)
        assert e(self._ind(99.0), 5) is False
        assert e(self._ind(99.0), -1) is False

    def test_thresholds_are_ordered_in_strictness(self):
        # An RSI of 72 exits E0 and E1 but not E2. If this ever fails the
        # three variants have collapsed into each other.
        i = self._ind(72.0)
        assert EXITS["E0"].exit_fn(i, 0) is True
        assert EXITS["E1"].exit_fn(i, 0) is True
        assert EXITS["E2"].exit_fn(i, 0) is False

    def test_e0_matches_the_live_incumbent_everywhere(self):
        ind = ind_from(rising_then_dip())
        for i in range(len(ind.closes)):
            assert EXITS["E0"].exit_fn(ind, i) == cum_exit(ind, i)

    @pytest.mark.parametrize("eid", ["E0", "E1", "E2"])
    def test_rsi_exits_actually_fire_on_real_shaped_data(self, eid):
        # The dead-exit guard: a rule that never fires is not a rule.
        ind = ind_from(rising())
        assert any(EXITS[eid].exit_fn(ind, i) for i in range(len(ind.closes)))

    def test_looser_thresholds_fire_no_more_often(self):
        ind = ind_from(rising_then_dip())
        n = {e: sum(EXITS[e].exit_fn(ind, i) for i in range(len(ind.closes)))
             for e in ("E0", "E1", "E2")}
        assert n["E0"] >= n["E1"] >= n["E2"]


# ─────────────────────────────────────────────────────────────────────
# The moving-average exits
# ─────────────────────────────────────────────────────────────────────

class TestMaExits:
    def test_sma10_fires_when_close_is_above(self):
        closes = [100.0] * 10 + [200.0]
        assert sma10_exit(ind_from(closes), 10) is True

    def test_sma10_does_not_fire_when_close_is_below(self):
        closes = [100.0] * 10 + [50.0]
        assert sma10_exit(ind_from(closes), 10) is False

    def test_sma10_does_not_fire_on_an_exact_touch(self):
        assert sma10_exit(ind_from([100.0] * 12), 11) is False

    def test_sma10_matches_a_hand_computed_average(self):
        closes = list(range(1, 12))              # 1..11
        ind = ind_from(closes)
        # SMA10 at bar 10 averages bars 1..10 -> mean(2..11) = 6.5
        assert ind.sma10[10] == pytest.approx(6.5)
        # `==` not `is`: numpy comparisons return np.bool_, which is never
        # identical to Python's True.
        assert sma10_exit(ind, 10) == (closes[10] > 6.5)

    def test_sma10_undefined_during_warmup(self):
        assert sma10_exit(ind_from([100.0] * 5), 4) is False

    def test_sma10_falls_back_when_the_field_is_absent(self):
        # An Indicators built by older code must not silently never exit.
        c = np.asarray([100.0] * 10 + [200.0], dtype=float)
        stripped = Indicators(closes=c, rsi2=np.full(11, np.nan),
                              cum_rsi=np.full(11, np.nan),
                              sma5=sma_series(c, 5), sma200=np.full(11, np.nan))
        assert stripped.sma10 is None
        assert sma10_exit(stripped, 10) is True

    def test_e3_and_e4_both_fire_on_real_shaped_data(self):
        ind = ind_from(rising())
        for eid in ("E3", "E4"):
            assert any(EXITS[eid].exit_fn(ind, i)
                       for i in range(len(ind.closes)))

    def test_sma5_and_sma10_agree_in_a_pure_uptrend(self):
        # Measured, not assumed: where BOTH are defined, a steady uptrend
        # sits above each on exactly the same bars. The only difference in
        # raw firing count is the 5-bar warm-up gap, which says nothing
        # about the rules. The exits are told apart by WHEN they fire after
        # a dip, which is a hold-length question — see
        # TestVariantsDiverge.test_sma10_holds_longer_than_sma5.
        ind = ind_from(rising())
        both = np.isfinite(ind.sma5) & np.isfinite(ind.sma10)
        n5 = sum(c1_exit(ind, i) for i in range(len(ind.closes)) if both[i])
        n10 = sum(sma10_exit(ind, i) for i in range(len(ind.closes)) if both[i])
        assert n5 == n10

    def test_sma10_warmup_is_five_bars_later_than_sma5(self):
        ind = ind_from(rising())
        assert int(np.argmax(np.isfinite(ind.sma5))) == 4
        assert int(np.argmax(np.isfinite(ind.sma10))) == 9

    def test_out_of_range_index_does_not_fire(self):
        ind = ind_from([100.0] * 12)
        assert sma10_exit(ind, 99) is False
        assert sma10_exit(ind, -1) is False


# ─────────────────────────────────────────────────────────────────────
# The added sma10 field must not disturb the validated indicators
# ─────────────────────────────────────────────────────────────────────

class TestIndicatorsUnchanged:
    def test_ready_ignores_sma10(self):
        # sma10 is deliberately outside `ready()`; if it were inside, the
        # warm-up boundary could shift and prior results with it.
        ind = ind_from(rising())
        for i in (0, 5, 199, 200, 299):
            expected = bool(
                np.isfinite(ind.rsi2[i]) and np.isfinite(ind.cum_rsi[i])
                and np.isfinite(ind.sma5[i]) and np.isfinite(ind.sma200[i]))
            assert ind.ready(i) is expected

    def test_sma10_is_populated_by_build(self):
        ind = ind_from(rising())
        assert ind.sma10 is not None
        assert np.isfinite(ind.sma10[-1])

    def test_sma10_is_defined_wherever_sma200_is(self):
        ind = ind_from(rising())
        fin200 = np.isfinite(ind.sma200)
        assert bool(np.all(np.isfinite(ind.sma10[fin200])))

    def test_existing_series_are_untouched_by_the_addition(self):
        c = np.asarray(rising_then_dip(), dtype=float)
        ind = Indicators.build(c)
        from strategies.rsi2 import (
            CUM_RSI_DAYS, C1_EXIT_SMA, RSI_PERIOD, TREND_SMA,
            cumulative_rsi_series, rsi_series,
        )
        assert np.allclose(ind.rsi2, rsi_series(c, RSI_PERIOD), equal_nan=True)
        assert np.allclose(ind.cum_rsi,
                           cumulative_rsi_series(c, RSI_PERIOD, CUM_RSI_DAYS),
                           equal_nan=True)
        assert np.allclose(ind.sma5, sma_series(c, C1_EXIT_SMA), equal_nan=True)
        assert np.allclose(ind.sma200, sma_series(c, TREND_SMA), equal_nan=True)


# ─────────────────────────────────────────────────────────────────────
# The shared entry universe
# ─────────────────────────────────────────────────────────────────────

class TestSharedEntries:
    def test_raw_signals_match_the_frozen_entry(self):
        closes = rising_then_dip()
        ind = ind_from(closes)
        assert raw_entry_signals(closes) == [
            i for i in range(len(closes)) if c2_entry(ind, i)]

    def test_raw_signals_are_exit_independent(self):
        # The one quantity that IS identical across the five variants.
        closes = rising_then_dip()
        a = raw_entry_signals(closes)
        assert len(a) >= 1
        assert a == raw_entry_signals(closes)

    def test_a_pure_uptrend_produces_no_entries(self):
        assert raw_entry_signals(rising()) == []


# ─────────────────────────────────────────────────────────────────────
# End-to-end: the exits must produce DIFFERENT trade sets
# ─────────────────────────────────────────────────────────────────────

class TestVariantsDiverge:
    def _bars(self, closes):
        import pandas as pd
        idx = pd.date_range("2008-01-02", periods=len(closes), freq="B")
        c = np.asarray(closes, dtype=float)
        return pd.DataFrame({"open": c, "high": c, "low": c, "close": c,
                             "volume": np.full(len(c), 1e6)}, index=idx)

    def _wavy(self, n=1200):
        """Trending with regular dips — enough entries to compare exits."""
        t = np.arange(n)
        return list(100 * (1.0008 ** t) * (1 + 0.05 * np.sin(t / 7.0)))

    def test_every_variant_produces_trades(self):
        from core.c_series_backtest import FREE, replay_variant
        bars = self._bars(self._wavy())
        for eid in EXIT_IDS:
            tr = replay_variant(bars, exit_variant(eid), fill_mode="close",
                                costs=FREE)
            assert len(tr) > 0, f"{eid} produced no trades — dead exit"

    def test_looser_exits_hold_longer(self):
        from core.c_series_backtest import FREE, replay_variant
        bars = self._bars(self._wavy())
        hold = {}
        for eid in ("E0", "E1", "E2"):
            tr = replay_variant(bars, exit_variant(eid), fill_mode="close",
                                costs=FREE)
            hold[eid] = float(np.mean([t.bars_held for t in tr]))
        assert hold["E0"] <= hold["E1"] <= hold["E2"]

    def test_sma10_holds_longer_than_sma5(self):
        from core.c_series_backtest import FREE, replay_variant
        bars = self._bars(self._wavy())
        hold = {}
        for eid in ("E3", "E4"):
            tr = replay_variant(bars, exit_variant(eid), fill_mode="close",
                                costs=FREE)
            hold[eid] = float(np.mean([t.bars_held for t in tr]))
        assert hold["E4"] > hold["E3"]

    def test_positions_never_overlap_within_an_instrument(self):
        # THIS is the mechanism that lets taken entries differ between
        # exits: one position per instrument, and scanning resumes only
        # after the exit. A slower exit can therefore be mid-trade when a
        # signal fires and skip it. Whether that actually happens is an
        # empirical question answered on real data in Step 1's census, not
        # something to assert here.
        from core.c_series_backtest import FREE, replay_variant
        bars = self._bars(self._wavy())
        for eid in EXIT_IDS:
            tr = replay_variant(bars, exit_variant(eid), fill_mode="close",
                                costs=FREE)
            for a, b in zip(tr, tr[1:]):
                assert b.entry_index > a.exit_index, (
                    f"{eid}: overlapping positions {a.entry_date}/{b.entry_date}")

    def test_taken_entries_can_be_a_strict_subset_of_the_universe(self):
        # Signals fired while already in a position are skipped by every
        # variant — so "shared entry count" means the raw universe, not the
        # taken set.
        from core.c_series_backtest import FREE, replay_variant
        closes = self._wavy()
        bars = self._bars(closes)
        universe = raw_entry_signals(closes)
        tr = replay_variant(bars, exit_variant("E0"), fill_mode="close",
                            costs=FREE)
        assert len(tr) < len(universe)

    def test_all_taken_entries_are_a_subset_of_the_shared_universe(self):
        from core.c_series_backtest import FREE, replay_variant
        closes = self._wavy()
        bars = self._bars(closes)
        universe = {str(bars.index[i].date()) for i in raw_entry_signals(closes)}
        for eid in EXIT_IDS:
            tr = replay_variant(bars, exit_variant(eid), fill_mode="close",
                                costs=FREE)
            assert {t.entry_date for t in tr} <= universe
