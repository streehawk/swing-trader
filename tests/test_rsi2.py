"""tests/test_rsi2.py — C-0b/C-0c acceptance tests.

Covers exactly what `connors_rsi2_spec.md` registers:

  C-0b: "(a) known-value test against a hand-computed or published RSI(2)
         sequence (assert to 2 decimals); (b) all-gains series -> 100;
         (c) all-losses -> 0; (d) insufficient history -> None (fail
         closed)." and "cumulative_rsi ... X=2 with RSI values [20, 10]
         -> 30."
  Variants: "uptrend + qualifying RSI -> True; below SMA200 -> False;
         RSI above threshold -> False. Exit functions unit-tested (SMA5
         cross for C1; RSI > 65 for C2/C3; 25-day valve)."
  C-0c: close-fill mode, next-open mode, and the no-look-ahead replay.

The variant predicates are tested against HAND-BUILT indicator arrays
rather than against prices. That separates "does the rule read the right
series" from "is the RSI maths right", so a broken RSI cannot make a
broken rule look correct.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from core import c_series_backtest as CB
from core.c_series_backtest import CostModelPct, FREE, replay_variant
from strategies import rsi2 as R
from strategies.rsi2 import Indicators, Variant


# ─────────────────────────────────────────────────────────────────────
# C-0b — RSI(2), Wilder
# ─────────────────────────────────────────────────────────────────────

# Hand-computed, Wilder, period=2, on closes [100, 101, 100.5, 102, 101, 103]:
#
#   diffs:            +1.0   -0.5   +1.5   -1.0   +2.0
#   seed (first 2):   avg_gain = (1.0+0)/2   = 0.5
#                     avg_loss = (0+0.5)/2   = 0.25
#                     RS = 2.0        -> RSI = 100 - 100/3      = 66.6667  (i=2)
#   +1.5:             avg_gain = (0.5*1 + 1.5)/2   = 1.0
#                     avg_loss = (0.25*1 + 0)/2    = 0.125
#                     RS = 8.0        -> RSI = 100 - 100/9      = 88.8889  (i=3)
#   -1.0:             avg_gain = (1.0*1 + 0)/2     = 0.5
#                     avg_loss = (0.125*1 + 1)/2   = 0.5625
#                     RS = 0.888889   -> RSI = 100 - 100/1.8889 = 47.0588  (i=4)
#   +2.0:             avg_gain = (0.5*1 + 2)/2     = 1.25
#                     avg_loss = (0.5625*1 + 0)/2  = 0.28125
#                     RS = 4.444444   -> RSI = 100 - 100/5.4444 = 81.6327  (i=5)
HAND_CLOSES = [100.0, 101.0, 100.5, 102.0, 101.0, 103.0]
HAND_RSI = [66.67, 88.89, 47.06, 81.63]


class TestRSIKnownValues:

    def test_known_value_sequence_to_two_decimals(self):
        """Registered acceptance (a)."""
        got = R.rsi_series(HAND_CLOSES, 2)
        assert np.isnan(got[:2]).all(), "RSI(2) needs 2 changes = 3 closes"
        for i, want in zip(range(2, 6), HAND_RSI):
            assert round(float(got[i]), 2) == want, f"index {i}"

    def test_scalar_rsi_matches_the_series_tail(self):
        assert round(R.rsi(HAND_CLOSES, 2), 2) == HAND_RSI[-1]

    def test_all_gains_is_100(self):
        """Registered acceptance (b)."""
        assert R.rsi([1, 2, 3, 4, 5, 6], 2) == pytest.approx(100.0)

    def test_all_losses_is_zero(self):
        """Registered acceptance (c)."""
        assert R.rsi([6, 5, 4, 3, 2, 1], 2) == pytest.approx(0.0)

    def test_insufficient_history_is_none(self):
        """Registered acceptance (d) — fails closed, never a neutral 50."""
        assert R.rsi([100.0, 101.0], 2) is None
        assert R.rsi([], 2) is None

    def test_flat_series_is_fifty_not_a_crash(self):
        """Zero gains AND zero losses is genuinely no information. The
        alternative is a divide-by-zero somewhere in the middle of a
        backtest."""
        assert R.rsi([100.0] * 10, 2) == pytest.approx(50.0)

    def test_wilder_differs_from_the_simple_average_variant(self):
        """The spec insists on Wilder specifically. If the two agreed,
        the insistence would be pointless and this test would be dead
        weight — it exists to prove the distinction is real."""
        closes = HAND_CLOSES
        diff = np.diff(np.asarray(closes, float))
        g = np.where(diff > 0, diff, 0.0)[-2:]
        l = np.where(diff < 0, -diff, 0.0)[-2:]
        simple = 100 - 100 / (1 + (g.mean() / l.mean())) if l.mean() else 100.0
        assert abs(simple - R.rsi(closes, 2)) > 1.0

    def test_no_lookahead_by_construction(self):
        """Each value must be reproducible from a truncated prefix. This
        is the property the close-fill convention depends on."""
        rng = np.random.default_rng(4)
        closes = list(100 + np.cumsum(rng.normal(0, 1, 120)))
        full = R.rsi_series(closes, 2)
        for i in (5, 17, 60, 119):
            assert R.rsi(closes[: i + 1], 2) == pytest.approx(float(full[i]))


class TestCumulativeRSI:

    def test_sums_the_last_x_rsi_values(self, monkeypatch):
        """Registered acceptance: X=2 with RSI values [20, 10] -> 30."""
        monkeypatch.setattr(R, "rsi_series",
                            lambda closes, period=2: np.array([20.0, 10.0]))
        assert R.cumulative_rsi([1, 2, 3], 2, 2) == pytest.approx(30.0)

    def test_matches_the_hand_computed_sequence(self):
        assert R.cumulative_rsi(HAND_CLOSES, 2, 2) == pytest.approx(
            47.0588 + 81.6327, abs=0.01)

    def test_x_of_one_is_plain_rsi(self):
        assert R.cumulative_rsi(HAND_CLOSES, 2, 1) == pytest.approx(
            R.rsi(HAND_CLOSES, 2))

    def test_none_when_undefined(self):
        assert R.cumulative_rsi([100.0, 101.0], 2, 2) is None


class TestSMA:

    def test_trailing_window(self):
        s = R.sma_series([1, 2, 3, 4, 5], 3)
        assert np.isnan(s[:2]).all()
        assert s[2] == pytest.approx(2.0)
        assert s[4] == pytest.approx(4.0)

    def test_no_lookahead(self):
        closes = list(np.arange(1.0, 60.0))
        full = R.sma_series(closes, 5)
        for i in (10, 30, 58):
            assert R.sma_series(closes[: i + 1], 5)[-1] == pytest.approx(full[i])


# ─────────────────────────────────────────────────────────────────────
# The variants
# ─────────────────────────────────────────────────────────────────────

def ind_at(*, close, rsi2, cum_rsi, sma5, sma200):
    """A one-bar Indicators built from explicit values."""
    f = lambda v: np.array([float(v)])
    return Indicators(closes=f(close), rsi2=f(rsi2), cum_rsi=f(cum_rsi),
                      sma5=f(sma5), sma200=f(sma200))


class TestC1:

    def test_uptrend_plus_qualifying_rsi_is_true(self):
        i = ind_at(close=100, rsi2=4.0, cum_rsi=10, sma5=102, sma200=90)
        assert R.c1_entry(i, 0) is True

    def test_below_sma200_is_false(self):
        i = ind_at(close=100, rsi2=4.0, cum_rsi=10, sma5=102, sma200=110)
        assert R.c1_entry(i, 0) is False

    def test_rsi_above_threshold_is_false(self):
        i = ind_at(close=100, rsi2=5.0, cum_rsi=10, sma5=102, sma200=90)
        assert R.c1_entry(i, 0) is False        # strict <, 5.0 does not qualify

    def test_exit_when_close_above_sma5(self):
        assert R.c1_exit(ind_at(close=103, rsi2=50, cum_rsi=100,
                                sma5=102, sma200=90), 0) is True

    def test_no_exit_at_or_below_sma5(self):
        assert R.c1_exit(ind_at(close=102, rsi2=50, cum_rsi=100,
                                sma5=102, sma200=90), 0) is False


class TestC2C3:

    def test_c2_qualifies_below_35(self):
        i = ind_at(close=100, rsi2=20, cum_rsi=34.9, sma5=102, sma200=90)
        assert R.c2_entry(i, 0) is True

    def test_c2_rejects_at_35(self):
        i = ind_at(close=100, rsi2=20, cum_rsi=35.0, sma5=102, sma200=90)
        assert R.c2_entry(i, 0) is False

    def test_c2_rejects_below_sma200(self):
        i = ind_at(close=100, rsi2=20, cum_rsi=10, sma5=102, sma200=110)
        assert R.c2_entry(i, 0) is False

    def test_c3_is_looser_than_c2(self):
        """Y=50 must admit what Y=35 rejects — otherwise the two variants
        are the same test run twice."""
        i = ind_at(close=100, rsi2=20, cum_rsi=42.0, sma5=102, sma200=90)
        assert R.c2_entry(i, 0) is False
        assert R.c3_entry(i, 0) is True

    def test_exit_when_rsi_above_65(self):
        assert R.cum_exit(ind_at(close=100, rsi2=65.1, cum_rsi=10,
                                 sma5=99, sma200=90), 0) is True

    def test_no_exit_at_65(self):
        assert R.cum_exit(ind_at(close=100, rsi2=65.0, cum_rsi=10,
                                 sma5=99, sma200=90), 0) is False


class TestWarmupFailsClosed:

    def test_nan_indicators_never_produce_an_entry(self):
        i = Indicators(closes=np.array([100.0]), rsi2=np.array([np.nan]),
                       cum_rsi=np.array([np.nan]), sma5=np.array([np.nan]),
                       sma200=np.array([np.nan]))
        assert R.c1_entry(i, 0) is False
        assert R.c2_entry(i, 0) is False
        assert R.c3_entry(i, 0) is False

    def test_registered_constants_are_the_spec_values(self):
        """A guard against silent drift: the spec forbids sweeps, so these
        numbers must be exactly what was registered."""
        assert (R.RSI_PERIOD, R.TREND_SMA) == (2, 200)
        assert R.C1_RSI_ENTRY == 5.0 and R.C1_EXIT_SMA == 5
        assert (R.CUM_RSI_DAYS, R.C2_CUM_ENTRY, R.C3_CUM_ENTRY) == (2, 35.0, 50.0)
        assert R.CUM_RSI_EXIT == 65.0 and R.TIME_VALVE_BARS == 25


# ─────────────────────────────────────────────────────────────────────
# C-0c — execution modes
# ─────────────────────────────────────────────────────────────────────

def make_bars(n=60, base=100.0, step=1.0):
    """Deterministic ramp; OHLC spread around each close."""
    close = base + step * np.arange(n, dtype=float)
    return pd.DataFrame(
        {"open": close - 0.5, "high": close + 1.0,
         "low": close - 1.0, "close": close,
         "volume": np.full(n, 1e6)},
        index=pd.date_range("2020-01-01", periods=n, freq="B"),
    )


def variant_at(entry_i, exit_from):
    """A stub variant firing at fixed indices, so fill mechanics can be
    tested without depending on RSI values."""
    return Variant("TEST", "stub",
                   lambda ind, i: i == entry_i,
                   lambda ind, i: i >= exit_from,
                   None)


class TestFillModes:

    def test_close_fill_buys_the_signal_bar_close(self):
        b = make_bars()
        t = replay_variant(b, variant_at(10, 12), fill_mode="close", costs=FREE)[0]
        assert t.entry_index == 10
        assert t.entry_price == pytest.approx(float(b["close"].iloc[10]))

    def test_next_open_fill_buys_the_following_open(self):
        b = make_bars()
        t = replay_variant(b, variant_at(10, 12), fill_mode="next_open", costs=FREE)[0]
        assert t.entry_index == 11
        assert t.entry_price == pytest.approx(float(b["open"].iloc[11]))

    def test_next_open_exits_at_the_open_after_the_signal(self):
        """A trader placing orders after the close is late leaving as well
        as late arriving. Modelling only the late entry would flatter the
        result at exactly the wrong moment."""
        b = make_bars()
        t = replay_variant(b, variant_at(10, 12), fill_mode="next_open", costs=FREE)[0]
        assert t.exit_index == 13
        assert t.exit_price == pytest.approx(float(b["open"].iloc[13]))

    def test_close_fill_exits_at_the_signal_close(self):
        b = make_bars()
        t = replay_variant(b, variant_at(10, 12), fill_mode="close", costs=FREE)[0]
        assert t.exit_index == 12
        assert t.exit_price == pytest.approx(float(b["close"].iloc[12]))

    def test_exit_cannot_fire_on_the_entry_bar(self):
        """We commit at the signal bar's close; the earliest exit decision
        is the next close."""
        b = make_bars()
        t = replay_variant(b, variant_at(10, 0), fill_mode="close", costs=FREE)[0]
        assert t.exit_index == 11
        assert t.bars_held == 1

    def test_unknown_fill_mode_raises(self):
        with pytest.raises(ValueError):
            replay_variant(make_bars(), variant_at(10, 12), fill_mode="magic")

    def test_signal_on_the_last_bar_produces_no_next_open_trade(self):
        b = make_bars(n=30)
        assert replay_variant(b, variant_at(29, 40),
                              fill_mode="next_open", costs=FREE) == []


class TestCosts:

    def test_costs_are_charged_on_both_sides(self):
        b = make_bars()
        v = variant_at(10, 12)
        free = replay_variant(b, v, fill_mode="close", costs=FREE)[0]
        paid = replay_variant(b, v, fill_mode="close",
                              costs=CostModelPct(slippage_bps=50.0))[0]
        assert paid.entry_price > free.entry_price
        assert paid.exit_price < free.exit_price
        assert paid.pct_return < free.pct_return

    def test_two_bps_is_the_registered_spy_default(self):
        assert CostModelPct().slippage_bps == 2.0


class TestTimeValve:

    def test_valve_fires_at_25_bars(self):
        b = make_bars(n=80)
        t = replay_variant(b, variant_at(10, 9_999), fill_mode="close",
                           costs=FREE)[0]
        assert t.exit_reason == "TIME_VALVE"
        assert t.bars_held == 25

    def test_valve_does_not_pre_empt_a_real_exit(self):
        b = make_bars(n=80)
        t = replay_variant(b, variant_at(10, 20), fill_mode="close", costs=FREE)[0]
        assert t.exit_reason == "SIGNAL"
        assert t.bars_held == 10

    def test_valve_fires_at_25_bars_under_next_open(self):
        b = make_bars(n=80)
        t = replay_variant(b, variant_at(10, 9_999), fill_mode="next_open",
                           costs=FREE)[0]
        assert t.exit_reason == "TIME_VALVE"
        assert t.bars_held == 25


class TestPositionHandling:

    def test_one_position_at_a_time(self):
        b = make_bars(n=120)
        v = Variant("T", "", lambda ind, i: True, lambda ind, i: i % 7 == 0, None)
        trades = replay_variant(b, v, fill_mode="close", costs=FREE)
        assert len(trades) >= 3
        for a, c in zip(trades, trades[1:]):
            assert c.entry_index > a.exit_index

    def test_percent_return_is_computed_from_the_fills(self):
        b = make_bars()
        t = replay_variant(b, variant_at(10, 12), fill_mode="close", costs=FREE)[0]
        want = (t.exit_price - t.entry_price) / t.entry_price * 100.0
        assert t.pct_return == pytest.approx(want, abs=1e-4)

    def test_mae_ignores_the_entry_bar_low_under_close_fill(self):
        """We bought at that bar's close; its low may have printed before
        we were in, so counting it would invent drawdown we never had."""
        b = make_bars(n=40)
        t = replay_variant(b, variant_at(10, 11), fill_mode="close", costs=FREE)[0]
        # Ramp is monotonically up, so the only sub-entry prints would be
        # the entry bar's own low.
        assert t.mae_pct == pytest.approx(0.0)

    def test_mae_counts_the_entry_bar_under_next_open(self):
        """Bought at the open, so that bar's whole range was ours."""
        b = make_bars(n=40)
        t = replay_variant(b, variant_at(10, 11), fill_mode="next_open", costs=FREE)[0]
        assert t.mae_pct < 0.0


class TestWindowing:

    def test_start_and_end_bound_the_signals(self):
        b = make_bars(n=120)
        v = Variant("T", "", lambda ind, i: True, lambda ind, i: True, None)
        trades = replay_variant(b, v, fill_mode="close", costs=FREE,
                                start=str(b.index[50].date()),
                                end=str(b.index[60].date()))
        assert trades
        assert all(str(b.index[50].date()) <= t.entry_date for t in trades)

    def test_indicators_are_built_on_full_history_not_the_window(self):
        """A trade on the first day of POST-BOOK must use the SMA200 a
        real trader had — not one carved out of 2008 itself."""
        b = make_bars(n=400)
        v = R.get_variant("C1")
        full = replay_variant(b, v, fill_mode="close", costs=FREE)
        windowed = replay_variant(b, v, fill_mode="close", costs=FREE,
                                  start=str(b.index[300].date()))
        late = [t for t in full if t.entry_index >= 300]
        assert [t.entry_index for t in late] == [t.entry_index for t in windowed]


class TestNoLookahead:

    def test_close_fill_replay_is_unchanged_by_a_fabricated_future(self):
        """The C-0c registered check: close-fill is legitimate only if the
        signal uses no data beyond the close it fills at."""
        rng = np.random.default_rng(11)
        n = 700
        close = 100 + np.cumsum(rng.normal(0.05, 1.0, n))
        b = pd.DataFrame(
            {"open": close - 0.3, "high": close + 0.8,
             "low": close - 0.8, "close": close, "volume": np.full(n, 1e6)},
            index=pd.date_range("2015-01-01", periods=n, freq="B"))
        for name in ("C1", "C2", "C3"):
            res = CB.confirm_no_lookahead(b, name, fill_mode="close")
            assert res["passed"], (name, res["mismatches"])
            assert res["trades_compared"] >= 2, (name, res["note"])


class TestMetrics:

    def _trades(self, pcts):
        return [CB.CTrade(variant="T", entry_date=f"2020-01-{i+1:02d}",
                          exit_date=f"2020-01-{i+2:02d}", entry_index=i,
                          exit_index=i + 1, bars_held=1, entry_price=100.0,
                          exit_price=100.0 * (1 + p / 100), pct_return=p,
                          mae_pct=-abs(p), mfe_pct=abs(p),
                          exit_reason="SIGNAL", year=2020)
                for i, p in enumerate(pcts)]

    def test_win_rate_and_profit_factor(self):
        s = CB.compute_c_stats(self._trades([1.0, 2.0, -1.0]))
        assert s["n_trades"] == 3
        assert s["win_rate"] == pytest.approx(2 / 3, abs=1e-4)
        assert s["profit_factor"] == pytest.approx(3.0)
        assert s["worst_trade_pct"] == pytest.approx(-1.0)

    def test_equity_curve_compounds_and_reports_drawdown(self):
        e = CB.equity_curve_stats(self._trades([10.0, -10.0]))
        assert e["final_multiple"] == pytest.approx(0.99)
        assert e["max_drawdown_pct"] == pytest.approx(10.0, abs=1e-6)

    def test_allocation_overlay_scales_the_curve(self):
        e = CB.equity_curve_stats(self._trades([10.0, -10.0]), allocation=0.1)
        assert e["max_drawdown_pct"] == pytest.approx(1.0, abs=1e-6)

    def test_worst_trades_are_sorted_worst_first(self):
        w = CB.worst_trades(self._trades([1.0, -5.0, -2.0]), k=2)
        assert [x["pct"] for x in w] == [-5.0, -2.0]

    def test_overlap_is_symmetric_and_bounded(self):
        a = self._trades([1.0, 1.0, 1.0])
        o = CB.trade_overlap(a, a)
        assert o["jaccard"] == pytest.approx(1.0)
        assert CB.trade_overlap(a, [])["jaccard"] == 0.0

    def test_empty_inputs_do_not_crash(self):
        assert CB.compute_c_stats([]) == {"n_trades": 0}
        assert CB.equity_curve_stats([]) == {"n_trades": 0}
