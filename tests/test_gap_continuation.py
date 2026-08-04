"""tests/test_gap_continuation.py — H2 acceptance tests.

Covers exactly what edge_search_protocol.md registers under H2:

    "unit tests for gap detection (gap >= 1 ATR over 20-day high = True;
     gap without new high = False; earnings-day gap = excluded), SMA10
     trail exit."

plus the protocol's global guardrails: fail-closed on missing data, the
regime gate registered ON, no look-ahead, the break-even ratchet, and the
verdict machinery shared with H1 via `core.protocol_gate`.

Synthetic OHLCV throughout — no network. The earnings tests use a
hand-built calendar rather than the fetched parquet, so they assert the
GATE's behaviour and not EDGAR's contents.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from core import h2_backtest as H2
from core.swing_backtest import CostModel
from strategies import filters as F
from strategies import gap_continuation as G


# ── Builders ─────────────────────────────────────────────────────────

def frame(rows, start="2024-01-01"):
    return pd.DataFrame(rows, index=pd.date_range(start, periods=len(rows), freq="B"))


def make_uptrend(n=260, *, base=100.0, drift=0.0025, band=0.01, seed=11,
                 noise=0.004, volume=1_000_000.0):
    """A clean uptrend: close > SMA50 > SMA200 by the end.

    H2's trend context is measured on the bar BEFORE the gap, so the
    fixture has to be genuinely trending rather than merely ending high.
    """
    rng = np.random.default_rng(seed)
    closes = np.empty(n, float)
    px = base
    for i in range(n):
        px *= (1 + drift + rng.normal(0, noise))
        closes[i] = px
    return pd.DataFrame(
        {
            "open":   closes * 0.999,
            "high":   closes * (1 + band),
            "low":    closes * (1 - band),
            "close":  closes,
            "volume": np.full(n, volume),
        },
        index=pd.date_range("2023-01-02", periods=n, freq="B"),
    )


def append_gap(bars, *, gap_atr=1.5, rvol=3.0, new_high=True, close_frac=0.9):
    """Append one gap bar sized in ATRs of the PRIOR series.

    `new_high=False` produces a gap that is large in ATR terms but opens
    below the 20-day high — the registered negative case.
    """
    atr = G._wilder_atr(bars)
    prev_close = float(bars["close"].iloc[-1])
    prior_high = float(bars["high"].iloc[-20:].max())

    op = prev_close + gap_atr * atr
    if new_high:
        op = max(op, prior_high * 1.001)
    else:
        op = min(op, prior_high * 0.995)

    hi = op * 1.02
    lo = op * 0.995
    cl = lo + close_frac * (hi - lo)
    vol = float(bars["volume"].iloc[-50:].mean()) * rvol

    row = pd.DataFrame(
        [{"open": op, "high": hi, "low": lo, "close": cl, "volume": vol}],
        index=[bars.index[-1] + pd.offsets.BDay(1)],
    )
    return pd.concat([bars, row])


def spy_frame(bars, *, healthy=True):
    """SPY aligned to `bars`, either above or below its own SMA200."""
    n = len(bars)
    closes = (np.linspace(300, 450, n) if healthy else np.linspace(450, 300, n))
    return pd.DataFrame(
        {"open": closes, "high": closes * 1.01, "low": closes * 0.99,
         "close": closes, "volume": np.full(n, 1e8)},
        index=bars.index,
    )


class Cal:
    """A hand-built earnings calendar with the real one's interface."""
    def __init__(self, dates_by_ticker):
        self._b = {k: [pd.Timestamp(d) for d in v]
                   for k, v in dates_by_ticker.items()}

    def covers(self, t):
        return t.upper() in self._b

    def in_blackout(self, t, d, *, days_before=2, days_after=10):
        ds = self._b.get(t.upper())
        if not ds:
            return False
        d = pd.Timestamp(d)
        return any(d - pd.Timedelta(days=days_before) <= e
                   <= d + pd.Timedelta(days=days_after) for e in ds)


# ─────────────────────────────────────────────────────────────────────
# Gap detection — the three registered cases
# ─────────────────────────────────────────────────────────────────────

class TestGapDetection:

    def test_gap_over_one_atr_and_over_20day_high_is_true(self):
        """Registered case 1."""
        b = append_gap(make_uptrend(), gap_atr=1.5, rvol=3.0, new_high=True)
        assert G.gap_atr(b) >= 1.0
        assert G.clears_prior_high(b) is True
        assert G.is_strong_gap(b) is True

    def test_gap_without_a_new_high_is_false(self):
        """Registered case 2. Big in ATR terms, but the open is still
        inside the recent range — a bounce, not a breakout."""
        b = append_gap(make_uptrend(), gap_atr=1.5, rvol=3.0, new_high=False)
        assert G.clears_prior_high(b) is False
        assert G.is_strong_gap(b) is False

    def test_small_gap_is_false(self):
        b = append_gap(make_uptrend(), gap_atr=0.3, rvol=3.0, new_high=True)
        assert G.gap_atr(b) < 1.0
        assert G.is_strong_gap(b) is False

    def test_gap_without_volume_is_false(self):
        """RVOL >= 2.0 is registered. A gap nobody traded is a quote,
        not a move."""
        b = append_gap(make_uptrend(), gap_atr=1.5, rvol=1.1, new_high=True)
        assert G.is_strong_gap(b) is False

    def test_rvol_threshold_is_inclusive(self):
        """Registered as ">= 2.0", unlike Phase 1B's strict ">"."""
        b = append_gap(make_uptrend(), gap_atr=1.5, rvol=2.0, new_high=True)
        rv = F.relative_volume(b, lookback=G.RVOL_LOOKBACK)
        assert rv == pytest.approx(2.0, rel=1e-6)
        assert G.is_strong_gap(b) is True

    def test_gap_atr_uses_yesterdays_atr_not_todays(self):
        """Including the gap bar's own range in the ATR would shrink the
        measured gap exactly when the gap is biggest — inverting the
        filter. The measure must use the last COMPLETED bar's ATR."""
        b = append_gap(make_uptrend(), gap_atr=2.0, rvol=3.0, new_high=True)
        atr_prior = G._wilder_atr(b.iloc[:-1])
        atr_incl = G._wilder_atr(b)
        assert atr_incl > atr_prior          # the gap bar widened it
        expected = (float(b["open"].iloc[-1]) - float(b["close"].iloc[-2])) / atr_prior
        assert G.gap_atr(b) == pytest.approx(expected)

    def test_prior_high_window_excludes_today(self):
        """A bar's high is always >= its open, so including today would
        make the test nearly impossible to pass."""
        b = append_gap(make_uptrend(), gap_atr=3.0, rvol=3.0, new_high=True)
        assert float(b["high"].iloc[-1]) > float(b["open"].iloc[-1])
        assert G.clears_prior_high(b) is True

    def test_fails_closed_on_short_history(self):
        b = make_uptrend(n=10)
        assert G.gap_atr(b) is None
        assert G.is_strong_gap(b) is False

    def test_fails_closed_on_missing_columns(self):
        b = append_gap(make_uptrend(), gap_atr=1.5).drop(columns=["open"])
        assert G.gap_atr(b) is None
        assert G.is_strong_gap(b) is False


class TestTrendContext:

    def test_uptrend_passes(self):
        b = append_gap(make_uptrend(), gap_atr=1.5, rvol=3.0)
        assert G.trend_context_ok(b) is True

    def test_downtrend_fails(self):
        b = append_gap(make_uptrend(drift=-0.0025), gap_atr=1.5, rvol=3.0)
        assert G.trend_context_ok(b) is False

    def test_context_is_measured_before_the_gap_bar(self):
        """One enormous session must not be able to drag a stock's own
        moving averages into qualifying it."""
        base = make_uptrend(drift=-0.0025)
        huge = append_gap(base, gap_atr=8.0, rvol=5.0)
        assert G.trend_context_ok(huge) is False


# ─────────────────────────────────────────────────────────────────────
# The gate
# ─────────────────────────────────────────────────────────────────────

class TestGate:

    def _qualifying(self, **kw):
        b = append_gap(make_uptrend(), gap_atr=1.5, rvol=3.0, new_high=True)
        return b, dict(spy_hist=spy_frame(b), earnings_calendar=None, **kw)

    def test_qualifying_series_produces_a_plan(self):
        b, kw = self._qualifying()
        s = G.evaluate_bars("TEST", b, **kw)
        assert s.qualifies, s.reject_reason
        assert s.setup_type == "GAP_CONTINUATION"
        assert s.entry_limit is None          # registered: no chase cap
        assert s.target is None               # registered: no fixed target
        assert s.stop < s.last_close

    def test_regime_gate_is_on_by_default(self):
        """Registered ON with no A/B. A soft tape blocks the entry."""
        b = append_gap(make_uptrend(), gap_atr=1.5, rvol=3.0)
        s = G.evaluate_bars("TEST", b, spy_hist=spy_frame(b, healthy=False))
        assert not s.qualifies
        assert "regime" in s.reject_reason

    def test_regime_fails_closed_without_spy(self):
        b = append_gap(make_uptrend(), gap_atr=1.5, rvol=3.0)
        s = G.evaluate_bars("TEST", b, spy_hist=None)
        assert not s.qualifies

    def test_earnings_day_gap_is_excluded(self):
        """Registered case 3, and the load-bearing one: without it, H2
        silently becomes H3 (post-earnings drift), which is a different
        hypothesis with a different counterparty."""
        b, kw = self._qualifying()
        kw["earnings_calendar"] = Cal({"TEST": [b.index[-1]]})
        s = G.evaluate_bars("TEST", b, **kw)
        assert not s.qualifies
        assert s.earnings_in_window
        assert "H3" in s.reject_reason

    def test_earnings_the_day_before_the_gap_is_excluded(self):
        """A report released after yesterday's close causes today's gap."""
        b, kw = self._qualifying()
        kw["earnings_calendar"] = Cal({"TEST": [b.index[-1] - pd.Timedelta(days=1)]})
        assert not G.evaluate_bars("TEST", b, **kw).qualifies

    def test_earnings_inside_the_holding_window_is_excluded(self):
        b, kw = self._qualifying()
        kw["earnings_calendar"] = Cal({"TEST": [b.index[-1] + pd.Timedelta(days=6)]})
        assert not G.evaluate_bars("TEST", b, **kw).qualifies

    def test_earnings_well_outside_the_window_is_allowed(self):
        b, kw = self._qualifying()
        kw["earnings_calendar"] = Cal({"TEST": [b.index[-1] + pd.Timedelta(days=45)]})
        assert G.evaluate_bars("TEST", b, **kw).qualifies

    def test_uncovered_ticker_is_rejected_not_waved_through(self):
        """Applying an earnings filter to some tickers and not others
        biases the sample BY TICKER, which is worse than applying it to
        none. A name the calendar cannot gate does not trade."""
        b, kw = self._qualifying()
        kw["earnings_calendar"] = Cal({"OTHER": [pd.Timestamp("2020-01-01")]})
        s = G.evaluate_bars("TEST", b, **kw)
        assert not s.qualifies
        assert "cobertura" in s.reject_reason

    def test_uncovered_ticker_allowed_when_coverage_not_required(self):
        b, kw = self._qualifying()
        kw["earnings_calendar"] = Cal({"OTHER": []})
        kw["require_earnings_coverage"] = False
        assert G.evaluate_bars("TEST", b, **kw).qualifies

    def test_closed_end_funds_and_pltr_excluded(self):
        b, kw = self._qualifying()
        assert not G.evaluate_bars("GAB", b, **kw).qualifies
        assert not G.evaluate_bars("PLTR", b, **kw).qualifies

    def test_price_floor(self):
        b = append_gap(make_uptrend(base=2.0), gap_atr=1.5, rvol=3.0)
        s = G.evaluate_bars("TEST", b, spy_hist=spy_frame(b))
        assert not s.qualifies
        assert "mínimo" in s.reject_reason


# ─────────────────────────────────────────────────────────────────────
# Fills and exits
# ─────────────────────────────────────────────────────────────────────

class _Setup:
    def __init__(self, atr, setup_type="GAP_CONTINUATION"):
        self.atr14 = atr
        self.setup_type = setup_type
        self.volume_ratio_20d = 3.0
        self._gap_atr = 1.5


def _trade_over(closes, *, atr=2.0, opens=None, lows=None, highs=None,
                costs=None, time_stop=15, entry_open=100.0):
    """Signal bar at index 0; the trade fills at bar 1's open."""
    n = len(closes)
    opens = list(opens) if opens is not None else list(closes)
    lows = list(lows) if lows is not None else [c * 0.99 for c in closes]
    highs = list(highs) if highs is not None else [c * 1.01 for c in closes]
    opens[0] = entry_open
    rows = [{"open": 99, "high": 100, "low": 98, "close": 99, "volume": 1e6}]
    for i in range(n):
        rows.append({"open": opens[i], "high": highs[i], "low": lows[i],
                     "close": closes[i], "volume": 1e6})
    return H2.simulate_h2_trade(frame(rows), 0, _Setup(atr), ticker="T",
                                costs=costs, time_stop_bars=time_stop)


class TestFill:

    def test_fills_at_the_next_open(self):
        t = _trade_over([101, 102], entry_open=100.0)
        assert t.fill_price == pytest.approx(100.0)
        assert t.entry_index == 1

    def test_market_entry_slips_adversely(self):
        """H2's premise is that the bad fill is the cost of the signal,
        so the fill has to be modelled as genuinely bad."""
        c = CostModel(slippage_bps=100.0, tick_size=0.01)
        t = _trade_over([101, 102], entry_open=100.0, costs=c)
        assert t.fill_price > 100.0

    def test_stop_is_one_and_a_half_atr_below_the_fill(self):
        t = _trade_over([101, 102], atr=2.0, entry_open=100.0)
        assert t.stop_price == pytest.approx(97.0)
        assert t.risk_per_share == pytest.approx(3.0)

    def test_no_next_bar_means_no_trade(self):
        rows = [{"open": 99, "high": 100, "low": 98, "close": 99, "volume": 1e6}]
        assert H2.simulate_h2_trade(frame(rows), 0, _Setup(2.0), ticker="T") is None


class TestExits:

    def test_trail_fires_on_the_first_close_below_sma10(self):
        """Registered primary exit. Ten closes at 100 put SMA10 at 100;
        the eleventh at 95 is the first below it."""
        t = _trade_over([100] * 10 + [95],
                        lows=[99] * 10 + [94], highs=[101] * 11,
                        entry_open=100.0, atr=10.0)
        assert t.exit_reason == "TRAIL_SMA"
        assert t.exit_price == pytest.approx(95.0)
        assert t.bars_held == 10

    def test_trail_does_not_fire_at_or_above_the_average(self):
        t = _trade_over([100] * 12, lows=[99] * 12, highs=[101] * 12,
                        entry_open=100.0, atr=10.0)
        assert t.exit_reason != "TRAIL_SMA"

    def test_sma_helper_uses_only_closes_up_to_the_exit_bar(self):
        arr = np.array([1.0] * 10 + [99.0])
        assert H2._sma_at(arr, 9, 10) == pytest.approx(1.0)    # ignores index 10
        assert H2._sma_at(arr, 8, 10) is None

    def test_hard_stop_fires(self):
        """Opens above the stop, then trades down through it — the fill
        is the stop itself, not the open. (The open must sit inside its
        own bar; an open below the low is a malformed fixture, and the
        gap-down case is the next test.)"""
        t = _trade_over([100, 97.5], lows=[99, 96.9], highs=[101, 100],
                        opens=[100, 98], entry_open=100.0, atr=2.0)
        assert t.exit_reason == "STOP"
        assert t.exit_price == pytest.approx(97.0)
        assert t.r_multiple == pytest.approx(-1.0)

    def test_stop_gap_down_fills_at_the_open(self):
        t = _trade_over([100, 92], lows=[99, 91], highs=[101, 93],
                        opens=[100, 93], entry_open=100.0, atr=2.0)
        assert t.exit_reason == "STOP"
        assert t.exit_price == pytest.approx(93.0)
        assert t.r_multiple < -1.0

    def test_stop_wins_when_one_bar_spans_both_triggers(self):
        t = _trade_over([100] * 10 + [95],
                        lows=[99] * 10 + [90], highs=[101] * 11,
                        entry_open=100.0, atr=2.0)
        assert t.exit_reason in ("STOP", "BREAKEVEN_STOP")

    def test_time_stop_fires_at_fifteen_bars(self):
        t = _trade_over([100] * 25, lows=[99.5] * 25, highs=[100.5] * 25,
                        entry_open=100.0, atr=20.0)
        assert t.exit_reason == "TIME_STOP"
        assert t.bars_held == 15

    def test_breakeven_ratchet_saves_a_winner_from_becoming_a_loser(self):
        """Registered at +1R. Run to +1R, then collapse: the trade must
        exit at breakeven, not at the original stop."""
        t = _trade_over([100, 104, 100, 99],
                        lows=[99.5, 100, 99, 96],
                        highs=[100.5, 104, 101, 100],
                        entry_open=100.0, atr=2.0)
        assert t.mfe_r >= 1.0
        assert t.exit_reason == "BREAKEVEN_STOP"
        assert t.r_multiple == pytest.approx(0.0, abs=1e-9)

    def test_ratchet_is_end_of_bar_not_intrabar(self):
        """A bar that touches +1R and then reverses to below the entry in
        the SAME session must NOT be rescued — we could not have moved
        the stop while that bar was still printing."""
        t = _trade_over([100, 97],
                        lows=[99.5, 96.9], highs=[100.5, 104],
                        entry_open=100.0, atr=2.0)
        assert t.exit_reason == "STOP"
        assert t.r_multiple == pytest.approx(-1.0)

    def test_no_fixed_target_lets_winners_run(self):
        """Registered out: "strength strategies die by capping winners"."""
        t = _trade_over([100, 105, 110, 115, 120] + [125] * 8,
                        lows=[99.5, 104, 109, 114, 119] + [124] * 8,
                        highs=[100.5, 106, 111, 116, 121] + [126] * 8,
                        entry_open=100.0, atr=2.0)
        assert t.r_multiple > 2.0

    def test_unresolved_trade_is_labelled_end_of_data(self):
        t = _trade_over([100, 101], lows=[99.5, 100], highs=[100.5, 102],
                        entry_open=100.0, atr=20.0)
        assert t.exit_reason == "END_OF_DATA"


# ─────────────────────────────────────────────────────────────────────
# Replay, look-ahead, verdict
# ─────────────────────────────────────────────────────────────────────

def make_repeated_gaps(n=900, *, every=90, seed=11, gap_atr=1.6, rvol=3.0):
    """An uptrend punctuated by a gap-up every `every` bars.

    A fixture whose only signal sits on the final bar makes the
    look-ahead check vacuous — it can only compare trades that CLOSED
    before the original data ended. Phase 1B's confirmation came back
    INCONCLUSIVE for exactly that reason.
    """
    b = make_uptrend(n=n, seed=seed)
    o, h, l, c, v = (b["open"].to_numpy().copy(), b["high"].to_numpy().copy(),
                     b["low"].to_numpy().copy(), b["close"].to_numpy().copy(),
                     b["volume"].to_numpy().copy())
    for i in range(260, n, every):
        atr = float(np.mean(h[i - 14:i] - l[i - 14:i]))
        prior_high = float(h[i - 20:i].max())
        op = max(c[i - 1] + gap_atr * atr, prior_high * 1.002)
        o[i] = op
        h[i] = op * 1.02
        l[i] = op * 0.995
        c[i] = op * 1.015
        v[i] = float(np.mean(v[i - 50:i])) * rvol
        # Carry the new level forward so the series does not snap back.
        scale = c[i] / c[i - 1]
        o[i + 1:] *= scale
        h[i + 1:] *= scale
        l[i + 1:] *= scale
        c[i + 1:] *= scale
    return pd.DataFrame({"open": o, "high": h, "low": l, "close": c,
                         "volume": v}, index=b.index)


class TestReplay:

    def _prices(self, tickers=("AAA", "BBB")):
        frames = []
        for k, t in enumerate(tickers):
            b = make_repeated_gaps(seed=11 + k)
            f = b.reset_index(names="date")
            f["ticker"] = t
            frames.append(f)
        spy = spy_frame(frames[0].set_index("date"))
        sf = spy.reset_index(names="date")
        sf["ticker"] = "SPY"
        frames.append(sf)
        return pd.concat(frames, ignore_index=True)

    def test_replay_produces_trades_and_a_census(self):
        b = make_repeated_gaps()
        census: dict = {}
        trades = H2.replay_ticker_h2(b, "AAA", spy_bars=spy_frame(b),
                                     census=census)
        assert census["bars"] > 0
        assert len(trades) >= 3, "fixture must produce several trades"
        assert census.get("signal", 0) >= len(trades)

    def test_one_position_per_ticker_at_a_time(self):
        b = make_repeated_gaps()
        trades = H2.replay_ticker_h2(b, "AAA", spy_bars=spy_frame(b))
        assert len(trades) >= 3
        for a, c in zip(trades, trades[1:]):
            assert c.entry_index > a.exit_index

    def test_no_lookahead(self):
        res = H2.confirm_no_lookahead_h2(self._prices(), n_tickers=2)
        assert res["passed"], res["mismatches"]
        # An assertion over zero trades is not a check.
        assert res["trades_compared"] >= 2, res["note"]


class TestGateMachinery:

    def test_h2_uses_the_unmodified_default_gate(self):
        from core.protocol_gate import DEFAULT_GATE
        assert H2.H2_GATE == DEFAULT_GATE

    def test_below_the_sample_floor_is_insufficient(self):
        v = H2.evaluate_h2_gate(
            {"n_trades": 50, "expectancy_r": -0.3, "profit_factor": 0.5},
            {"n_trades": 30, "expectancy_r": -0.3},
            {"n_trades": 20, "expectancy_r": -0.3})
        assert v["decision"] == "INSUFFICIENT-SAMPLE"

    def test_negative_expectancy_is_fail(self):
        v = H2.evaluate_h2_gate(
            {"n_trades": 500, "expectancy_r": -0.05, "profit_factor": 0.9},
            {"n_trades": 350, "expectancy_r": -0.05},
            {"n_trades": 150, "expectancy_r": -0.05})
        assert v["decision"] == "FAIL"

    def test_positive_but_under_threshold_is_no_edge(self):
        v = H2.evaluate_h2_gate(
            {"n_trades": 500, "expectancy_r": 0.04, "profit_factor": 1.05},
            {"n_trades": 350, "expectancy_r": 0.05},
            {"n_trades": 150, "expectancy_r": 0.03})
        assert v["decision"] == "NO-EDGE"

    def test_sign_flip_is_fail(self):
        v = H2.evaluate_h2_gate(
            {"n_trades": 500, "expectancy_r": 0.15, "profit_factor": 1.3},
            {"n_trades": 350, "expectancy_r": 0.30},
            {"n_trades": 150, "expectancy_r": -0.10})
        assert v["decision"] == "FAIL"

    def test_survivor_downgrades_pass_to_marginal(self):
        v = H2.evaluate_h2_gate(
            {"n_trades": 500, "expectancy_r": 0.15, "profit_factor": 1.3},
            {"n_trades": 350, "expectancy_r": 0.16},
            {"n_trades": 150, "expectancy_r": 0.13},
            provenance={"universe_type": "SURVIVOR", "n_delisted": 0,
                        "n_tickers": 395})
        assert v["decision"] == "MARGINAL"

    def test_lookahead_failure_invalidates(self):
        v = H2.evaluate_h2_gate(
            {"n_trades": 500, "expectancy_r": 0.9, "profit_factor": 3.0},
            {"n_trades": 350, "expectancy_r": 0.9},
            {"n_trades": 150, "expectancy_r": 0.9},
            lookahead={"passed": False, "mismatches": ["AAA"]})
        assert v["decision"] == "INVALID"


class TestEarningsCalendar:
    """The real calendar class, against a temporary parquet."""

    def _cal(self, tmp_path, rows):
        p = tmp_path / "ed.parquet"
        pd.DataFrame(rows).to_parquet(p, index=False)
        return F.EarningsCalendar.load(p)

    def test_loads_and_reports_coverage(self, tmp_path):
        cal = self._cal(tmp_path, [{"ticker": "AAA", "date": "2024-02-01"}])
        assert cal.covers("AAA") and cal.covers("aaa")
        assert not cal.covers("BBB")
        assert cal.n_tickers == 1

    def test_blackout_window_is_inclusive_on_both_legs(self, tmp_path):
        cal = self._cal(tmp_path, [{"ticker": "AAA", "date": "2024-02-10"}])
        assert cal.in_blackout("AAA", "2024-02-08")     # 2 days before
        assert cal.in_blackout("AAA", "2024-02-10")     # the day itself
        assert cal.in_blackout("AAA", "2024-01-31")     # 10 days after
        assert not cal.in_blackout("AAA", "2024-02-13")
        assert not cal.in_blackout("AAA", "2024-01-30")

    def test_uncovered_ticker_is_never_in_blackout(self, tmp_path):
        """`covers()` is the honesty valve — absence must be visible to
        the caller rather than silently reported as 'no earnings'."""
        cal = self._cal(tmp_path, [{"ticker": "AAA", "date": "2024-02-10"}])
        assert not cal.in_blackout("ZZZ", "2024-02-10")
        assert not cal.covers("ZZZ")

    def test_missing_file_yields_an_empty_calendar(self, tmp_path):
        cal = F.EarningsCalendar.load(tmp_path / "nope.parquet")
        assert cal.n_tickers == 0


class TestOtherStrategiesUntouched:

    def test_all_four_strategies_resolve(self):
        from strategies import get_strategy
        for n in ("trend_pullback", "dip_in_uptrend",
                  "panic_reversion", "gap_continuation"):
            assert get_strategy(n) is not None
