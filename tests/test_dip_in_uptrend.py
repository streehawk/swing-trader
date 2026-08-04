"""tests/test_dip_in_uptrend.py — Phase 1B acceptance tests.

One test class per ticket, covering exactly the cases each ticket's
Acceptance section names, plus the guardrails the spec insists on
(fail-closed on missing data, short-circuit ordering, Phase 1 untouched).

Synthetic OHLCV throughout — no network.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from strategies import filters as F
from strategies import dip_in_uptrend as D


# ── Builders ─────────────────────────────────────────────────────────

def bars_from(closes, *, volume=1_000_000.0, band=0.01, highs=None, lows=None):
    closes = np.asarray(closes, dtype=float)
    n = len(closes)
    vol = np.full(n, volume, dtype=float) if np.isscalar(volume) else np.asarray(volume, float)
    return pd.DataFrame(
        {
            "open":   closes * 0.999,
            "high":   closes * (1 + band) if highs is None else np.asarray(highs, float),
            "low":    closes * (1 - band) if lows is None else np.asarray(lows, float),
            "close":  closes,
            "volume": vol,
        },
        index=pd.date_range("2024-01-01", periods=n, freq="B"),
    )


def make_dip_series(n=260, *, last_close=100.0, sma20=94.0, sma50=105.0, sma200=90.0):
    """Construct closes whose trailing SMAs hit the requested values.

    Built backwards from the targets so each rule can be exercised in
    isolation: set `last_close` relative to sma20/50/200 and the gate
    flips predictably. The oldest bars carry whatever residual is needed
    to land SMA200 on target, which is fine — nothing reads them directly.
    """
    closes = np.empty(n, dtype=float)
    closes[-1] = last_close

    # Bars -20..-2 carry the SMA20 residual.
    need20 = sma20 * 20 - last_close
    closes[-20:-1] = need20 / 19.0

    # Bars -50..-21 carry the SMA50 residual.
    need50 = sma50 * 50 - (last_close + closes[-20:-1].sum())
    closes[-50:-20] = need50 / 30.0

    # Bars -200..-51 carry the SMA200 residual.
    need200 = sma200 * 200 - (last_close + closes[-50:-1].sum())
    closes[-200:-50] = need200 / 150.0

    # Anything older is irrelevant to the three SMAs under test.
    closes[:-200] = sma200
    return closes


def make_realistic(
    n=260, *, up=0.004, dip_len=22, dip_rate=0.013,
    base_len=24, pop_len=2, pop_rate=0.025,
    band=0.04, seed=5, noise=0.006,
    base_volume=500_000.0, today_volume=1_200_000.0,
):
    """Uptrend → correction → base → pop. The shape this strategy targets.

    A V-shaped bounce will NOT pass, and that is correct rather than a
    fixture flaw: entry sits at the top of the pop and the stop below the
    swing low, so a bounce that has already run 10% makes 2R a move the
    volatility cannot plausibly deliver, and P1B-5 rejects it. The
    tradeable shape is dip → consolidate near the low → pop, which keeps
    the swing low close to the entry.

    `band` sets intraday range and therefore ATR. It has to be wide
    (~4%) for a 2R target to clear the realism check — this strategy is
    only viable on genuinely volatile names.
    """
    rng = np.random.default_rng(seed)
    c = [100.0]
    for _ in range(n - dip_len - base_len - pop_len - 1):
        c.append(c[-1] * (1 + up + rng.normal(0, noise)))
    for _ in range(dip_len):
        c.append(c[-1] * (1 - dip_rate + rng.normal(0, noise * 0.4)))
    base = c[-1]
    for _ in range(base_len):
        c.append(base * (1 + rng.normal(0, 0.004)))
    for _ in range(pop_len):
        c.append(c[-1] * (1 + pop_rate + rng.normal(0, noise * 0.4)))

    closes = np.array(c)
    vol = np.full(len(closes), base_volume)
    vol[-1] = today_volume
    return bars_from(closes, volume=vol, band=band)


@pytest.fixture
def dip_bars():
    """The canonical P1B pass: popped >=5% over SMA20, below SMA50, above
    SMA200, on 2.4x volume, with structure that supports 2R."""
    return make_realistic()


# ── TICKET P1B-1 ─────────────────────────────────────────────────────

class TestP1B1DipInUptrend:
    """Acceptance: 6% above SMA20 + below SMA50 + above SMA200 -> True;
    below SMA20 -> False; above SMA50 -> False; below SMA200 -> False."""

    def test_pop_above_sma20_below_sma50_above_sma200_is_true(self, dip_bars):
        c = dip_bars["close"]
        last, s20 = float(c.iloc[-1]), float(c.iloc[-20:].mean())
        s50, s200 = float(c.iloc[-50:].mean()), float(c.iloc[-200:].mean())
        assert last / s20 - 1 >= 0.05      # the pop
        assert last < s50                  # really did dip
        assert last > s200                 # long-term trend intact
        assert D.is_dip_in_uptrend(dip_bars) is True

    def test_price_below_sma20_is_false(self):
        # 2% above SMA20 — a pop, but not the 5% the rule demands.
        b = bars_from(make_dip_series(last_close=95.9, sma20=94.0,
                                      sma50=105.0, sma200=90.0))
        assert D.is_dip_in_uptrend(b) is False

    def test_price_above_sma50_is_false(self):
        """No real dip happened — that is Phase 1's setup, not this one."""
        b = bars_from(make_dip_series(last_close=110.0, sma20=94.0,
                                      sma50=105.0, sma200=90.0))
        assert D.is_dip_in_uptrend(b) is False

    def test_price_below_sma200_is_false(self):
        """Long-term uptrend broken — a correction is not the same as a
        collapse, and the deep-reversal variant is out of scope."""
        b = bars_from(make_dip_series(last_close=100.0, sma20=94.0,
                                      sma50=105.0, sma200=110.0))
        assert D.is_dip_in_uptrend(b) is False

    def test_short_history_fails_closed(self):
        assert D.is_dip_in_uptrend(bars_from(np.full(120, 100.0))) is False

    def test_missing_close_fails_closed(self, dip_bars):
        assert D.is_dip_in_uptrend(dip_bars.drop(columns=["close"])) is False

    def test_exactly_five_pct_above_sma20_passes(self):
        """Rule is `>=`, so the boundary qualifies."""
        b = bars_from(make_dip_series(last_close=98.7, sma20=94.0,
                                      sma50=105.0, sma200=90.0))
        assert D.is_dip_in_uptrend(b) is True


# ── TICKET P1B-2 ─────────────────────────────────────────────────────

class TestP1B2AverageVolume:
    """Acceptance: 150k -> False, 250k -> True, missing volume -> False."""

    def test_150k_is_false(self):
        assert F.passes_avg_volume(bars_from(np.full(60, 50.0), volume=150_000)) is False

    def test_250k_is_true(self):
        assert F.passes_avg_volume(bars_from(np.full(60, 50.0), volume=250_000)) is True

    def test_missing_volume_column_fails_closed(self):
        b = bars_from(np.full(60, 50.0)).drop(columns=["volume"])
        assert F.passes_avg_volume(b) is False

    def test_short_history_fails_closed(self):
        assert F.passes_avg_volume(bars_from(np.full(5, 50.0), volume=1e6)) is False

    def test_exactly_at_threshold_passes(self):
        assert F.passes_avg_volume(bars_from(np.full(60, 50.0), volume=200_000)) is True

    def test_uses_only_the_last_twenty_bars(self):
        """A stock that was liquid a year ago but is thin now must fail."""
        vol = np.concatenate([np.full(40, 5_000_000.0), np.full(20, 100_000.0)])
        assert F.passes_avg_volume(bars_from(np.full(60, 50.0), volume=vol)) is False

    def test_default_threshold_is_200k(self):
        assert F.MIN_AVG_VOL == 200_000


# ── TICKET P1B-3 ─────────────────────────────────────────────────────

class TestP1B3RelativeVolume:
    """Acceptance: today 2x the 50-day avg -> True (RVOL~2.0);
    today at the average -> False (RVOL~1.0). Guard divide-by-zero and
    short history (fail closed)."""

    def _with_today(self, today_vol, prior_vol=500_000.0, n=60):
        vol = np.full(n, prior_vol)
        vol[-1] = today_vol
        return bars_from(np.full(n, 50.0), volume=vol)

    def test_double_average_is_true(self):
        b = self._with_today(1_000_000.0, 500_000.0)
        assert F.relative_volume(b) == pytest.approx(2.0)
        assert F.passes_relative_volume(b) is True

    def test_at_average_is_false(self):
        b = self._with_today(500_000.0, 500_000.0)
        assert F.relative_volume(b) == pytest.approx(1.0)
        assert F.passes_relative_volume(b) is False

    def test_exactly_at_threshold_is_false(self):
        """Ticket says `RVOL > 1.5`, strictly."""
        b = self._with_today(750_000.0, 500_000.0)
        assert F.relative_volume(b) == pytest.approx(1.5)
        assert F.passes_relative_volume(b) is False

    def test_zero_denominator_fails_closed(self):
        b = self._with_today(1_000_000.0, 0.0)
        assert F.relative_volume(b) is None
        assert F.passes_relative_volume(b) is False

    def test_short_history_fails_closed(self):
        assert F.passes_relative_volume(bars_from(np.full(20, 50.0), volume=1e6)) is False

    def test_missing_volume_fails_closed(self):
        b = bars_from(np.full(60, 50.0)).drop(columns=["volume"])
        assert F.passes_relative_volume(b) is False

    def test_denominator_excludes_today(self):
        """Including today would dampen exactly the spike being detected."""
        b = self._with_today(10_000_000.0, 500_000.0)
        assert F.relative_volume(b) == pytest.approx(20.0)

    def test_defaults_match_the_ticket(self):
        assert F.MIN_RVOL == 1.5
        assert F.RVOL_LOOKBACK == 50


# ── TICKET P1B-4 ─────────────────────────────────────────────────────

class TestP1B4EarningsGate:
    """Acceptance: one integration test confirming a name reporting in
    3 days is rejected."""

    def test_reports_in_three_days_is_rejected(self, dip_bars):
        s = D.evaluate_bars(
            "TEST", dip_bars,
            get_earnings_days_fn=lambda sym, max_days=45: 3,
        )
        assert s.qualifies is False
        assert "earnings" in s.reject_reason
        assert s.days_to_earnings == 3

    def test_far_earnings_does_not_block(self, dip_bars):
        ok, days, _ = F.passes_earnings_gate(
            "TEST", get_earnings_days_fn=lambda sym, max_days=45: 90)
        assert ok is True and days == 90

    def test_unknown_earnings_does_not_block(self):
        """Mirrors Phase 1: absence of a calendar entry is common and must
        not silently kill most candidates."""
        ok, days, _ = F.passes_earnings_gate(
            "TEST", get_earnings_days_fn=lambda sym, max_days=45: None)
        assert ok is True and days is None

    def test_uses_the_same_window_as_phase_1(self):
        """15 trading days -> 21 calendar. A report on day 18 is inside the
        hold, so it must be rejected even though it is past the 10-day floor."""
        ok, _, _ = F.passes_earnings_gate(
            "TEST", time_stop_days=15,
            get_earnings_days_fn=lambda sym, max_days=45: 18)
        assert ok is False


# ── TICKET P1B-5 ─────────────────────────────────────────────────────

class TestP1B5Structure:
    """Acceptance: given a fixture dip, returns {entry, stop, target,
    risk_per_share} with target >= 2R; rejects when structure cannot
    support 2R."""

    def test_returns_a_complete_plan(self, dip_bars):
        plan = D.build_structure(dip_bars)
        assert plan is not None
        for k in ("entry", "stop", "target", "risk_per_share"):
            assert k in plan and plan[k] is not None

    def test_target_is_at_least_two_r(self, dip_bars):
        plan = D.build_structure(dip_bars)
        assert plan["rr_ratio"] >= 2.0 - 1e-9
        assert plan["target"] - plan["entry"] == pytest.approx(
            2.0 * plan["risk_per_share"], rel=1e-3)

    def test_levels_are_ordered(self, dip_bars):
        p = D.build_structure(dip_bars)
        assert p["stop"] < p["entry"] < p["entry_limit"] < p["target"]

    def test_entry_is_above_the_swing_high(self, dip_bars):
        p = D.build_structure(dip_bars)
        assert p["entry"] > p["swing_high"]

    def test_stop_is_below_the_swing_low(self, dip_bars):
        p = D.build_structure(dip_bars)
        assert p["stop"] <= p["swing_low"]

    def test_stop_is_at_least_one_atr_from_entry(self, dip_bars):
        p = D.build_structure(dip_bars)
        assert p["entry"] - p["stop"] >= D.STOP_ATR_FLOOR_MULT * p["atr14"] - 1e-6

    def test_rejects_when_two_r_is_not_realistic(self, dip_bars):
        """A very wide swing makes 2R a fantasy move for the volatility;
        the spec says reject rather than force it."""
        assert D.build_structure(dip_bars, max_target_atr=0.5) is None

    def test_short_history_returns_none(self):
        assert D.build_structure(bars_from(np.full(10, 100.0))) is None


# ── TICKET P1B-6 ─────────────────────────────────────────────────────

class TestP1B6MasterGate:
    """Acceptance: a symbol passing all emits a full plan; failing any one
    emits nothing; order short-circuits."""

    NO_EARNINGS = staticmethod(lambda sym, max_days=45: None)

    def _pass_bars(self, n=260):
        """All six gates satisfied: >=$10, 500k avg volume, RVOL 2.4,
        dip-in-uptrend, no earnings, structure supports 2R."""
        return make_realistic(n=n)

    def test_all_gates_pass_emits_a_full_plan(self):
        s = D.evaluate_bars("TEST", self._pass_bars(),
                            get_earnings_days_fn=self.NO_EARNINGS)
        assert s.qualifies, s.reject_reason
        assert s.setup_type == "DIP_IN_UPTREND"
        for f in ("entry", "entry_limit", "stop", "target", "risk_per_share", "rr_ratio"):
            assert getattr(s, f) is not None, f
        assert s.rr_ratio >= 2.0 - 1e-9

    def test_excluded_ticker_emits_nothing(self):
        s = D.evaluate_bars("PLTR", self._pass_bars(),
                            get_earnings_days_fn=self.NO_EARNINGS)
        assert not s.qualifies and "exclusão" in s.reject_reason

    def test_leveraged_etf_emits_nothing(self):
        s = D.evaluate_bars("TQQQ", self._pass_bars(),
                            get_earnings_days_fn=self.NO_EARNINGS)
        assert not s.qualifies and "alavancado" in s.reject_reason

    def test_low_price_emits_nothing(self):
        b = self._pass_bars()
        b[["open", "high", "low", "close"]] *= 0.05      # ~$5
        s = D.evaluate_bars("TEST", b, get_earnings_days_fn=self.NO_EARNINGS)
        assert not s.qualifies and "preço" in s.reject_reason

    def test_thin_volume_emits_nothing(self):
        b = make_realistic(base_volume=100_000.0, today_volume=400_000.0)
        s = D.evaluate_bars("TEST", b, get_earnings_days_fn=self.NO_EARNINGS)
        assert not s.qualifies and "volume médio" in s.reject_reason

    def test_low_rvol_emits_nothing(self):
        """Same passing setup, but today's volume is merely average."""
        b = make_realistic(today_volume=500_000.0)
        s = D.evaluate_bars("TEST", b, get_earnings_days_fn=self.NO_EARNINGS)
        assert not s.qualifies and "RVOL" in s.reject_reason

    def test_wrong_trend_emits_nothing(self):
        """No dip ever happened — price is above SMA50, which is Phase 1's
        setup, not this one."""
        n = 260
        vol = np.full(n, 500_000.0); vol[-1] = 1_200_000.0
        b = bars_from(make_dip_series(n=n, last_close=110.0, sma20=94.0,
                                      sma50=105.0, sma200=90.0), volume=vol)
        s = D.evaluate_bars("TEST", b, get_earnings_days_fn=self.NO_EARNINGS)
        assert not s.qualifies and "SMA50" in s.reject_reason

    def test_gate_order_short_circuits(self):
        """Volume is checked before the trend signal, so a thin name must
        never reach the SMA maths. Proven by asserting the earnings
        callback — a LATER gate — is never invoked."""
        calls = []

        def spy_earnings(sym, max_days=45):
            calls.append(sym)
            return None

        b = make_realistic(base_volume=100_000.0, today_volume=120_000.0)
        s = D.evaluate_bars("TEST", b, get_earnings_days_fn=spy_earnings)
        assert not s.qualifies
        assert "volume médio" in s.reject_reason
        assert calls == [], "earnings gate ran despite an earlier failure"

    def test_regime_gate_is_off_by_default(self):
        """P1B-6: the base case runs WITHOUT the regime gate. Passing no
        SPY series must not block anything."""
        s = D.evaluate_bars("TEST", self._pass_bars(), spy_hist=None,
                            get_earnings_days_fn=self.NO_EARNINGS)
        assert s.qualifies, s.reject_reason

    def test_regime_gate_blocks_when_enabled_and_spy_weak(self):
        n = 260
        spy = bars_from(np.concatenate([np.full(n - 60, 500.0), np.full(60, 400.0)]))
        s = D.evaluate_bars("TEST", self._pass_bars(), spy_hist=spy,
                            use_regime_gate=True,
                            get_earnings_days_fn=self.NO_EARNINGS)
        assert not s.qualifies and "regime" in s.reject_reason

    def test_regime_gate_allows_when_enabled_and_spy_strong(self):
        n = 260
        spy = bars_from(np.linspace(300.0, 500.0, n))
        s = D.evaluate_bars("TEST", self._pass_bars(), spy_hist=spy,
                            use_regime_gate=True,
                            get_earnings_days_fn=self.NO_EARNINGS)
        assert s.qualifies, s.reject_reason

    def test_regime_gate_fails_closed_without_spy(self):
        s = D.evaluate_bars("TEST", self._pass_bars(), spy_hist=None,
                            use_regime_gate=True,
                            get_earnings_days_fn=self.NO_EARNINGS)
        assert not s.qualifies


# ── Strategy registry + Phase 1 isolation ────────────────────────────

class TestStrategyRegistry:
    def test_both_strategies_resolve(self):
        from strategies import get_strategy
        assert callable(get_strategy("trend_pullback"))
        assert callable(get_strategy("dip_in_uptrend"))

    def test_default_is_phase_1(self):
        from strategies import DEFAULT_STRATEGY, get_strategy
        assert DEFAULT_STRATEGY == "trend_pullback"
        assert callable(get_strategy())

    def test_unknown_strategy_raises(self):
        from strategies import get_strategy
        with pytest.raises(ValueError, match="unknown strategy"):
            get_strategy("moon_phase")

    def test_phase_1_adapter_delegates_unchanged(self, monkeypatch):
        """The benchmark must not drift: the adapter forwards to Phase 1
        and adds nothing."""
        import core.swing_setups as ss
        seen = {}

        def fake(ticker, hist, *, spy_hist=None, regime_ok=None):
            seen["called"] = (ticker, spy_hist is None, regime_ok)
            return "SENTINEL"

        monkeypatch.setattr(ss, "evaluate_bars", fake)
        from strategies.trend_pullback import evaluate_bars
        out = evaluate_bars("AAPL", None, spy_hist=None, regime_ok=True,
                            min_rvol=99, use_regime_gate=True)   # P1B-only kwargs
        assert out == "SENTINEL"
        assert seen["called"] == ("AAPL", True, True)

    def test_strategies_are_mutually_exclusive(self):
        """A bar set cannot satisfy both: one needs close ABOVE SMA50, the
        other BELOW it. This is why they must never be merged."""
        b = make_realistic()
        dip = D.evaluate_bars("TEST", b, get_earnings_days_fn=lambda s, max_days=45: None)
        from core.swing_setups import evaluate_bars as phase1
        spy = bars_from(np.linspace(300.0, 500.0, len(b)))
        p1 = phase1("TEST", b, spy_hist=spy)

        assert not (dip.qualifies and p1.qualifies)


class TestBacktestWiring:
    def test_replay_defaults_to_phase_1(self):
        """Existing callers must be unaffected by the new flag."""
        import inspect
        from core.swing_backtest import replay_ticker, run_backtest
        assert inspect.signature(replay_ticker).parameters["strategy"].default == "trend_pullback"
        assert inspect.signature(run_backtest).parameters["strategy"].default == "trend_pullback"

    def test_replay_accepts_the_dip_strategy(self):
        from core.swing_backtest import replay_ticker
        b = make_realistic(n=300)
        out = replay_ticker(
            b, "TEST", spy_bars=None, costs_bps=0,
            strategy="dip_in_uptrend",
            strategy_kwargs={"get_earnings_days_fn": lambda s, max_days=45: None},
        )
        assert isinstance(out, list)      # runs end to end without raising
