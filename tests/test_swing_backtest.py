"""tests/test_swing_backtest.py — replay engine correctness (Phase 4).

A backtest that is wrong in the optimistic direction is worse than no
backtest: it manufactures confidence. These tests pin the properties that
decide whether the verdict can be believed at all.

The load-bearing ones:
  - NO LOOKAHEAD. A setup computed on bar i can only fill on bar i+1 or
    later, never at bar i's close.
  - Pessimistic intrabar. When one bar spans both stop and target, the
    stop is assumed first.
  - Gaps fill against you on both sides.
  - Costs actually reduce R.
  - The engine can produce a winner on a series engineered to be one —
    a backtest that can only lose is broken, not conservative.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from core import swing_backtest as bt
from core.swing_backtest import (
    Trade, compute_stats, evaluate_gate, replay_ticker, simulate_portfolio,
    simulate_trade, stats_by_year,
)


def frame(rows) -> pd.DataFrame:
    """rows = list of (open, high, low, close, volume)."""
    df = pd.DataFrame(rows, columns=["open", "high", "low", "close", "volume"])
    df.index = pd.date_range("2025-01-01", periods=len(df), freq="B")
    return df


class _Setup:
    """Minimal stand-in for a SwingSetup — simulate_trade only reads the
    plan levels and the setup type."""
    def __init__(self, entry, stop, target, setup_type="PULLBACK", entry_limit=None):
        self.entry, self.stop, self.target = entry, stop, target
        self.entry_limit = entry_limit if entry_limit is not None else entry * 1.002
        self.setup_type = setup_type
        self.qualifies = True


# entry 100 (limit 100.20), stop 96 → 1R = 4.00, target 110
def plan_setup():
    return _Setup(100.0, 96.0, 110.0)


class TestFills:
    def test_unfilled_plan_is_not_a_trade(self):
        """A plan whose buy stop never trades must not enter the stats —
        not even as a scratch. It would dilute expectancy toward zero."""
        bars = frame([(98, 99, 97, 98, 1e6)] * 5)
        assert simulate_trade(bars, 0, plan_setup(), ticker="T", costs_bps=0) is None

    def test_fill_at_trigger_when_bar_trades_through(self):
        bars = frame([(98, 105, 97, 104, 1e6)] + [(104, 105, 103, 104, 1e6)] * 20)
        t = simulate_trade(bars, 0, plan_setup(), ticker="T", costs_bps=0)
        assert t is not None
        assert t.fill_price == pytest.approx(100.0)

    def test_gap_within_limit_fills_at_open_not_trigger(self):
        """Gapping through your buy stop fills you worse — but only up to
        the stop-limit cap."""
        bars = frame([(100.15, 106, 100.1, 105, 1e6)] + [(105, 106, 104, 105, 1e6)] * 20)
        t = simulate_trade(bars, 0, plan_setup(), ticker="T", costs_bps=0)
        assert t.fill_price == pytest.approx(100.15)

    def test_gap_through_the_limit_is_no_trade(self):
        """Order A is a Stop-LIMIT (plan §6). A gap beyond the limit that
        never comes back must NOT fill — modelling it as a stop-market
        drags in exactly the chased entries the limit exists to refuse."""
        bars = frame([(103, 106, 102.5, 105, 1e6)] + [(105, 106, 104, 105, 1e6)] * 20)
        assert simulate_trade(bars, 0, plan_setup(), ticker="T", costs_bps=0) is None

    def test_gap_through_limit_then_back_fills_at_the_limit(self):
        bars = frame([(103, 106, 99, 105, 1e6)] + [(105, 106, 104, 105, 1e6)] * 20)
        t = simulate_trade(bars, 0, plan_setup(), ticker="T", costs_bps=0)
        assert t.fill_price == pytest.approx(100.20)   # the limit, not the open

    def test_gap_down_stop_fills_at_open(self):
        # Entry bar closes flat so the ratchet does not lift the stop; the
        # gap must therefore be attributed to the ORIGINAL stop.
        bars = frame([
            (100, 100.5, 99, 100, 1e6),   # entry bar, fills at 100
            (90, 91, 89, 90, 1e6),        # gaps way below the 96 stop
        ] + [(90, 91, 89, 90, 1e6)] * 20)
        t = simulate_trade(bars, 0, plan_setup(), ticker="T", costs_bps=0)
        assert t.exit_reason == "STOP"
        # Filled at the 90 open, not the 96 stop → worse than -1R.
        assert t.r_multiple < -1.0
        assert t.r_multiple == pytest.approx((90 - 100) / 4.0)

    def test_entry_bar_high_does_not_ratchet_the_stop(self):
        """We cannot prove we held through the entry bar's high — the fill
        happens somewhere inside that bar. Ratcheting on it would credit a
        move we may never have been in."""
        bars = frame([
            (100, 106, 99, 100, 1e6),     # runs to +1.5R intrabar, closes flat
            (90, 91, 89, 90, 1e6),        # then gaps down
        ] + [(90, 91, 89, 90, 1e6)] * 20)
        t = simulate_trade(bars, 0, plan_setup(), ticker="T", costs_bps=0)
        assert t.exit_reason == "STOP"          # not BREAKEVEN_STOP

    def test_later_bar_high_does_ratchet(self):
        """The same high on a bar we demonstrably held through must move
        the stop to breakeven."""
        bars = frame([
            (100, 100.5, 99, 100, 1e6),   # entry
            (100, 106, 99.5, 105, 1e6),   # +1.5R on a held bar
            (90, 91, 89, 90, 1e6),        # gap down
        ] + [(90, 91, 89, 90, 1e6)] * 20)
        t = simulate_trade(bars, 0, plan_setup(), ticker="T", costs_bps=0)
        assert t.exit_reason in ("BREAKEVEN_STOP", "TRAIL_STOP")

    def test_entry_bar_can_stop_out(self):
        bars = frame([(100, 105, 95, 96, 1e6)] + [(96, 97, 95, 96, 1e6)] * 20)
        t = simulate_trade(bars, 0, plan_setup(), ticker="T", costs_bps=0)
        assert t.exit_reason == "STOP"
        assert t.bars_held == 0


class TestPessimism:
    def test_stop_wins_when_one_bar_spans_both(self):
        """The intrabar path is unknowable from OHLC. Assuming the target
        first would roughly double reported expectancy and cannot be
        justified from daily data."""
        bars = frame([
            (100, 101, 99.5, 100.5, 1e6),        # entry fills at 100
            (100, 115, 90, 100, 1e6),            # spans stop 96 AND target 110
        ] + [(100, 101, 99, 100, 1e6)] * 20)
        t = simulate_trade(bars, 0, plan_setup(), ticker="T", costs_bps=0)
        assert t.exit_reason == "STOP"
        assert t.r_multiple < 0
        assert t.partial_taken is False

    def test_costs_reduce_r(self):
        bars = frame([(100, 101, 99.5, 100.5, 1e6)]
                     + [(105, 106, 104, 105, 1e6)] * 20)
        free = simulate_trade(bars, 0, plan_setup(), ticker="T",
                              costs=bt.CostModel(slippage_bps=0, tick_size=0))
        paid = simulate_trade(bars, 0, plan_setup(), ticker="T",
                              costs=bt.CostModel(slippage_bps=15, tick_size=0.01))
        assert paid.r_multiple < free.r_multiple

    def test_entry_slippage_worsens_the_fill(self):
        bars = frame([(100, 101, 99.5, 100.5, 1e6)]
                     + [(105, 106, 104, 105, 1e6)] * 20)
        t = simulate_trade(bars, 0, plan_setup(), ticker="T",
                           costs=bt.CostModel(slippage_bps=10, tick_size=0.01))
        assert t.fill_price > 100.0          # bought higher than the trigger


class TestCostModel:
    """P1-7 item 2 — commission and slippage are separate, and slippage
    applies to STOP fills only."""

    def test_stop_fills_slip_against_you(self):
        c = bt.CostModel(slippage_bps=10, tick_size=0.0)
        assert c.fill(100.0, buying=True, order_type="stop") == pytest.approx(100.10)
        assert c.fill(100.0, buying=False, order_type="stop") == pytest.approx(99.90)

    def test_limit_fills_do_not_slip(self):
        """Order B is a take-profit LIMIT: it fills at your price or not at
        all. Charging slippage there would overstate costs."""
        c = bt.CostModel(slippage_bps=50, tick_size=0.0)
        assert c.fill(100.0, buying=False, order_type="limit") == pytest.approx(100.0)

    def test_market_fills_slip(self):
        c = bt.CostModel(slippage_bps=10, tick_size=0.0)
        assert c.fill(100.0, buying=False, order_type="market") == pytest.approx(99.90)

    def test_tick_floor_binds_on_cheap_stocks(self):
        """P1-7: 'or 1 tick, whichever larger'. On a $10 stock, 10bps is
        $0.01 — the tick floor must dominate below that."""
        c = bt.CostModel(slippage_bps=10, tick_size=0.01)
        assert c.slip(5.0) == pytest.approx(0.01)     # 10bps = $0.005 → floor
        assert c.slip(100.0) == pytest.approx(0.10)   # 10bps = $0.10  → bps

    def test_slippage_never_helps(self):
        c = bt.CostModel(slippage_bps=10, tick_size=0.01)
        for p in (5.0, 50.0, 500.0):
            assert c.fill(p, buying=True, order_type="stop") > p
            assert c.fill(p, buying=False, order_type="stop") < p

    def test_describe_reports_the_config(self):
        d = bt.CostModel(commission_per_side=1.0, slippage_bps=12).describe()
        assert d["commission_per_side_usd"] == 1.0
        assert d["slippage_bps_per_side"] == 12
        assert "limit" in d["slippage_applies_to"]

    def test_stop_exit_is_charged_slippage(self):
        """The charge v1 missed entirely: a stop-market exit slips, and it
        slips worst exactly when it fires."""
        bars = frame([(100, 100.5, 99, 100, 1e6)]
                     + [(97, 97.5, 95, 96, 1e6)] * 20)
        free = simulate_trade(bars, 0, plan_setup(), ticker="T",
                              costs=bt.CostModel(slippage_bps=0, tick_size=0))
        paid = simulate_trade(bars, 0, plan_setup(), ticker="T",
                              costs=bt.CostModel(slippage_bps=15, tick_size=0.01))
        assert paid.exit_reason == "STOP"
        assert paid.r_multiple < free.r_multiple
        assert paid.r_multiple < -1.0        # a stop-out costs MORE than 1R


class TestProvenance:
    """P1-7 item 1 + acceptance — the universe must be classified, and a
    survivor universe must not be reported as a clean GO."""

    def _prices(self, ends):
        rows = []
        for tkr, end in ends.items():
            dates = pd.date_range("2021-01-01", end, freq="B")
            for d in dates:
                rows.append({"date": d, "ticker": tkr, "open": 10.0, "high": 11.0,
                             "low": 9.0, "close": 10.0, "volume": 2e6})
        return pd.DataFrame(rows)

    def test_survivor_universe_detected(self):
        p = self._prices({"A": "2026-06-26", "B": "2026-06-26", "C": "2026-06-26"})
        prov = bt.universe_provenance(p)
        assert prov["universe_type"] == "SURVIVOR"
        assert prov["n_delisted"] == 0
        assert "UPPER BOUND" in prov["warning"]

    def test_pit_universe_detected(self):
        p = self._prices({"A": "2026-06-26", "B": "2026-06-26", "DEAD": "2023-05-01"})
        prov = bt.universe_provenance(p)
        assert prov["universe_type"] == "PIT"
        assert prov["n_delisted"] == 1
        assert prov["warning"] is None

    def test_real_cache_is_flagged_survivor(self):
        """The shipped cache must be classified honestly, not by a label
        somebody could forget to update."""
        import pathlib
        f = pathlib.Path("data/validation_cache/prices.parquet")
        if not f.exists():
            pytest.skip("price cache not present")
        prov = bt.universe_provenance(pd.read_parquet(f))
        assert prov["universe_type"] == "SURVIVOR"

    def test_go_is_downgraded_on_a_survivor_universe(self):
        stats = dict(n_trades=300, expectancy_r=0.30, win_rate=0.45, t_stat=3.0)
        by_year = {"2024": {"n_trades": 100, "expectancy_r": 0.2},
                   "2025": {"n_trades": 100, "expectancy_r": 0.3}}
        pf = {"max_drawdown_pct": 10.0}
        prov = {"universe_type": "SURVIVOR", "n_delisted": 0, "n_tickers": 241}
        v = bt.evaluate_gate(stats, by_year, pf, provenance=prov)
        assert v["decision"] == "GO_UPPER_BOUND_ONLY"
        assert any("UPPER BOUND" in c for c in v["caveats"])

    def test_go_stands_on_a_pit_universe(self):
        stats = dict(n_trades=300, expectancy_r=0.30, win_rate=0.45, t_stat=3.0)
        by_year = {"2024": {"n_trades": 100, "expectancy_r": 0.2},
                   "2025": {"n_trades": 100, "expectancy_r": 0.3}}
        pf = {"max_drawdown_pct": 10.0}
        prov = {"universe_type": "PIT", "n_delisted": 12, "n_tickers": 241}
        assert bt.evaluate_gate(stats, by_year, pf, provenance=prov)["decision"] == "GO"

    def test_lookahead_failure_invalidates_everything(self):
        stats = dict(n_trades=300, expectancy_r=0.30, win_rate=0.45, t_stat=3.0)
        by_year = {"2024": {"n_trades": 100, "expectancy_r": 0.2}}
        pf = {"max_drawdown_pct": 10.0}
        v = bt.evaluate_gate(stats, by_year, pf,
                             lookahead={"passed": False, "mismatches": ["X"]})
        assert v["decision"] == "INVALID"

    def test_lookahead_confirmation_runs_on_real_data(self):
        """P1-7 item 3 acceptance: the run must CONFIRM no look-ahead, not
        assert it. This exercises the confirmation itself."""
        import pathlib
        f = pathlib.Path("data/validation_cache/prices.parquet")
        if not f.exists():
            pytest.skip("price cache not present")
        out = bt.confirm_no_lookahead(pd.read_parquet(f), n_tickers=2)
        assert out["passed"] is True
        assert out["trades_compared"] > 0, "confirmation was vacuous"


class TestExits:
    def test_target_takes_partial_and_keeps_running(self):
        bars = frame([(100, 101, 99.5, 100.5, 1e6),
                      (111, 112, 110, 111, 1e6)]
                     + [(111, 112, 110, 111, 1e6)] * 20)
        t = simulate_trade(bars, 0, plan_setup(), ticker="T", costs_bps=0)
        assert t.partial_taken is True

    def test_time_stop_fires_at_configured_bar_count(self):
        bars = frame([(100, 101, 99.5, 100.5, 1e6)]
                     + [(100.5, 101, 100, 100.5, 1e6)] * 30)
        t = simulate_trade(bars, 0, plan_setup(), ticker="T",
                           costs_bps=0, time_stop_bars=15)
        assert t.exit_reason.startswith("TIME_STOP")
        assert t.bars_held == 15

    def test_max_bars_never_binds_with_a_live_time_stop(self):
        bars = frame([(100, 101, 99.5, 100.5, 1e6)]
                     + [(100.5, 101, 100, 100.5, 1e6)] * 80)
        t = simulate_trade(bars, 0, plan_setup(), ticker="T",
                           costs_bps=0, time_stop_bars=15, max_bars=60)
        assert t.exit_reason != "END_OF_DATA"

    def test_unresolved_trade_is_labelled_end_of_data(self):
        """A trade still open when the series ends is marked to the last
        close. Its R is an estimate and must be distinguishable from a
        realised exit."""
        bars = frame([(100, 101, 99.5, 100.5, 1e6)]
                     + [(100.5, 101, 100, 100.5, 1e6)] * 4)
        t = simulate_trade(bars, 0, plan_setup(), ticker="T",
                           costs_bps=0, time_stop_bars=15)
        assert t.exit_reason == "END_OF_DATA"
        assert t.bars_held < 15

    def test_winner_is_possible(self):
        """Sanity: an engine that cannot produce a winner on a series
        engineered to be one is broken, not conservative."""
        bars = frame([(100, 101, 99.5, 100.5, 1e6)]
                     + [(100 + 2 * i, 101 + 2 * i, 99 + 2 * i, 100 + 2 * i, 1e6)
                        for i in range(1, 20)])
        t = simulate_trade(bars, 0, plan_setup(), ticker="T", costs_bps=0)
        assert t.r_multiple > 0

    def test_mfe_and_mae_bracket_the_result(self):
        bars = frame([(100, 101, 99.5, 100.5, 1e6),
                      (101, 108, 98, 100, 1e6),
                      (100, 101, 95, 95, 1e6)]
                     + [(95, 96, 94, 95, 1e6)] * 20)
        t = simulate_trade(bars, 0, plan_setup(), ticker="T", costs_bps=0)
        assert t.mae_r <= t.r_multiple <= t.mfe_r + 1e-9


class TestNoLookahead:
    """The property that decides whether the verdict means anything."""

    def _uptrend_with_signal(self, n=300, rally_bars=25):
        """A pullback setup followed by a rally that actually triggers the
        buy stop. make_pullback alone signals only on its FINAL bar, which
        replay_ticker correctly refuses to trade (no bar left to fill on),
        so the raw fixture yields zero trades."""
        from tests.test_swing_setups import make_pullback
        df = make_pullback(n=n)

        rows, px = [], float(df.iloc[-1]["close"])
        for _ in range(rally_bars):
            px *= 1.012
            rows.append((px * 0.998, px * 1.012, px * 0.99, px, 900_000.0))
        tail = pd.DataFrame(rows, columns=["open", "high", "low", "close", "volume"])
        tail.index = pd.date_range(df.index[-1] + pd.Timedelta(days=1),
                                   periods=len(tail), freq="B")
        return pd.concat([df, tail])

    def _spy(self, n=325):
        """SPY must be supplied or gates 1 and 5 fail closed and the
        replay yields nothing — which would make these tests vacuous."""
        from tests.test_swing_setups import make_spy
        return make_spy(n=n)

    def test_entry_index_is_after_signal_bar(self):
        df = self._uptrend_with_signal()
        trades = replay_ticker(df, "T", spy_bars=self._spy(), costs_bps=0)
        assert trades, "fixture should produce at least one setup"
        for t in trades:
            # replay_ticker signals on bar i and enters on i+1; the entry
            # bar therefore can never be the warmup bar itself.
            assert t.entry_index >= bt._WARMUP_BARS + 1

    def test_signal_only_sees_prior_bars(self, monkeypatch):
        """Assert the slice handed to evaluate_bars never extends past the
        signal bar. Catches an off-by-one that would leak tomorrow's close."""
        seen = []
        from core import swing_setups

        real = swing_setups.evaluate_bars

        def probe(ticker, hist, **kw):
            # Record BOTH slice lengths: SPY must be truncated to the same
            # as-of bar as the ticker, or the regime gate would be reading
            # a market state from the future.
            spy_hist = kw.get("spy_hist")
            seen.append((len(hist), None if spy_hist is None else len(spy_hist)))
            return real(ticker, hist, **kw)

        monkeypatch.setattr(swing_setups, "evaluate_bars", probe)
        df = self._uptrend_with_signal()
        replay_ticker(df, "T", spy_bars=self._spy(), costs_bps=0)
        assert seen, "evaluate_bars was never called"

        for n_hist, n_spy in seen:
            assert n_hist <= len(df)          # strictly a prefix
            assert n_spy == n_hist            # SPY aligned to the same bar

    def test_future_bars_cannot_change_a_closed_trade(self):
        """Append wildly different future data; already-closed trades must
        be byte-identical. Any dependence on later bars is lookahead."""
        df = self._uptrend_with_signal()
        spy = self._spy(n=len(df) + 60)
        base = replay_ticker(df, "T", spy_bars=spy, costs_bps=0)
        assert base

        tail = df.iloc[-1:].copy()
        tail = pd.concat([tail] * 40, ignore_index=False)
        tail.index = pd.date_range(df.index[-1] + pd.Timedelta(days=1),
                                   periods=len(tail), freq="B")
        tail[["open", "high", "low", "close"]] *= 3.0      # a violent rally
        extended = replay_ticker(pd.concat([df, tail]), "T", spy_bars=spy, costs_bps=0)

        for a, b in zip(base, extended):
            if b.exit_index < len(df) - 1:
                assert a.r_multiple == pytest.approx(b.r_multiple)
                assert a.exit_reason == b.exit_reason

    def test_no_overlapping_trades_in_one_ticker(self):
        df = self._uptrend_with_signal()
        trades = replay_ticker(df, "T", spy_bars=self._spy(), costs_bps=0)
        for a, b in zip(trades, trades[1:]):
            assert b.entry_index > a.exit_index

    def test_replay_is_deterministic(self):
        df = self._uptrend_with_signal()
        spy = self._spy()
        a = replay_ticker(df, "T", spy_bars=spy, costs_bps=0)
        b = replay_ticker(df, "T", spy_bars=spy, costs_bps=0)
        assert [t.r_multiple for t in a] == [t.r_multiple for t in b]


class TestStats:
    def _t(self, r, year=2025, exit_date="2025-06-01", **kw):
        base = dict(
            ticker="T", setup_type="PULLBACK", entry_date="2025-05-01",
            exit_date=exit_date, entry_index=0, exit_index=5, bars_held=5,
            plan_entry=100.0, plan_stop=96.0, plan_target=110.0,
            fill_price=100.0, exit_price=100.0, risk_per_share=4.0,
            r_multiple=r, mfe_r=max(r, 0), mae_r=min(r, 0),
            exit_reason="STOP" if r < 0 else "TRAIL_STOP",
            partial_taken=False, year=year,
        )
        base.update(kw)
        return Trade(**base)

    def test_expectancy_is_the_mean_r(self):
        s = compute_stats([self._t(1.0), self._t(-1.0), self._t(2.0)])
        assert s["expectancy_r"] == pytest.approx(2.0 / 3.0, abs=1e-3)

    def test_win_rate_counts_positive_r(self):
        s = compute_stats([self._t(1.0), self._t(-1.0), self._t(-0.5), self._t(0.5)])
        assert s["win_rate"] == pytest.approx(0.5)

    def test_zero_r_counts_as_a_loss_not_a_win(self):
        """A breakeven exit consumed risk and returned nothing. Counting it
        as a win would flatter the win rate."""
        s = compute_stats([self._t(0.0), self._t(1.0)])
        assert s["win_rate"] == pytest.approx(0.5)

    def test_drawdown_is_measured_on_the_realised_curve(self):
        trades = [
            self._t(2.0, exit_date="2025-01-01"),
            self._t(-1.0, exit_date="2025-02-01"),
            self._t(-1.0, exit_date="2025-03-01"),
            self._t(1.0, exit_date="2025-04-01"),
        ]
        assert compute_stats(trades)["max_drawdown_r"] == pytest.approx(2.0)

    def test_empty_input_is_safe(self):
        assert compute_stats([])["n_trades"] == 0

    def test_by_year_partitions_trades(self):
        by = stats_by_year([self._t(1.0, year=2024), self._t(-1.0, year=2025)])
        assert by["2024"]["n_trades"] == 1
        assert by["2025"]["n_trades"] == 1


class TestPortfolioSim:
    def _t(self, r, entry, exit_, **kw):
        return Trade(
            ticker="T", setup_type="PULLBACK", entry_date=entry, exit_date=exit_,
            entry_index=0, exit_index=5, bars_held=5, plan_entry=100.0,
            plan_stop=96.0, plan_target=110.0, fill_price=100.0, exit_price=100.0,
            risk_per_share=4.0, r_multiple=r, mfe_r=0.0, mae_r=0.0,
            exit_reason="STOP", partial_taken=False, year=int(entry[:4]), **kw,
        )

    def test_concurrency_cap_rejects_overlapping_trades(self):
        trades = [self._t(1.0, "2025-01-01", "2025-03-01") for _ in range(5)]
        out = simulate_portfolio(trades, max_concurrent=2)
        assert out["n_taken"] == 2
        assert out["n_rejected"] == 3

    def test_capacity_frees_after_an_exit(self):
        trades = [
            self._t(1.0, "2025-01-01", "2025-01-10"),
            self._t(1.0, "2025-01-20", "2025-01-30"),
        ]
        out = simulate_portfolio(trades, max_concurrent=1)
        assert out["n_taken"] == 2

    def test_rejected_trades_do_not_affect_equity(self):
        trades = [self._t(-5.0, "2025-01-01", "2025-03-01") for _ in range(4)]
        out = simulate_portfolio(trades, max_concurrent=1, risk_pct=0.01,
                                 starting_equity=10_000.0)
        # Only one trade taken: -5R * 1% = -5% of equity.
        assert out["final_equity"] == pytest.approx(9_500.0)

    def test_drawdown_is_reported_positive(self):
        trades = [self._t(-1.0, "2025-01-01", "2025-01-10")]
        assert simulate_portfolio(trades)["max_drawdown_pct"] >= 0


class TestGate:
    def _inputs(self, **over):
        stats = dict(n_trades=300, expectancy_r=0.30, win_rate=0.45,
                     t_stat=3.0, max_drawdown_r=10.0)
        stats.update(over.pop("stats", {}))
        by_year = over.pop("by_year", {"2024": {"n_trades": 100, "expectancy_r": 0.2},
                                       "2025": {"n_trades": 100, "expectancy_r": 0.3}})
        portfolio = dict(max_drawdown_pct=10.0)
        portfolio.update(over.pop("portfolio", {}))
        return stats, by_year, portfolio

    def test_all_pass_is_go(self):
        assert evaluate_gate(*self._inputs())["decision"] == "GO"

    @pytest.mark.parametrize("field,value", [
        ("n_trades", 100),
        ("expectancy_r", 0.10),
        ("win_rate", 0.30),
        ("t_stat", 1.0),
    ])
    def test_any_single_failure_reverts(self, field, value):
        stats, by_year, pf = self._inputs(stats={field: value})
        assert evaluate_gate(stats, by_year, pf)["decision"] == "REVERT"

    def test_drawdown_failure_reverts(self):
        stats, by_year, pf = self._inputs(portfolio={"max_drawdown_pct": 30.0})
        assert evaluate_gate(stats, by_year, pf)["decision"] == "REVERT"

    def test_one_bad_year_reverts_despite_good_aggregate(self):
        """F.5 died on a single fold. An aggregate that hides a
        catastrophic regime is not a validation."""
        stats, _, pf = self._inputs()
        by_year = {"2024": {"n_trades": 150, "expectancy_r": 0.9},
                   "2025": {"n_trades": 150, "expectancy_r": -0.6}}
        v = evaluate_gate(stats, by_year, pf)
        assert v["decision"] == "REVERT"
        assert v["worst_year"] == "2025"

    def test_thin_years_do_not_veto(self):
        """A year with 3 trades is noise, not a regime."""
        stats, _, pf = self._inputs()
        by_year = {"2024": {"n_trades": 150, "expectancy_r": 0.3},
                   "2025": {"n_trades": 3, "expectancy_r": -2.0}}
        assert evaluate_gate(stats, by_year, pf)["decision"] == "GO"

    def test_every_check_is_reported_not_just_the_first_failure(self):
        stats, by_year, pf = self._inputs(stats={"expectancy_r": -0.5, "win_rate": 0.1})
        v = evaluate_gate(stats, by_year, pf)
        assert len(v["checks"]) == 6
        assert sum(1 for c in v["checks"] if not c["pass"]) >= 2

    def test_gate_thresholds_are_the_registered_ones(self):
        """Pins the pre-registered values. If a future change relaxes the
        gate, that must be a deliberate, visible edit — not a drift."""
        assert bt.GATE["min_expectancy_r"] == 0.25
        assert bt.GATE["min_win_rate"] == 0.40
        assert bt.GATE["min_trades"] == 200
        assert bt.GATE["max_drawdown_pct"] == 15.0
        assert bt.GATE["min_t_stat"] == 2.0
