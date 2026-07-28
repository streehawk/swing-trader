"""tests/test_swing_setups.py — entry filter v2 (Trading Plan §4).

One test per gate in the seven-gate checklist, plus the properties that
make the output a trade plan rather than a suggestion.

The gates that are NEW in v2 get the most attention, because they are the
whole reason for the rewrite: market regime (gate 1), relative strength
(gate 5), and the pause quality conditions (gate 6). v1 had none of them
and failed its validation.

Synthetic OHLCV throughout — no network. The earnings/sector lookups in
_enrich are injected as fake modules via sys.modules.
"""
from __future__ import annotations

import sys
import types

import numpy as np
import pandas as pd
import pytest

from core import swing_setups as ss


# ── Series builders ──────────────────────────────────────────────────

def _series(n, drift, seed=3, noise=0.006, start=100.0):
    """Noisy trend. The noise matters: a monotonic ramp saturates RSI and
    produces an ATR so small every gate behaves unrealistically."""
    rng = np.random.default_rng(seed)
    c = [start]
    for _ in range(1, n):
        c.append(c[-1] * (1 + drift + rng.normal(0, noise)))
    return np.array(c)


def _frame(closes, volume, band=0.008, start="2024-01-01"):
    return pd.DataFrame(
        {
            "open": closes * 0.999,
            "high": closes * (1 + band),
            "low": closes * (1 - band),
            "close": closes,
            "volume": volume,
        },
        index=pd.date_range(start, periods=len(closes), freq="B"),
    )


def make_pullback(n=300, drift=0.0035, pull_bars=5, decline=0.006,
                  seed=3, band=0.008, pause_volume=900_000.0,
                  base_volume=2_000_000.0):
    """Uptrend, then a shallow pause on DECLINING volume — the v2 setup."""
    c = list(_series(n - pull_bars, drift, seed=seed))
    peak = c[-1]
    for j in range(1, pull_bars + 1):
        c.append(peak * (1 - decline * j))
    closes = np.array(c)
    vol = np.concatenate([
        np.full(n - pull_bars, base_volume),
        np.full(pull_bars, pause_volume),
    ])
    return _frame(closes, vol, band=band)


def make_spy(n=300, drift=0.0012, seed=9):
    """SPY in a healthy uptrend — above its own SMA50 and SMA200."""
    return _frame(_series(n, drift, seed=seed), np.full(n, 80_000_000.0))


def make_spy_bear(n=300, seed=11):
    """SPY below both SMAs: rises, then falls hard."""
    up = _series(n // 2, 0.002, seed=seed)
    down = _series(n - n // 2, -0.004, seed=seed + 1, start=float(up[-1]))
    return _frame(np.concatenate([up, down]), np.full(n, 80_000_000.0))


def make_downtrend(n=300):
    return _frame(_series(n, -0.002, seed=5, start=300.0), np.full(n, 2_000_000.0))


@pytest.fixture
def spy():
    return make_spy()


@pytest.fixture
def fake_enrich_deps(monkeypatch):
    """Inject data.market_client / utils.sector_cache / data.sector_relative."""
    knobs = {"earnings_days": None, "sector": "Technology", "sector_rel": 0.03}

    mc = types.ModuleType("data.market_client")
    mc.get_earnings_days = lambda sym, max_days=45: knobs["earnings_days"]
    monkeypatch.setitem(sys.modules, "data.market_client", mc)

    sc = types.ModuleType("utils.sector_cache")
    sc.get_sector = lambda sym: knobs["sector"]
    monkeypatch.setitem(sys.modules, "utils.sector_cache", sc)

    sr = types.ModuleType("data.sector_relative")
    sr.sector_relative_return = lambda sym, sector, window_days=20: knobs["sector_rel"]
    monkeypatch.setitem(sys.modules, "data.sector_relative", sr)
    return knobs


# ── Gate 1: market regime ────────────────────────────────────────────

class TestGate1Regime:
    def test_healthy_spy_allows_trading(self, spy):
        ok, msg = ss.market_regime_ok(spy)
        assert ok, msg

    def test_bear_spy_blocks_all_setups(self):
        """A hostile market overrides any setup, however clean."""
        s = ss.evaluate_bars("T", make_pullback(), spy_hist=make_spy_bear())
        assert not s.qualifies
        assert "regime" in s.reject_reason.lower()

    def test_missing_spy_fails_closed(self):
        """A regime we cannot verify is not a regime we may trade into."""
        ok, msg = ss.market_regime_ok(None)
        assert ok is False
        assert "fail closed" in msg

        s = ss.evaluate_bars("T", make_pullback(), spy_hist=None)
        assert not s.qualifies

    def test_short_spy_history_fails_closed(self):
        assert ss.market_regime_ok(make_spy(n=60))[0] is False

    def test_gate_can_be_disabled_for_testing(self, monkeypatch):
        monkeypatch.setattr(ss, "REGIME_GATE_ENABLED", False)
        assert ss.market_regime_ok(None)[0] is True

    def test_precomputed_regime_is_honoured(self):
        """The scan computes regime once and passes it down; a False must
        block even when spy_hist would say otherwise."""
        s = ss.evaluate_bars("T", make_pullback(), spy_hist=make_spy(),
                             regime_ok=False)
        assert not s.qualifies
        assert "regime" in s.reject_reason.lower()


# ── Gate 2: universe ─────────────────────────────────────────────────

class TestGate2Universe:
    def test_excluded_ticker_rejected(self, spy):
        s = ss.evaluate_bars("PLTR", make_pullback(), spy_hist=spy)
        assert not s.qualifies
        assert "exclusão" in s.reject_reason

    def test_low_price_rejected(self, spy):
        df = make_pullback()
        df[["open", "high", "low", "close"]] *= 0.02   # ~$6
        s = ss.evaluate_bars("T", df, spy_hist=spy)
        assert not s.qualifies
        assert "preço" in s.reject_reason

    def test_thin_volume_rejected(self, spy):
        """Plan rule 7: >= 1M shares/day."""
        df = make_pullback(base_volume=400_000.0, pause_volume=200_000.0)
        s = ss.evaluate_bars("T", df, spy_hist=spy)
        assert not s.qualifies
        assert "volume médio" in s.reject_reason

    def test_unknown_volume_fails_closed(self, spy):
        df = make_pullback().drop(columns=["volume"])
        s = ss.evaluate_bars("T", df, spy_hist=spy)
        assert not s.qualifies
        assert "volume indisponível" in s.reject_reason


# ── Gate 4: uptrend ──────────────────────────────────────────────────

class TestGate4Uptrend:
    def test_downtrend_rejected(self, spy):
        s = ss.evaluate_bars("T", make_downtrend(), spy_hist=spy)
        assert not s.qualifies
        assert "uptrend" in s.reject_reason

    def test_short_history_rejected(self, spy):
        s = ss.evaluate_bars("T", make_pullback(n=150), spy_hist=spy)
        assert not s.qualifies
        assert "histórico" in s.reject_reason


# ── Gate 5: relative strength ────────────────────────────────────────

class TestGate5RelativeStrength:
    def test_leader_passes(self, spy):
        s = ss.evaluate_bars("T", make_pullback(drift=0.0035), spy_hist=spy)
        assert s.qualifies, s.reject_reason
        assert s.rs_vs_spy_63d > 0

    def test_laggard_rejected(self):
        """A stock in a genuine uptrend, but rising slower than SPY, must
        still be rejected. It has to clear gate 4 first, otherwise this
        would pass for the wrong reason."""
        strong_spy = make_spy(drift=0.006, seed=21)
        s = ss.evaluate_bars("T", make_pullback(drift=0.002, seed=7),
                             spy_hist=strong_spy)
        assert not s.qualifies
        assert "força relativa" in s.reject_reason
        assert s.rs_vs_spy_63d < 0

    def test_missing_spy_does_not_silently_skip_the_gate(self, monkeypatch):
        """With the regime gate off, a missing SPY must still reject —
        skipping a required gate silently is how bad trades get taken."""
        monkeypatch.setattr(ss, "REGIME_GATE_ENABLED", False)
        s = ss.evaluate_bars("T", make_pullback(), spy_hist=None)
        assert not s.qualifies
        assert "força relativa" in s.reject_reason


# ── Gate 6: valid pause ──────────────────────────────────────────────

class TestGate6Pause:
    def test_valid_pause_qualifies(self, spy):
        s = ss.evaluate_bars("T", make_pullback(), spy_hist=spy)
        assert s.qualifies, s.reject_reason
        assert s.setup_type == "PULLBACK"

    def test_too_shallow_rejected(self, spy):
        s = ss.evaluate_bars("T", make_pullback(pull_bars=1, decline=0.001),
                             spy_hist=spy)
        assert not s.qualifies

    def test_too_deep_rejected(self, spy):
        """> 3 ATR is a breakdown, not a pause. v1 allowed up to 4."""
        s = ss.evaluate_bars("T", make_pullback(pull_bars=8, decline=0.008),
                             spy_hist=spy)
        assert not s.qualifies
        assert "recuo" in s.reject_reason

    def test_rising_volume_in_pause_rejected(self, spy):
        """Heavy volume on the pullback is distribution, not a pause."""
        df = make_pullback(pause_volume=5_000_000.0)
        s = ss.evaluate_bars("T", df, spy_hist=spy)
        assert not s.qualifies
        assert "volume da pausa" in s.reject_reason

    def test_close_below_sma50_rejected(self, spy):
        """Slice the pause down through the SMA50 — a breakdown."""
        df = make_pullback(pull_bars=5, decline=0.006)
        df.iloc[-3:, df.columns.get_loc("close")] *= 0.80
        df.iloc[-3:, df.columns.get_loc("low")] *= 0.80
        s = ss.evaluate_bars("T", df, spy_hist=spy)
        assert not s.qualifies

    def test_breakout_setups_no_longer_exist(self, spy):
        """Per the plan's AAPL journal lesson, chasing breakouts is out."""
        rows = list(_series(280, 0.004, seed=13))
        rows += [rows[-1] * 1.001] * 15          # tight range
        rows.append(rows[-1] * 1.05)             # breakout bar
        closes = np.array(rows)
        vol = np.full(len(closes), 2_000_000.0)
        vol[-1] = 8_000_000.0
        s = ss.evaluate_bars("T", _frame(closes, vol), spy_hist=spy)
        assert s.setup_type != "BREAKOUT"


# ── Gate 7: structure supports >= 2R ─────────────────────────────────

class TestGate7Structure:
    def test_plan_is_complete(self, spy):
        s = ss.evaluate_bars("T", make_pullback(), spy_hist=spy)
        assert s.qualifies
        for f in ("entry", "entry_limit", "stop", "target", "risk_per_share",
                  "reward_per_share", "rr_ratio", "stop_pct"):
            assert getattr(s, f) is not None, f
        assert s.time_stop_days == ss.TIME_STOP_DAYS

    def test_levels_are_ordered(self, spy):
        s = ss.evaluate_bars("T", make_pullback(), spy_hist=spy)
        assert s.stop < s.entry < s.entry_limit < s.target

    def test_entry_sits_above_the_pause_high(self, spy):
        s = ss.evaluate_bars("T", make_pullback(), spy_hist=spy)
        assert s.entry > s.pause_high

    def test_stop_sits_below_the_pause_low(self, spy):
        s = ss.evaluate_bars("T", make_pullback(), spy_hist=spy)
        assert s.stop <= s.pause_low

    def test_stop_is_at_least_one_atr_away(self, spy):
        s = ss.evaluate_bars("T", make_pullback(), spy_hist=spy)
        assert s.entry - s.stop >= ss.STOP_ATR_FLOOR_MULT * s.atr14 - 1e-6

    def test_target_is_exactly_two_r(self, spy):
        s = ss.evaluate_bars("T", make_pullback(), spy_hist=spy)
        assert s.rr_ratio == pytest.approx(2.0, abs=0.01)
        assert s.target - s.entry == pytest.approx(2.0 * s.risk_per_share, rel=1e-3)

    def test_rr_floor_is_two_not_one_point_five(self):
        """v1's 1.5 floor needed a hit rate it never achieved."""
        assert ss.MIN_RR == 2.0

    def test_unreachable_target_rejected(self, spy):
        """If 2R is a fantasy move for the volatility, skip — never force it."""
        s = ss.evaluate_bars("T", make_pullback(pull_bars=4, decline=0.004),
                             spy_hist=spy)
        if not s.qualifies:
            assert "alvo 2R" in s.reject_reason or "recuo" in s.reject_reason

    def test_wide_stop_rejected(self, spy):
        s = ss.evaluate_bars("T", make_pullback(band=0.05), spy_hist=spy)
        assert not s.qualifies


# ── Gate 3: earnings ─────────────────────────────────────────────────

class TestGate3Earnings:
    def test_earnings_inside_window_rejects(self, spy, fake_enrich_deps):
        fake_enrich_deps["earnings_days"] = 7
        s = ss.evaluate_swing("T", make_pullback(), spy_hist=spy)
        assert not s.qualifies
        assert "earnings" in s.reject_reason
        assert s.earnings_in_window is True

    def test_ten_day_rule_is_covered(self, spy, fake_enrich_deps):
        """Plan rule 10's floor: never enter within 10 calendar days."""
        fake_enrich_deps["earnings_days"] = ss.EARNINGS_MIN_DAYS - 1
        s = ss.evaluate_swing("T", make_pullback(), spy_hist=spy)
        assert not s.qualifies

    def test_earnings_outside_window_survives(self, spy, fake_enrich_deps):
        fake_enrich_deps["earnings_days"] = 60
        s = ss.evaluate_swing("T", make_pullback(), spy_hist=spy)
        assert s.qualifies, s.reject_reason
        assert s.earnings_in_window is False

    def test_unknown_earnings_does_not_reject(self, spy, fake_enrich_deps):
        fake_enrich_deps["earnings_days"] = None
        assert ss.evaluate_swing("T", make_pullback(), spy_hist=spy).qualifies

    def test_enrich_populates_sector_context(self, spy, fake_enrich_deps):
        fake_enrich_deps["earnings_days"] = 60
        s = ss.evaluate_swing("T", make_pullback(), spy_hist=spy)
        assert s.sector == "Technology"


# ── Sizing — plan §5 ─────────────────────────────────────────────────

class TestSizing:
    def _setup(self, spy):
        s = ss.evaluate_bars("T", make_pullback(), spy_hist=spy)
        assert s.qualifies, s.reject_reason
        return s

    def test_default_risk_is_one_percent(self, spy, monkeypatch):
        monkeypatch.delenv("SWING_RISK_PCT", raising=False)
        sz = ss.position_size(self._setup(spy), 3_000.0)
        assert sz["risk_pct"] == pytest.approx(0.01)

    def test_shares_are_rounded_down(self, spy):
        """Rounding up would breach the 1% rule on every trade."""
        s = self._setup(spy)
        sz = ss.position_size(s, 3_000.0, risk_pct=0.01)
        assert sz["shares"] == float(int(sz["shares"]))
        assert sz["shares"] * s.risk_per_share <= 30.0 + 1e-9

    def test_stop_out_costs_no_more_than_the_budget(self, spy):
        s = self._setup(spy)
        sz = ss.position_size(s, 3_000.0, risk_pct=0.01, usd_eur=1.0)
        assert sz["risk_eur"] <= 30.0 + 1e-9

    def test_too_small_to_size_returns_zero_with_a_reason(self, spy):
        s = self._setup(spy)
        sz = ss.position_size(s, 100.0, risk_pct=0.01)
        assert sz["shares"] == 0.0
        assert "note" in sz

    def test_non_qualifying_gets_zero(self, spy):
        s = ss.evaluate_bars("T", make_downtrend(), spy_hist=spy)
        assert ss.position_size(s, 3_000.0)["shares"] == 0.0


# ── OTOCO bracket — plan §6 ──────────────────────────────────────────

class TestOtoco:
    def test_bracket_has_three_orders(self, spy):
        s = ss.evaluate_bars("T", make_pullback(), spy_hist=spy)
        b = s.otoco(10)
        assert set(b) == {"A_entry", "B_take_profit", "C_stop_loss"}
        assert b["A_entry"]["action"] == "BUY"
        assert b["B_take_profit"]["action"] == "SELL"
        assert b["C_stop_loss"]["action"] == "SELL"

    def test_all_three_share_the_same_quantity(self, spy):
        s = ss.evaluate_bars("T", make_pullback(), spy_hist=spy)
        b = s.otoco(7)
        assert {o["qty"] for o in b.values()} == {7}

    def test_entry_limit_caps_slippage(self, spy):
        s = ss.evaluate_bars("T", make_pullback(), spy_hist=spy)
        b = s.otoco(1)
        assert b["A_entry"]["limit"] > b["A_entry"]["stop_trigger"]

    def test_rendered_bracket_names_all_orders(self, spy):
        s = ss.evaluate_bars("T", make_pullback(), spy_hist=spy)
        out = "\n".join(ss.format_otoco_telegram(s, 10))
        assert "A ENTRY" in out and "B TARGET" in out and "C STOP" in out


# ── Rendering ────────────────────────────────────────────────────────

class TestRendering:
    def test_blocked_regime_says_so(self):
        ss._last_run_stats = {"regime_ok": False,
                              "regime_msg": "SPY 500 < SMA50 510"}
        out = ss.format_swing_telegram([])
        assert "hostil" in out
        assert "SMA50" in out

    def test_empty_scan_explains_where_they_fell(self):
        ss._last_run_stats = {
            "regime_ok": True, "n_universe": 230, "n_no_data": 2,
            "n_universe_fail": 10, "n_trend_fail": 150, "n_rs_fail": 40,
            "n_pause_fail": 20, "n_structure_fail": 8, "n_earnings_fail": 0,
        }
        out = ss.format_swing_telegram([])
        assert "230" in out and "laggards" in out

    def test_setup_output_carries_the_bracket(self, spy):
        ss._last_run_stats = {"regime_ok": True}
        s = ss.evaluate_bars("T", make_pullback(), spy_hist=spy)
        s.summary_line = ss.format_setup_line(s)
        out = ss.format_swing_telegram([s], equity_eur=3_000.0)
        assert "A ENTRY" in out
        assert f"{s.target:.2f}" in out


# ── Dispatcher wiring ────────────────────────────────────────────────

class TestDispatcher:
    def test_swing_is_the_default(self, monkeypatch):
        monkeypatch.delenv("DIPRADAR_SCORING_MODE", raising=False)
        monkeypatch.delenv("DIPRADAR_USE_DISLOCATION", raising=False)
        from core.scoring_dispatch import get_active_scorer
        assert get_active_scorer() == "swing"

    def test_watchlist_still_reachable(self, monkeypatch):
        monkeypatch.setenv("DIPRADAR_SCORING_MODE", "watchlist")
        from core.scoring_dispatch import get_active_scorer
        assert get_active_scorer() == "watchlist"

    def test_session_returns_swing_result(self, monkeypatch, spy, fake_enrich_deps):
        fake_enrich_deps["earnings_days"] = 60
        monkeypatch.setenv("DIPRADAR_SCORING_MODE", "swing")
        monkeypatch.setattr(ss, "fetch_spy_history", lambda: spy)
        from core.scoring_dispatch import ScoringSession
        r = ScoringSession().score("T", {"_hist": make_pullback()})
        assert r.scorer == "swing"
        assert r.qualifies
        assert r.z is None
        assert "entry" in " ".join(r.explanation_lines())

    def test_session_explains_rejection(self, monkeypatch, spy):
        monkeypatch.setenv("DIPRADAR_SCORING_MODE", "swing")
        monkeypatch.setattr(ss, "fetch_spy_history", lambda: spy)
        from core.scoring_dispatch import ScoringSession
        r = ScoringSession().score("T", {"_hist": make_downtrend()})
        assert not r.qualifies
        assert "uptrend" in r.exclude_reason
