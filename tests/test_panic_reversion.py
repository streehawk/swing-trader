"""tests/test_panic_reversion.py — H1 acceptance tests.

Covers exactly what edge_search_protocol.md registers under H1's
Acceptance clause:

    "unit tests for the panic condition (deep multi-day drop above SMA200
     = True; single-day drop = False; drop below SMA200 = False),
     limit-fill logic (open below limit fills at open; never fills above
     limit), SMA5 exit."

plus the guardrails the protocol imposes globally: fail-closed on missing
data, no look-ahead, the closed-end-fund exclusion (the pre-registered
Phase 1B postmortem fix), and the verdict machinery — including that the
trade-count floor produces INSUFFICIENT-SAMPLE rather than FAIL.

Synthetic OHLCV throughout — no network.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from core import h1_backtest as H
from core.swing_backtest import CostModel
from strategies import filters as F
from strategies import panic_reversion as P


# ── Builders ─────────────────────────────────────────────────────────

def bars_from(closes, *, volume=5_000_000.0, band=0.01, highs=None, lows=None,
              opens=None):
    closes = np.asarray(closes, dtype=float)
    n = len(closes)
    vol = np.full(n, volume, float) if np.isscalar(volume) else np.asarray(volume, float)
    return pd.DataFrame(
        {
            "open":   closes * 0.999 if opens is None else np.asarray(opens, float),
            "high":   closes * (1 + band) if highs is None else np.asarray(highs, float),
            "low":    closes * (1 - band) if lows is None else np.asarray(lows, float),
            "close":  closes,
            "volume": vol,
        },
        index=pd.date_range("2024-01-01", periods=n, freq="B"),
    )


def make_panic(n=260, *, base=100.0, drift=0.0022, drop_days=4, drop_rate=0.03,
               band=0.012, seed=3, noise=0.003):
    """A slow uptrend, then a multi-day flush at the end.

    Deliberately built from a real-ish price path rather than from target
    SMAs: the panic condition is a joint statement about ATR, the 10-day
    high and the last four closes, and a series constructed to satisfy
    one of those in isolation tends to violate another.

    `drift` has to out-run the flush: a ~12% drop must still leave the
    close above a 200-day mean that trails the price by ~100 bars of
    trend. At 0.0022/day the close sits ~25% above SMA200 before the
    drop, so the fixture tests the panic rule rather than the trend rule.
    """
    rng = np.random.default_rng(seed)
    closes = np.empty(n, float)
    px = base
    for i in range(n):
        px *= (1 + drift + rng.normal(0, noise))
        closes[i] = px
    for k in range(drop_days):
        closes[n - drop_days + k] = closes[n - drop_days - 1] * (1 - drop_rate) ** (k + 1)
    return bars_from(closes, band=band)


def make_repeated_panics(n=900, *, base=100.0, drift=0.003, every=120,
                         drop_days=4, drop_rate=0.03, band=0.012, seed=7,
                         noise=0.003):
    """An uptrend punctuated by a flush every `every` bars.

    `make_panic` puts its single flush at the very end of the series,
    which is useless for the look-ahead check: that check can only
    compare trades that CLOSED before the original data ended, and a
    trade opened on the last bar has not closed. A fixture that produces
    only trailing trades makes the look-ahead assertion vacuously true —
    exactly the failure mode the Phase 1B run hit and reported as
    INCONCLUSIVE.

    `drift` must out-run the flushes with room to spare: each one cuts
    ~12%, and if the trend cannot recover that before the next arrives
    the series spends most of its life under SMA200, where H1 correctly
    refuses to trade. At 0.3%/day and one flush per 120 bars the price
    is back above its 200-day mean well before the next flush lands.
    """
    rng = np.random.default_rng(seed)
    closes = np.empty(n, float)
    px = base
    for i in range(n):
        px *= (1 + drift + rng.normal(0, noise))
        closes[i] = px
    for start in range(250, n - drop_days, every):
        anchor = closes[start - 1]
        for k in range(drop_days):
            closes[start + k] = anchor * (1 - drop_rate) ** (k + 1)
        # Re-base the rest of the path onto the post-flush level so the
        # recovery starts from where the flush ended rather than snapping
        # back to the old trend line in one bar.
        closes[start + drop_days:] *= (closes[start + drop_days - 1] /
                                       closes[start + drop_days]) * (1 + drift)
    return bars_from(closes, band=band)


def _atr(bars):
    return P._wilder_atr(bars)


# ─────────────────────────────────────────────────────────────────────
# The panic condition — the three registered cases
# ─────────────────────────────────────────────────────────────────────

class TestPanicCondition:

    def test_deep_multi_day_drop_above_sma200_is_true(self):
        """Registered case 1: the shape H1 is looking for."""
        b = make_panic()
        assert P.is_panic_drop(b) is True

    def test_single_day_drop_is_false(self):
        """Registered case 2. Same total depth, delivered in ONE session.

        This is the case that separates H1 from "buy any big red day":
        one bad print is usually news being priced correctly, and there
        is no forced-seller cascade behind it.
        """
        b = make_panic(drop_days=1, drop_rate=0.115)
        atr = _atr(b)
        closes = b["close"].to_numpy()
        depth = (closes[-10:].max() - closes[-1]) / atr
        assert depth >= P.PANIC_ATR_MULT, (
            f"fixture must be deep enough to isolate the down-days rule "
            f"(got {depth:.2f} ATR)"
        )
        assert P.count_down_days(b) < P.DOWN_DAYS_MIN
        assert P.is_panic_drop(b) is False

    def test_drop_below_sma200_is_false(self):
        """Registered case 3. A structural decline is not a panic.

        Without this filter the strategy buys names in downtrends, where
        every bounce is sold and "reverting to the mean" means reverting
        to lower. That is the documented way this family dies.
        """
        b = make_panic(drift=-0.0025, drop_days=4, drop_rate=0.03)
        closes = b["close"]
        assert float(closes.iloc[-1]) < float(closes.iloc[-200:].mean())
        assert P.is_panic_drop(b) is False

    def test_shallow_multi_day_drift_is_false(self):
        """Four small down days is not a flush."""
        b = make_panic(drop_days=4, drop_rate=0.001)
        assert P.count_down_days(b) >= P.DOWN_DAYS_MIN
        assert P.is_panic_drop(b) is False

    def test_lookback_window_includes_the_signal_day(self):
        """A stock that already bounced today must not read as panicked."""
        b = make_panic()
        assert P.is_panic_drop(b) is True
        closes = b["close"].to_numpy().copy()
        closes[-1] = closes[-11:-1].max()      # today recovers to the window high
        assert P.is_panic_drop(bars_from(closes, band=0.012)) is False

    def test_fails_closed_on_short_history(self):
        assert P.is_panic_drop(bars_from(np.linspace(100, 90, 40))) is False

    def test_fails_closed_on_missing_close(self):
        b = make_panic().drop(columns=["close"])
        assert P.is_panic_drop(b) is False

    def test_fails_closed_on_nan_in_window(self):
        b = make_panic()
        b.iloc[-3, b.columns.get_loc("close")] = np.nan
        assert P.is_panic_drop(b) is False


class TestDownDayCount:

    def test_counts_closes_below_prior_close(self):
        b = bars_from([10, 11, 10.5, 10.2, 9.9])       # 3 of the last 4 down
        assert P.count_down_days(b) == 3

    def test_first_of_window_is_judged_against_the_bar_before_it(self):
        """A 4-day window needs 5 closes. Judging the first bar against
        itself would silently make every window one day shorter."""
        b = bars_from([10, 9, 9, 9, 9])
        assert P.count_down_days(b) == 1

    def test_none_not_zero_when_history_too_short(self):
        assert P.count_down_days(bars_from([10, 9, 8])) is None

    def test_flat_close_is_not_a_down_day(self):
        b = bars_from([10, 10, 10, 10, 10])
        assert P.count_down_days(b) == 0


# ─────────────────────────────────────────────────────────────────────
# The plan
# ─────────────────────────────────────────────────────────────────────

class TestPlan:

    def test_limit_is_half_an_atr_below_the_signal_close(self):
        b = make_panic()
        plan = P.build_plan(b)
        atr = _atr(b)
        assert plan["entry_limit"] == pytest.approx(
            float(b["close"].iloc[-1]) - 0.5 * atr, abs=1e-3)

    def test_nominal_stop_is_two_atr_below_the_limit(self):
        b = make_panic()
        plan = P.build_plan(b)
        assert plan["stop"] == pytest.approx(
            plan["entry_limit"] - 2.0 * plan["atr14"], abs=1e-3)

    def test_no_target_is_produced(self):
        """Registered out. A target would reintroduce the 2R shape that
        is structurally wrong for mean reversion."""
        setup = P.evaluate_bars("TEST", make_panic(), check_earnings=False)
        assert setup.qualifies
        assert setup.target is None
        assert setup.rr_ratio is None

    def test_time_stop_is_seven_bars(self):
        setup = P.evaluate_bars("TEST", make_panic(), check_earnings=False)
        assert setup.time_stop_days == 7


# ─────────────────────────────────────────────────────────────────────
# The gate
# ─────────────────────────────────────────────────────────────────────

class TestGate:

    def test_qualifying_series_produces_a_plan(self):
        setup = P.evaluate_bars("TEST", make_panic(), check_earnings=False)
        assert setup.qualifies
        assert setup.setup_type == "PANIC_REVERSION"
        assert setup.entry_limit < setup.last_close
        assert setup.stop < setup.entry_limit

    def test_price_floor_rejects_sub_ten_dollar_names(self):
        b = make_panic(base=4.0)
        setup = P.evaluate_bars("TEST", b, check_earnings=False)
        assert not setup.qualifies
        assert "mínimo" in setup.reject_reason

    def test_liquidity_floor_rejects_thin_names(self):
        b = make_panic()
        b["volume"] = 100_000.0
        setup = P.evaluate_bars("TEST", b, check_earnings=False)
        assert not setup.qualifies
        assert "volume" in setup.reject_reason

    def test_closed_end_funds_are_excluded(self):
        """Pre-registered Phase 1B postmortem fix, not tuning."""
        setup = P.evaluate_bars("GAB", make_panic(), check_earnings=False)
        assert not setup.qualifies
        assert "closed-end" in setup.reject_reason

    def test_pltr_is_excluded(self):
        setup = P.evaluate_bars("PLTR", make_panic(), check_earnings=False)
        assert not setup.qualifies

    def test_leveraged_etf_is_excluded(self):
        setup = P.evaluate_bars("TQQQ", make_panic(), check_earnings=False)
        assert not setup.qualifies

    def test_regime_argument_is_accepted_and_ignored(self):
        """H1 registers the regime gate OFF with no A/B. Accepting the
        argument keeps the common signature; ignoring it means no caller
        can quietly switch the gate on after seeing a result."""
        b = make_panic()
        a = P.evaluate_bars("TEST", b, check_earnings=False)
        c = P.evaluate_bars("TEST", b, check_earnings=False, regime_ok=False)
        assert a.qualifies and c.qualifies
        assert a.entry_limit == c.entry_limit

    def test_short_history_rejected(self):
        setup = P.evaluate_bars("TEST", bars_from(np.linspace(100, 95, 100)),
                                check_earnings=False)
        assert not setup.qualifies
        assert "histórico" in setup.reject_reason


# ─────────────────────────────────────────────────────────────────────
# Limit-fill logic — the two registered cases
# ─────────────────────────────────────────────────────────────────────

class _Setup:
    """Minimal stand-in for SwingSetup, so fill tests state their own
    numbers instead of reverse-engineering them out of a fixture."""
    def __init__(self, limit, atr, setup_type="PANIC_REVERSION"):
        self.entry = self.entry_limit = limit
        self.atr14 = atr
        self.setup_type = setup_type


def _one_bar_trade(entry_bar, follow=None, *, limit=100.0, atr=2.0, costs=None):
    """Signal bar + entry bar (+ optional follow-on bars)."""
    rows = [{"open": 110, "high": 111, "low": 109, "close": 110, "volume": 1e6},
            entry_bar]
    rows += (follow or [])
    bars = pd.DataFrame(rows, index=pd.date_range("2024-01-01", periods=len(rows), freq="B"))
    return H.simulate_h1_trade(bars, 0, _Setup(limit, atr), ticker="T", costs=costs)


class TestLimitFill:

    def test_open_below_limit_fills_at_the_open(self):
        """Registered case 1. A resting buy limit at 100 with the bar
        opening at 97 is filled at 97, not at 100 — you do not pay your
        limit when the market gaps past it in your favour."""
        t = _one_bar_trade({"open": 97, "high": 99, "low": 96, "close": 98, "volume": 1e6})
        assert t is not None
        assert t.fill_price == pytest.approx(97.0)

    def test_never_fills_above_the_limit(self):
        """Registered case 2. The bar trades entirely above 100, so the
        order rests untouched and there is NO trade. Not a scratch, not a
        zero — the absence of a trade."""
        t = _one_bar_trade({"open": 104, "high": 108, "low": 101, "close": 107, "volume": 1e6})
        assert t is None

    def test_touching_the_limit_fills_at_the_limit(self):
        t = _one_bar_trade({"open": 103, "high": 104, "low": 99.5, "close": 102, "volume": 1e6})
        assert t is not None
        assert t.fill_price == pytest.approx(100.0)

    def test_limit_fill_is_not_charged_slippage(self):
        """A limit fills at your price or better, or not at all. Charging
        adverse slippage on it would overstate costs."""
        c = CostModel(slippage_bps=50.0, tick_size=0.01)
        t = _one_bar_trade({"open": 103, "high": 104, "low": 99.5, "close": 102, "volume": 1e6},
                           costs=c)
        assert t.fill_price == pytest.approx(100.0)

    def test_order_expires_after_one_bar(self):
        """Registered: "Order expires end of day." A bar that never reaches
        the limit produces no trade even if a LATER bar would have."""
        t = _one_bar_trade(
            {"open": 104, "high": 108, "low": 101, "close": 107, "volume": 1e6},
            follow=[{"open": 99, "high": 100, "low": 90, "close": 95, "volume": 1e6}],
        )
        assert t is None

    def test_stop_is_anchored_to_the_fill_not_the_limit(self):
        """Registered: "2 x ATR below fill". A gap-down fill must not be
        allowed to manufacture a bigger R by widening the denominator."""
        t = _one_bar_trade({"open": 97, "high": 99, "low": 96, "close": 98, "volume": 1e6},
                           limit=100.0, atr=2.0)
        assert t.fill_price == pytest.approx(97.0)
        assert t.stop_price == pytest.approx(93.0)
        assert t.risk_per_share == pytest.approx(4.0)   # always exactly 2 x ATR


# ─────────────────────────────────────────────────────────────────────
# Exits
# ─────────────────────────────────────────────────────────────────────

def _trade_over(closes, *, limit=100.0, atr=2.0, lows=None, highs=None,
                opens=None, costs=None, time_stop=7):
    """Signal bar, then a path. Bar 1 always fills at the limit."""
    n = len(closes)
    lows = lows if lows is not None else [c * 0.99 for c in closes]
    highs = highs if highs is not None else [c * 1.01 for c in closes]
    opens = opens if opens is not None else list(closes)
    rows = [{"open": 110, "high": 111, "low": 109, "close": 110, "volume": 1e6}]
    for i in range(n):
        rows.append({"open": opens[i], "high": highs[i], "low": lows[i],
                     "close": closes[i], "volume": 1e6})
    rows[1]["low"] = min(rows[1]["low"], limit - 0.5)
    rows[1]["open"] = max(rows[1]["open"], limit)
    bars = pd.DataFrame(rows, index=pd.date_range("2024-01-01", periods=len(rows), freq="B"))
    return H.simulate_h1_trade(bars, 0, _Setup(limit, atr), ticker="T",
                               costs=costs, time_stop_bars=time_stop)


class TestExits:

    def test_sma5_exit_fires_on_the_first_close_above_sma5(self):
        """Registered primary exit. Five flat closes at 100 put SMA5 at
        100; the sixth close at 105 is the first above it."""
        t = _trade_over([100, 100, 100, 100, 100, 105])
        assert t.exit_reason == "SMA_EXIT"
        assert t.exit_price == pytest.approx(105.0)
        assert t.bars_held == 5

    def test_sma5_exit_does_not_fire_at_or_below_the_average(self):
        """Strictly above. A close exactly at SMA5 has not reverted."""
        t = _trade_over([100, 100, 100, 100, 100, 100])
        assert t.exit_reason != "SMA_EXIT"

    def test_sma5_uses_only_closes_up_to_and_including_the_exit_bar(self):
        """The no-look-ahead guarantee of the exit rule, tested directly."""
        arr = np.array([1.0, 2.0, 3.0, 4.0, 5.0, 99.0])
        assert H._sma_at(arr, 4, 5) == pytest.approx(3.0)     # ignores index 5
        assert H._sma_at(arr, 3, 5) is None                   # not enough history

    def test_hard_stop_fires_two_atr_below_fill(self):
        t = _trade_over([100, 99], lows=[99, 95.9], highs=[101, 100])
        assert t.exit_reason == "STOP"
        assert t.exit_price == pytest.approx(96.0)
        assert t.r_multiple == pytest.approx(-1.0)

    def test_stop_gap_down_fills_at_the_open_not_the_stop(self):
        t = _trade_over([100, 92], lows=[99, 91], highs=[101, 93], opens=[100, 93])
        assert t.exit_reason == "STOP"
        assert t.exit_price == pytest.approx(93.0)
        assert t.r_multiple < -1.0

    def test_stop_wins_when_one_bar_spans_both_triggers(self):
        """The pessimistic intrabar assumption, stated as a test."""
        t = _trade_over([100, 100, 100, 100, 100, 106],
                        lows=[99, 99, 99, 99, 99, 95.5],
                        highs=[101, 101, 101, 101, 101, 107])
        assert t.exit_reason == "STOP"

    def test_time_stop_fires_at_seven_bars(self):
        flat = [100] * 12
        t = _trade_over(flat, lows=[97] * 12, highs=[100.5] * 12)
        assert t.exit_reason == "TIME_STOP"
        assert t.bars_held == 7

    def test_max_bars_never_binds_under_a_seven_bar_time_stop(self):
        t = _trade_over([100] * 25, lows=[97] * 25, highs=[100.5] * 25)
        assert t.bars_held <= 7

    def test_no_breakeven_ratchet(self):
        """Registered out. A trade that runs to +1.5R and then reverses to
        the original stop must lose a full 1R, not scratch."""
        t = _trade_over([100, 106, 100, 99],
                        lows=[99, 100, 99, 95.9], highs=[101, 107, 101, 100])
        assert t.exit_reason == "STOP"
        assert t.mfe_r > 1.0
        assert t.r_multiple == pytest.approx(-1.0)

    def test_exit_slippage_is_charged_on_the_stop(self):
        c = CostModel(slippage_bps=100.0, tick_size=0.01)
        t = _trade_over([100, 99], lows=[99, 95.9], highs=[101, 100], costs=c)
        assert t.exit_price < 96.0
        assert t.r_multiple < -1.0

    def test_unresolved_trade_is_labelled_end_of_data(self):
        t = _trade_over([100, 100], lows=[99, 99], highs=[100.5, 100.5], time_stop=7)
        assert t.exit_reason == "END_OF_DATA"


# ─────────────────────────────────────────────────────────────────────
# Replay, look-ahead, verdict
# ─────────────────────────────────────────────────────────────────────

class TestReplay:

    def _prices(self, tickers=("AAA", "BBB")):
        frames = []
        for k, t in enumerate(tickers):
            b = make_repeated_panics(seed=7 + k)
            f = b.reset_index().rename(columns={"index": "date"})
            f["ticker"] = t
            frames.append(f)
        return pd.concat(frames, ignore_index=True)

    def test_replay_produces_trades_and_a_census(self):
        census: dict = {}
        trades = H.replay_ticker_h1(make_repeated_panics(), "AAA", census=census)
        assert census["bars"] > 0
        assert len(trades) >= 3, "fixture must produce several trades to be worth asserting on"
        assert census.get("signal", 0) >= len(trades)
        assert census["filled"] + census.get("unfilled", 0) == census["signal"]

    def test_one_position_per_ticker_at_a_time(self):
        trades = H.replay_ticker_h1(make_repeated_panics(), "AAA")
        assert len(trades) >= 3
        for a, c in zip(trades, trades[1:]):
            assert c.entry_index > a.exit_index

    def test_no_lookahead(self):
        res = H.confirm_no_lookahead_h1(self._prices(), n_tickers=2)
        assert res["passed"], res["mismatches"]
        # An assertion over zero trades is not a check. Phase 1B's
        # look-ahead confirmation came back INCONCLUSIVE for exactly this
        # reason, so the test refuses to pass vacuously.
        assert res["trades_compared"] >= 2, res["note"]


class TestSplitAndGate:

    def _t(self, date, r):
        return H.H1Trade(
            ticker="T", setup_type="PANIC_REVERSION", signal_date=date,
            entry_date=date, exit_date=date, entry_index=0, exit_index=1,
            bars_held=1, signal_close=100.0, plan_limit=99.0, fill_price=99.0,
            stop_price=95.0, exit_price=99.0, atr14=2.0, risk_per_share=4.0,
            r_multiple=r, mfe_r=0.0, mae_r=0.0, exit_reason="TIME_STOP",
            year=int(date[:4]),
        )

    def test_split_is_by_date_not_by_trade_count(self):
        """Ten trades in 2021 and one in 2025 must NOT split 7/4."""
        trades = [self._t("2021-01-04", 0.1) for _ in range(10)]
        trades.append(self._t("2025-01-06", 0.1))
        train, test, cut = H.split_trades(trades, 0.70)
        assert len(train) == 10 and len(test) == 1
        assert cut.startswith("2023")

    def test_below_the_sample_floor_is_insufficient_not_fail(self):
        """Protocol global rule 7. A 50-trade sample says nothing, and
        calling it FAIL would claim knowledge the data does not carry."""
        stats = {"n_trades": 50, "expectancy_r": -0.3, "profit_factor": 0.5,
                 "avg_loss_r": -1.0}
        v = H.evaluate_h1_gate(stats, {"n_trades": 30, "expectancy_r": -0.3},
                               {"n_trades": 20, "expectancy_r": -0.3})
        assert v["decision"] == "INSUFFICIENT-SAMPLE"

    def test_negative_expectancy_is_fail(self):
        stats = {"n_trades": 500, "expectancy_r": -0.05, "profit_factor": 0.9,
                 "avg_loss_r": -1.0}
        v = H.evaluate_h1_gate(stats, {"n_trades": 350, "expectancy_r": -0.05},
                               {"n_trades": 150, "expectancy_r": -0.05})
        assert v["decision"] == "FAIL"

    def test_positive_but_under_threshold_is_no_edge(self):
        stats = {"n_trades": 500, "expectancy_r": 0.04, "profit_factor": 1.05,
                 "avg_loss_r": -1.0}
        v = H.evaluate_h1_gate(stats, {"n_trades": 350, "expectancy_r": 0.05},
                               {"n_trades": 150, "expectancy_r": 0.03})
        assert v["decision"] == "NO-EDGE"

    def test_sign_flip_between_splits_is_fail(self):
        stats = {"n_trades": 500, "expectancy_r": 0.15, "profit_factor": 1.3,
                 "avg_loss_r": -1.0}
        v = H.evaluate_h1_gate(stats, {"n_trades": 350, "expectancy_r": 0.30},
                               {"n_trades": 150, "expectancy_r": -0.10})
        assert v["decision"] == "FAIL"

    def test_survivor_universe_downgrades_pass_to_marginal(self):
        """Protocol rule 5: PASS requires point-in-time data."""
        stats = {"n_trades": 500, "expectancy_r": 0.15, "profit_factor": 1.3,
                 "avg_loss_r": -1.0}
        v = H.evaluate_h1_gate(
            stats, {"n_trades": 350, "expectancy_r": 0.16},
            {"n_trades": 150, "expectancy_r": 0.13},
            provenance={"universe_type": "SURVIVOR", "n_delisted": 0, "n_tickers": 229},
        )
        assert v["decision"] == "MARGINAL"

    def test_lookahead_failure_invalidates_everything(self):
        stats = {"n_trades": 500, "expectancy_r": 0.9, "profit_factor": 3.0,
                 "avg_loss_r": -1.0}
        v = H.evaluate_h1_gate(
            stats, {"n_trades": 350, "expectancy_r": 0.9},
            {"n_trades": 150, "expectancy_r": 0.9},
            lookahead={"passed": False, "mismatches": ["AAA"]},
        )
        assert v["decision"] == "INVALID"

    def test_avg_loss_kill_switch_is_flagged(self):
        """H1's registered known failure mode: stops gapping badly."""
        stats = {"n_trades": 500, "expectancy_r": 0.02, "profit_factor": 1.02,
                 "avg_loss_r": -1.8}
        v = H.evaluate_h1_gate(stats, {"n_trades": 350, "expectancy_r": 0.02},
                               {"n_trades": 150, "expectancy_r": 0.02})
        assert any("KILL-SWITCH" in c for c in v["caveats"])


class TestPhase1Untouched:
    """The protocol forbids modifying the closed strategies. H1 must not
    have leaked into them."""

    def test_trend_pullback_still_resolves(self):
        from strategies import get_strategy
        assert get_strategy("trend_pullback") is not None
        assert get_strategy("dip_in_uptrend") is not None
        assert get_strategy("panic_reversion") is not None

    def test_shared_exclusions_still_reject_the_old_cases(self):
        assert F.passes_instrument_exclusions("PLTR")[0] is False
        assert F.passes_instrument_exclusions("SOXL")[0] is False
        assert F.passes_instrument_exclusions("AAPL")[0] is True
