"""tests/test_etf_basket.py — A1-2 acceptance tests.

The portfolio simulator is the only genuinely new logic in this addendum,
and it is the piece that killed H1, so it is tested on HAND-CONSTRUCTED
positions where the right answer is arithmetic rather than on market data
where a bug hides inside a plausible number.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from core.basket_backtest import (
    BASKET_COSTS_BPS, BASKET_TICKERS, BROAD_INDEX, SECTORS, CONTROL_FIELDS,
    Signal, _equity_stats, _value_path, build_signals, buy_and_hold_daily,
    bars_in_window, cost_model, pooled_stats, signal_overlap_matrix,
    simulate_portfolio, spy_control,
)
from core.c_series_backtest import FREE, CTrade, CostModelPct


# ─────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────

def make_bars(closes, start="2010-01-04"):
    """OHLC where every bar's range is the close — so any range-based bug
    shows up as a difference rather than being masked by a wide bar."""
    idx = pd.date_range(start, periods=len(closes), freq="B")
    c = np.asarray(closes, dtype=float)
    return pd.DataFrame(
        {"open": c, "high": c, "low": c, "close": c,
         "volume": np.full(len(c), 1e6)},
        index=idx,
    )


def trade(entry_i, exit_i, entry_px, exit_px, ticker="XLK"):
    pct = (exit_px - entry_px) / entry_px * 100.0
    return CTrade(
        variant="C2", entry_date="", exit_date="",
        entry_index=entry_i, exit_index=exit_i, bars_held=exit_i - entry_i,
        entry_price=entry_px, exit_price=exit_px, pct_return=round(pct, 4),
        mae_pct=0.0, mfe_pct=0.0, exit_reason="SIGNAL", year=2010,
    )


def sig(ticker, bars, tr, cum_rsi):
    return Signal(
        ticker=ticker,
        entry_date=str(bars.index[tr.entry_index].date()),
        exit_date=str(bars.index[tr.exit_index].date()),
        entry_index=tr.entry_index, exit_index=tr.exit_index,
        pct_return=tr.pct_return, cum_rsi=cum_rsi, trade=tr,
    )


# ─────────────────────────────────────────────────────────────────────
# The registered basket itself
# ─────────────────────────────────────────────────────────────────────

class TestRegistration:
    def test_basket_is_twelve(self):
        assert len(BASKET_TICKERS) == 12

    def test_spy_is_not_in_the_basket(self):
        # The addendum excludes it: its verdict already exists.
        assert "SPY" not in BASKET_TICKERS

    def test_broad_and_sectors_partition_the_basket(self):
        assert set(BROAD_INDEX) | set(SECTORS) == set(BASKET_TICKERS)
        assert not set(BROAD_INDEX) & set(SECTORS)
        assert len(BROAD_INDEX) == 3 and len(SECTORS) == 9

    def test_registered_costs(self):
        for t in BROAD_INDEX:
            assert BASKET_COSTS_BPS[t] == 2.0
        for t in SECTORS:
            assert BASKET_COSTS_BPS[t] == 5.0

    def test_cost_model_matches_registration(self):
        assert cost_model("QQQ").slippage_bps == 2.0
        assert cost_model("XLU").slippage_bps == 5.0
        assert cost_model("SPY").slippage_bps == 2.0

    def test_costs_off_is_free(self):
        assert cost_model("XLU", costs_on=False) is FREE

    def test_unknown_ticker_raises_rather_than_defaulting(self):
        # Silently defaulting to 2bps on something that should pay 5 would
        # improve the result by inventing liquidity.
        with pytest.raises(KeyError):
            cost_model("AAPL")


# ─────────────────────────────────────────────────────────────────────
# Value path — where entry/exit slippage lands
# ─────────────────────────────────────────────────────────────────────

class TestValuePath:
    def test_endpoints_are_the_cost_adjusted_fills(self):
        bars = make_bars([100, 101, 102, 103])
        tr = trade(0, 3, entry_px=100.02, exit_px=102.98)
        dates, vals = _value_path(bars, tr)
        assert len(dates) == 4
        assert vals[0] == pytest.approx(100.02)
        assert vals[-1] == pytest.approx(102.98)

    def test_interior_is_the_raw_close(self):
        bars = make_bars([100, 101, 102, 103])
        tr = trade(0, 3, 100.02, 102.98)
        _, vals = _value_path(bars, tr)
        assert vals[1] == pytest.approx(101.0)
        assert vals[2] == pytest.approx(102.0)

    def test_compounded_path_equals_the_trade_return(self):
        bars = make_bars([100, 101, 102, 103])
        tr = trade(0, 3, 100.02, 102.98)
        _, vals = _value_path(bars, tr)
        compounded = np.prod(vals[1:] / vals[:-1])
        # `pct_return` is stored rounded to 4dp, so the tolerance is that
        # rounding and not a looser "close enough".
        assert compounded == pytest.approx(1.0 + tr.pct_return / 100.0, abs=1e-6)

    def test_single_bar_trade_has_no_daily_return(self):
        bars = make_bars([100, 101])
        tr = trade(0, 0, 100.0, 100.0)
        dates, vals = _value_path(bars, tr)
        assert len(dates) == 1 and len(vals) == 1


# ─────────────────────────────────────────────────────────────────────
# Equity statistics
# ─────────────────────────────────────────────────────────────────────

class TestEquityStats:
    def test_flat_curve(self):
        eq = pd.Series(np.ones(300), index=pd.date_range("2010-01-04", periods=300, freq="B"))
        s = _equity_stats(eq)
        assert s["max_drawdown_pct"] == 0.0
        assert s["cagr_pct"] == pytest.approx(0.0, abs=1e-9)
        assert s["mar"] is None            # 0/0 is not a ratio

    def test_known_drawdown(self):
        # 1.0 -> 1.5 -> 0.75 -> 1.2 : the trough is 50% below the 1.5 peak.
        eq = pd.Series([1.0, 1.5, 0.75, 1.2],
                       index=pd.date_range("2010-01-04", periods=4, freq="B"))
        assert _equity_stats(eq)["max_drawdown_pct"] == pytest.approx(50.0)

    def test_cagr_doubling_over_one_year(self):
        idx = pd.DatetimeIndex(["2010-01-04", "2011-01-04"])
        s = _equity_stats(pd.Series([1.0, 2.0], index=idx))
        assert s["cagr_pct"] == pytest.approx(100.0, abs=0.3)

    def test_marking_is_labelled_daily(self):
        eq = pd.Series([1.0, 1.1], index=pd.date_range("2010-01-04", periods=2, freq="B"))
        assert "daily" in _equity_stats(eq)["marking"]


# ─────────────────────────────────────────────────────────────────────
# The portfolio simulator
# ─────────────────────────────────────────────────────────────────────

class TestPortfolioBasics:
    def test_uncapped_takes_every_signal(self):
        bars = {t: make_bars([100, 102, 104, 106, 108]) for t in ("XLK", "XLF", "XLE", "XLU")}
        sigs = [sig(t, bars[t], trade(0, 2, 100.0, 104.0, t), 20.0) for t in bars]
        r = simulate_portfolio(sigs, bars, name="u", max_concurrent=None,
                               start="2010-01-01")
        assert r.stats["n_taken"] == 4
        assert r.stats["n_refused"] == 0

    def test_cap_refuses_the_overflow(self):
        bars = {t: make_bars([100, 102, 104, 106, 108]) for t in ("XLK", "XLF", "XLE", "XLU")}
        sigs = [sig(t, bars[t], trade(0, 2, 100.0, 104.0, t), 20.0) for t in bars]
        r = simulate_portfolio(sigs, bars, name="c", max_concurrent=3,
                               start="2010-01-01")
        assert r.stats["n_taken"] == 3
        assert r.stats["n_refused"] == 1
        assert r.stats["pct_signals_refused"] == pytest.approx(25.0)

    def test_priority_takes_the_most_oversold_first(self):
        bars = {t: make_bars([100, 102, 104, 106]) for t in ("XLK", "XLF", "XLE", "XLU")}
        cum = {"XLK": 30.0, "XLF": 10.0, "XLE": 20.0, "XLU": 5.0}
        sigs = [sig(t, bars[t], trade(0, 2, 100.0, 104.0, t), cum[t]) for t in bars]
        r = simulate_portfolio(sigs, bars, name="c", max_concurrent=3,
                               start="2010-01-01")
        assert sorted(s.ticker for s in r.taken) == ["XLE", "XLF", "XLU"]
        assert [s.ticker for s in r.refused] == ["XLK"]      # least oversold

    def test_one_position_per_instrument(self):
        # A second signal on a ticker already open must not double up.
        bars = {"XLK": make_bars([100, 101, 102, 103, 104, 105])}
        sigs = [sig("XLK", bars["XLK"], trade(0, 4, 100.0, 104.0), 10.0),
                sig("XLK", bars["XLK"], trade(2, 5, 102.0, 105.0), 10.0)]
        r = simulate_portfolio(sigs, bars, name="c", max_concurrent=3,
                               start="2010-01-01")
        assert r.stats["n_taken"] == 1

    def test_slot_is_held_through_the_exit_bar(self):
        # XLK exits on bar 2; XLF signals on bar 2. With a cap of 1 the slot
        # is NOT recycled within the same session.
        bars = {"XLK": make_bars([100, 101, 102, 103]),
                "XLF": make_bars([100, 101, 102, 103])}
        sigs = [sig("XLK", bars["XLK"], trade(0, 2, 100.0, 102.0, "XLK"), 10.0),
                sig("XLF", bars["XLF"], trade(2, 3, 102.0, 103.0, "XLF"), 5.0)]
        r = simulate_portfolio(sigs, bars, name="c", max_concurrent=1,
                               start="2010-01-01")
        assert [s.ticker for s in r.taken] == ["XLK"]
        assert [s.ticker for s in r.refused] == ["XLF"]

    def test_slot_frees_the_day_after_the_exit(self):
        bars = {"XLK": make_bars([100, 101, 102, 103, 104]),
                "XLF": make_bars([100, 101, 102, 103, 104])}
        sigs = [sig("XLK", bars["XLK"], trade(0, 2, 100.0, 102.0, "XLK"), 10.0),
                sig("XLF", bars["XLF"], trade(3, 4, 103.0, 104.0, "XLF"), 5.0)]
        r = simulate_portfolio(sigs, bars, name="c", max_concurrent=1,
                               start="2010-01-01")
        assert sorted(s.ticker for s in r.taken) == ["XLF", "XLK"]
        assert r.stats["n_refused"] == 0


class TestPortfolioArithmetic:
    def test_single_position_equity_equals_the_trade(self):
        bars = {"XLK": make_bars([100, 110, 121])}
        sigs = [sig("XLK", bars["XLK"], trade(0, 2, 100.0, 121.0), 10.0)]
        r = simulate_portfolio(sigs, bars, name="u", max_concurrent=None,
                               start="2010-01-01")
        assert float(r.equity.iloc[-1]) == pytest.approx(1.21, rel=1e-9)

    def test_two_concurrent_positions_are_equal_weighted(self):
        # +10% and +0% held together = +5% on the account.
        bars = {"XLK": make_bars([100, 110]), "XLF": make_bars([100, 100])}
        sigs = [sig("XLK", bars["XLK"], trade(0, 1, 100.0, 110.0, "XLK"), 10.0),
                sig("XLF", bars["XLF"], trade(0, 1, 100.0, 100.0, "XLF"), 10.0)]
        r = simulate_portfolio(sigs, bars, name="u", max_concurrent=None,
                               start="2010-01-01")
        assert float(r.equity.iloc[-1]) == pytest.approx(1.05, rel=1e-9)

    def test_fixed_slots_sizing_leaves_cash_idle(self):
        # Same +10% single position, but one of three slots = +3.333%.
        bars = {"XLK": make_bars([100, 110])}
        sigs = [sig("XLK", bars["XLK"], trade(0, 1, 100.0, 110.0), 10.0)]
        r = simulate_portfolio(sigs, bars, name="c", max_concurrent=3,
                               sizing="fixed_slots", start="2010-01-01")
        assert float(r.equity.iloc[-1]) == pytest.approx(1.0 + 0.10 / 3, rel=1e-9)

    def test_equal_open_is_fully_invested_with_one_position(self):
        bars = {"XLK": make_bars([100, 110])}
        sigs = [sig("XLK", bars["XLK"], trade(0, 1, 100.0, 110.0), 10.0)]
        r = simulate_portfolio(sigs, bars, name="c", max_concurrent=3,
                               sizing="equal_open", start="2010-01-01")
        assert float(r.equity.iloc[-1]) == pytest.approx(1.10, rel=1e-9)

    def test_cash_days_do_not_move_equity(self):
        bars = {"XLK": make_bars([100, 110, 120, 130, 140])}
        sigs = [sig("XLK", bars["XLK"], trade(0, 1, 100.0, 110.0), 10.0)]
        r = simulate_portfolio(sigs, bars, name="u", max_concurrent=None,
                               start="2010-01-01")
        # Flat from the exit onward even though the instrument kept rising.
        assert float(r.equity.iloc[-1]) == pytest.approx(1.10, rel=1e-9)
        assert r.stats["pct_days_position_open"] == pytest.approx(40.0)

    def test_entry_slippage_is_a_real_drag(self):
        clean = trade(0, 1, 100.0, 110.0)
        slipped = trade(0, 1, 100.10, 110.0)
        assert slipped.pct_return < clean.pct_return


class TestCapacityAutopsy:
    """The H1 post-mortem, repeated: the cap must be measured, not assumed."""

    def test_taken_and_refused_expectancies_are_reported(self):
        bars = {t: make_bars([100, 101, 102, 103]) for t in ("XLK", "XLF", "XLE", "XLU")}
        cum = {"XLK": 5.0, "XLF": 10.0, "XLE": 20.0, "XLU": 30.0}
        # The most-oversold names are deliberately the WORST performers, so a
        # simulator that silently took the best ones would fail this test.
        px = {"XLK": 99.0, "XLF": 99.0, "XLE": 105.0, "XLU": 105.0}
        sigs = [sig(t, bars[t], trade(0, 2, 100.0, px[t], t), cum[t]) for t in bars]
        r = simulate_portfolio(sigs, bars, name="c", max_concurrent=2,
                               start="2010-01-01")
        assert sorted(s.ticker for s in r.taken) == ["XLF", "XLK"]
        assert r.stats["taken_avg_pct"] < 0
        assert r.stats["refused_avg_pct"] > 0

    def test_cluster_reporting(self):
        bars = {t: make_bars([100, 101, 102, 103]) for t in ("XLK", "XLF", "XLE")}
        sigs = [sig(t, bars[t], trade(0, 2, 100.0, 102.0, t), 10.0) for t in bars]
        r = simulate_portfolio(sigs, bars, name="c", max_concurrent=2,
                               start="2010-01-01")
        assert r.cluster["largest_single_day_cluster"] == 3
        assert r.cluster["signal_days_over_cap"] == 1

    def test_random_priority_is_available_as_a_diagnostic(self):
        bars = {t: make_bars([100, 101, 102, 103]) for t in ("XLK", "XLF", "XLE")}
        sigs = [sig(t, bars[t], trade(0, 2, 100.0, 102.0, t), 10.0) for t in bars]
        r = simulate_portfolio(sigs, bars, name="c", max_concurrent=2,
                               priority="random", start="2010-01-01")
        assert r.stats["priority"] == "random"
        assert r.stats["n_taken"] == 2

    def test_random_priority_is_reproducible(self):
        bars = {t: make_bars([100, 101, 102, 103]) for t in ("XLK", "XLF", "XLE")}
        sigs = [sig(t, bars[t], trade(0, 2, 100.0, 102.0, t), 10.0) for t in bars]
        a = simulate_portfolio(sigs, bars, name="c", max_concurrent=2,
                               priority="random", start="2010-01-01", seed=7)
        b = simulate_portfolio(sigs, bars, name="c", max_concurrent=2,
                               priority="random", start="2010-01-01", seed=7)
        assert [s.ticker for s in a.taken] == [s.ticker for s in b.taken]

    def test_bad_sizing_and_priority_raise(self):
        bars = {"XLK": make_bars([100, 101])}
        sigs = [sig("XLK", bars["XLK"], trade(0, 1, 100.0, 101.0), 10.0)]
        with pytest.raises(ValueError):
            simulate_portfolio(sigs, bars, name="x", sizing="kelly",
                               start="2010-01-01")
        with pytest.raises(ValueError):
            simulate_portfolio(sigs, bars, name="x", priority="best_first",
                               start="2010-01-01")
        with pytest.raises(ValueError):
            simulate_portfolio(sigs, bars, name="x", sizing="fixed_slots",
                               max_concurrent=None, start="2010-01-01")


# ─────────────────────────────────────────────────────────────────────
# Clustering + pooling
# ─────────────────────────────────────────────────────────────────────

class TestOverlapAndPooling:
    def test_identical_signal_dates_give_jaccard_one(self):
        a = [trade(0, 1, 100, 101)]
        a[0].entry_date = "2010-01-04"
        b = [trade(0, 1, 100, 102)]
        b[0].entry_date = "2010-01-04"
        m = signal_overlap_matrix({"XLK": a, "XLF": b})
        assert m["matrix"]["XLK"]["XLF"] == pytest.approx(1.0)
        assert m["mean_pairwise_jaccard"] == pytest.approx(1.0)

    def test_disjoint_signal_dates_give_jaccard_zero(self):
        a = [trade(0, 1, 100, 101)]
        a[0].entry_date = "2010-01-04"
        b = [trade(0, 1, 100, 102)]
        b[0].entry_date = "2011-06-01"
        m = signal_overlap_matrix({"XLK": a, "XLF": b})
        assert m["matrix"]["XLK"]["XLF"] == pytest.approx(0.0)

    def test_pooled_stats_counts_every_instrument(self):
        t1 = [trade(0, 1, 100, 101)]
        t2 = [trade(0, 1, 100, 99)]
        p = pooled_stats({"XLK": t1, "XLF": t2})
        assert p["n_trades"] == 2
        assert p["n_instruments"] == 2

    def test_pooled_stats_carries_the_correlation_caveat(self):
        # 12 correlated instruments are not 12 independent confirmations.
        p = pooled_stats({"XLK": [trade(0, 1, 100, 101)]})
        assert "effective" in p["caveat"]


# ─────────────────────────────────────────────────────────────────────
# The control
# ─────────────────────────────────────────────────────────────────────

class TestControl:
    def test_control_detects_a_changed_number(self):
        bars = make_bars(list(np.linspace(100, 200, 400)))
        registered = [{"entry_date": "2010-01-04", "pct_return": 999.0}]
        r = spy_control(bars, registered, start="2010-01-01")
        assert r["passed"] is False
        assert r["n_mismatches"] >= 1

    def test_control_detects_a_trade_count_difference(self):
        bars = make_bars(list(np.linspace(100, 200, 400)))
        r = spy_control(bars, [], start="2010-01-01")
        # A rising line never triggers C2, so both are empty and it passes;
        # an injected phantom trade must not.
        r2 = spy_control(bars, [{"entry_date": "x", "pct_return": 1.0}],
                         start="2010-01-01")
        assert r["passed"] is True
        assert r2["passed"] is False

    def test_control_compares_prices_not_just_returns(self):
        assert "entry_price" in CONTROL_FIELDS
        assert "exit_price" in CONTROL_FIELDS
        assert "exit_reason" in CONTROL_FIELDS


# ─────────────────────────────────────────────────────────────────────
# Window helpers
# ─────────────────────────────────────────────────────────────────────

class TestWindows:
    def test_bars_in_window_counts_from_the_start_date(self):
        bars = make_bars(list(range(100, 200)))
        total = len(bars)
        assert bars_in_window(bars, None) == total
        cut = str(bars.index[10].date())
        assert bars_in_window(bars, cut) == total - 10

    def test_buy_and_hold_is_daily_marked(self):
        bars = make_bars([100, 110, 90, 120])
        s = buy_and_hold_daily(bars, "2010-01-01")
        # Drawdown is reported to 2dp.
        assert s["max_drawdown_pct"] == pytest.approx(100 * (1 - 90 / 110), abs=0.005)
        assert "daily" in s["marking"]

    def test_empty_window_raises_rather_than_returning_zero(self):
        bars = {"XLK": make_bars([100, 101])}
        with pytest.raises(ValueError):
            simulate_portfolio([], bars, name="x", start="2099-01-01")
