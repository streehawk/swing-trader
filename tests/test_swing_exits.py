"""tests/test_swing_exits.py — swing exit state machine (Phase 3).

The exit ladder is where a swing system actually makes or loses money, so
these tests pin the behaviours that are easy to get subtly wrong:

  - the stop RATCHETS and never loosens, across ticks
  - the ratchet is driven by MFE, not by the current price (otherwise a
    position that ran to +1.8R and pulled back keeps its original stop —
    exactly the giving-back case breakeven exists to prevent)
  - a stop hit after a run exits at the TRAIL, not at -1R
  - the time stop cannot pre-empt a target that printed the same day
  - a missing ATR can never loosen a stop, only fail to tighten it
  - trading-day plans are compared against calendar-day position ages

No I/O, no market data — the module is a pure function by design.
"""
from __future__ import annotations

import pytest

from core import swing_exits as sx
from core.swing_exits import SwingPlan, SwingState, evaluate_swing_exit


# entry 100, stop 96 → 1R = 4.00, target 110 → 2.5R
def plan(entry=100.0, stop=96.0, target=110.0, days=15) -> SwingPlan:
    return SwingPlan(entry=entry, stop=stop, target=target, time_stop_days=days)


def fresh(p: SwingPlan) -> SwingState:
    """State as written by add_conviction/set_swing_plan at entry."""
    return SwingState(current_stop=p.stop, high_water=p.entry, partial_taken=False)


class TestPlanArithmetic:
    def test_r_is_entry_minus_stop(self):
        assert plan().r == pytest.approx(4.0)

    def test_invalid_plan_suspends_management(self):
        """A non-positive 1R must disable management loudly, not divide by
        zero or invent a stop the user never agreed to."""
        p = SwingPlan(entry=100.0, stop=100.0, target=110.0)
        d = evaluate_swing_exit(p, fresh(p), current_price=90.0, days_held=3)
        assert d.trigger == sx.SWING_PLAN_INVALID
        assert d.action == "HOLD"
        assert d.sell_fraction == 0.0

    def test_inverted_plan_suspends_management(self):
        p = SwingPlan(entry=100.0, stop=105.0, target=110.0)
        d = evaluate_swing_exit(p, fresh(p), current_price=90.0, days_held=3)
        assert d.trigger == sx.SWING_PLAN_INVALID


class TestInitialStop:
    def test_stop_hit_is_minus_one_r(self):
        p = plan()
        d = evaluate_swing_exit(p, fresh(p), current_price=95.5, atr=2.0, days_held=3)
        assert d.trigger == sx.SWING_STOP
        assert d.action == "EXIT"
        assert d.sell_fraction == 1.0
        assert d.r_multiple == pytest.approx(-1.125, abs=0.01)

    def test_exactly_at_stop_exits(self):
        p = plan()
        d = evaluate_swing_exit(p, fresh(p), current_price=96.0, atr=2.0, days_held=3)
        assert d.trigger == sx.SWING_STOP

    def test_just_above_stop_holds(self):
        p = plan()
        d = evaluate_swing_exit(p, fresh(p), current_price=96.01, atr=2.0, days_held=3)
        assert d.action == "HOLD"

    def test_stop_fires_on_day_one(self):
        """No grace period. The DIP path waits 5 days before its stop can
        fire; a swing plan's stop is live from the moment it is filled."""
        p = plan()
        d = evaluate_swing_exit(p, fresh(p), current_price=95.0, atr=2.0, days_held=0)
        assert d.trigger == sx.SWING_STOP


class TestBreakevenRatchet:
    def test_stop_moves_to_entry_at_one_r(self):
        p = plan()
        d = evaluate_swing_exit(p, fresh(p), current_price=104.0, atr=2.0, days_held=4)
        assert d.trigger == sx.SWING_RAISE_STOP
        assert d.new_stop == pytest.approx(100.0)
        assert d.stop_basis == "breakeven"
        assert d.stop_moved is True

    def test_below_one_r_keeps_original_stop(self):
        p = plan()
        d = evaluate_swing_exit(p, fresh(p), current_price=103.9, atr=2.0, days_held=4)
        assert d.new_stop == pytest.approx(96.0)
        assert d.stop_basis == "initial"
        assert d.action == "HOLD"

    def test_ratchet_uses_mfe_not_current_price(self):
        """Ran to +1.5R, gave it back to +0.4R. The stop must already be at
        breakeven — this is the whole point of the rule."""
        p = plan()
        state = SwingState(current_stop=96.0, high_water=106.0, partial_taken=False)
        d = evaluate_swing_exit(p, state, current_price=101.6, atr=2.0, days_held=6)
        assert d.mfe_r == pytest.approx(1.5)
        assert d.r_multiple == pytest.approx(0.4)
        assert d.new_stop >= 100.0

    def test_breakeven_stop_exit_is_labelled_distinctly(self):
        """Exiting at entry after a +1R run is a different outcome from
        being stopped at -1R and must not be reported as a loss."""
        p = plan()
        state = SwingState(current_stop=100.0, high_water=105.0, partial_taken=False)
        d = evaluate_swing_exit(p, state, current_price=99.5, atr=2.0, days_held=7)
        assert d.trigger == sx.SWING_BREAKEVEN_STOP
        assert d.action == "EXIT"


class TestTrailRatchet:
    """The ATR trail is OFF by default in v2 — an OTOCO bracket cannot
    express a continuous trail, and the plan's management is break-even
    only. The machinery is kept and tested behind SWING_TRAIL_ENABLED."""

    @pytest.fixture
    def trail_on(self, monkeypatch):
        monkeypatch.setattr(sx, "TRAIL_ENABLED", True)

    def test_trail_is_off_by_default(self):
        p = plan()
        state = SwingState(current_stop=100.0, high_water=100.0, partial_taken=False)
        d = evaluate_swing_exit(p, state, current_price=106.0, atr=2.0, days_held=5)
        assert d.stop_basis != "trail"
        assert d.new_stop == pytest.approx(100.0)   # breakeven only

    def test_trail_engages_at_one_point_five_r(self, trail_on):
        p = plan()
        state = SwingState(current_stop=100.0, high_water=100.0, partial_taken=False)
        # price 106 = +1.5R, ATR 2.0 → trail = 106 - 3.0 = 103.0
        d = evaluate_swing_exit(p, state, current_price=106.0, atr=2.0, days_held=5)
        assert d.stop_basis == "trail"
        assert d.new_stop == pytest.approx(103.0)

    def test_trail_never_loosens_across_ticks(self):
        p = plan()
        state = SwingState(current_stop=103.0, high_water=106.0, partial_taken=False)
        # Price falls back; the trail computed from the OLD high is lower
        # than the stored stop and must be ignored.
        d = evaluate_swing_exit(p, state, current_price=104.0, atr=2.0, days_held=6)
        assert d.new_stop == pytest.approx(103.0)
        assert d.stop_moved is False

    def test_missing_atr_cannot_loosen_stop(self):
        """A data failure must not widen risk. Without ATR the trail is
        skipped, but the persisted stop is kept."""
        p = plan()
        state = SwingState(current_stop=103.0, high_water=106.0, partial_taken=False)
        d = evaluate_swing_exit(p, state, current_price=105.0, atr=None, days_held=6)
        assert d.new_stop == pytest.approx(103.0)

    def test_missing_atr_still_allows_breakeven(self):
        p = plan()
        d = evaluate_swing_exit(p, fresh(p), current_price=104.5, atr=None, days_held=4)
        assert d.new_stop == pytest.approx(100.0)
        assert d.stop_basis == "breakeven"

    def test_trail_stop_exit_reports_profit(self):
        p = plan()
        state = SwingState(current_stop=103.0, high_water=108.0, partial_taken=False)
        d = evaluate_swing_exit(p, state, current_price=102.5, atr=2.0, days_held=8)
        assert d.trigger == sx.SWING_TRAIL_STOP
        assert d.r_multiple > 0
        assert d.mfe_r == pytest.approx(2.0)


class TestTarget:
    def test_target_closes_the_whole_position_by_default(self):
        """Plan §6: order B is a full-size limit. Target hit = trade over,
        order C cancelled by the OCO pairing."""
        p = plan()
        d = evaluate_swing_exit(p, fresh(p), current_price=110.5, atr=2.0, days_held=6)
        assert d.trigger == sx.SWING_TARGET_PARTIAL
        assert d.action == "EXIT"
        assert d.sell_fraction == pytest.approx(1.0)

    def test_partial_mode_still_available(self, monkeypatch):
        monkeypatch.setattr(sx, "PARTIAL_FRACTION", 0.5)
        p = plan()
        d = evaluate_swing_exit(p, fresh(p), current_price=110.5, atr=2.0, days_held=6)
        assert d.action == "PARTIAL"
        assert d.sell_fraction == pytest.approx(0.5)

    def test_target_lifts_stop_to_at_least_breakeven(self):
        """The remainder must never be able to become a loser."""
        p = plan()
        d = evaluate_swing_exit(p, fresh(p), current_price=110.5, atr=None, days_held=6)
        assert d.new_stop >= p.entry

    def test_target_fires_only_once(self):
        p = plan()
        state = SwingState(current_stop=100.0, high_water=110.5, partial_taken=True)
        d = evaluate_swing_exit(p, state, current_price=111.0, atr=2.0, days_held=7)
        assert d.trigger != sx.SWING_TARGET_PARTIAL

    def test_remainder_rides_the_trail_after_partial(self, monkeypatch):
        monkeypatch.setattr(sx, "PARTIAL_FRACTION", 0.5)
        monkeypatch.setattr(sx, "TRAIL_ENABLED", True)
        p = plan()
        state = SwingState(current_stop=100.0, high_water=110.5, partial_taken=True)
        d = evaluate_swing_exit(p, state, current_price=112.0, atr=2.0, days_held=7)
        assert d.stop_basis == "trail"
        assert d.new_stop == pytest.approx(112.0 - 3.0)


class TestTimeStop:
    def test_time_stop_exits_at_horizon(self):
        p = plan(days=21)
        d = evaluate_swing_exit(p, fresh(p), current_price=101.0, atr=2.0, days_held=21)
        assert d.trigger == sx.SWING_TIME_STOP
        assert d.action == "EXIT"
        assert d.sell_fraction == 1.0

    def test_before_horizon_holds(self):
        p = plan(days=21)
        d = evaluate_swing_exit(p, fresh(p), current_price=101.0, atr=2.0, days_held=20)
        assert d.action == "HOLD"

    def test_stop_outranks_time_stop(self):
        p = plan(days=21)
        d = evaluate_swing_exit(p, fresh(p), current_price=95.0, atr=2.0, days_held=25)
        assert d.trigger == sx.SWING_STOP

    def test_target_outranks_time_stop(self):
        """A target that prints on the last day should be realised, not
        flushed by the clock."""
        p = plan(days=21)
        d = evaluate_swing_exit(p, fresh(p), current_price=111.0, atr=2.0, days_held=21)
        assert d.trigger == sx.SWING_TARGET_PARTIAL

    def test_time_stop_can_exit_a_winner(self):
        p = plan(days=21)
        d = evaluate_swing_exit(p, fresh(p), current_price=105.0, atr=2.0, days_held=22)
        assert d.trigger == sx.SWING_TIME_STOP
        assert d.r_multiple > 0


class TestCalendarConversion:
    def test_fifteen_bars_is_twentyone_days(self):
        assert sx.trading_days_to_calendar(15) == 21

    def test_conversion_is_monotonic(self):
        vals = [sx.trading_days_to_calendar(n) for n in range(1, 40)]
        assert vals == sorted(vals)

    def test_calendar_horizon_always_at_least_bar_count(self):
        for n in range(1, 40):
            assert sx.trading_days_to_calendar(n) >= n


class TestSequences:
    def _walk(self, p, st):
        def tick(price, days, atr=2.0):
            d = evaluate_swing_exit(p, st, current_price=price, atr=atr, days_held=days)
            st.current_stop = d.new_stop
            st.high_water = d.high_water
            if d.trigger == sx.SWING_TARGET_PARTIAL:
                st.partial_taken = True
            return d
        return tick

    def test_full_winner_lifecycle_plan_default(self):
        """The plan's OTOCO life: entry → break-even ratchet → target fills
        order B and the trade is done."""
        p = plan()
        st = fresh(p)
        tick = self._walk(p, st)

        assert tick(102.0, 1).action == "HOLD"
        assert tick(104.5, 3).trigger == sx.SWING_RAISE_STOP   # breakeven
        assert st.current_stop == pytest.approx(100.0)
        assert tick(107.0, 5).action == "HOLD"                 # no trail in v2
        assert st.current_stop == pytest.approx(100.0)
        d = tick(110.5, 7)
        assert d.trigger == sx.SWING_TARGET_PARTIAL
        assert d.action == "EXIT"
        assert d.r_multiple > 2.0

    def test_full_winner_lifecycle_with_trail_enabled(self, monkeypatch):
        monkeypatch.setattr(sx, "TRAIL_ENABLED", True)
        monkeypatch.setattr(sx, "PARTIAL_FRACTION", 0.5)
        p = plan()
        st = fresh(p)
        tick = self._walk(p, st)

        assert tick(102.0, 1).action == "HOLD"
        assert tick(104.5, 3).trigger == sx.SWING_RAISE_STOP
        assert tick(107.0, 5).stop_basis == "trail"
        assert st.current_stop == pytest.approx(104.0)
        assert tick(110.5, 7).trigger == sx.SWING_TARGET_PARTIAL
        assert st.partial_taken is True
        d = tick(106.0, 9)
        assert d.trigger == sx.SWING_TRAIL_STOP
        assert d.r_multiple > 0

    def test_stop_never_decreases_over_a_noisy_path(self):
        p = plan()
        st = fresh(p)
        seen = []
        for price, day in [(101, 1), (105, 2), (103, 3), (108, 4), (102, 5), (107, 6)]:
            d = evaluate_swing_exit(p, st, current_price=float(price), atr=2.0, days_held=day)
            if d.is_exit:
                break
            st.current_stop = d.new_stop
            st.high_water = d.high_water
            seen.append(d.new_stop)
        assert seen == sorted(seen), f"stop loosened: {seen}"

    def test_loser_exits_at_one_r_not_worse(self):
        p = plan()
        st = fresh(p)
        d = evaluate_swing_exit(p, st, current_price=95.9, atr=2.0, days_held=2)
        assert d.trigger == sx.SWING_STOP
        # The plan budgeted 1R; a same-day gap can overshoot, but the
        # decision must not wait for more.
        assert d.r_multiple <= -1.0


class TestRendering:
    def test_exit_alert_names_the_action(self):
        p = plan()
        d = evaluate_swing_exit(p, fresh(p), current_price=95.0, atr=2.0, days_held=3)
        out = sx.format_exit_alert("TEST", d, days_held=3)
        assert "TEST" in out
        assert "fecha a posição" in out.lower()
        assert "R" in out

    def test_target_alert_says_close_the_position(self):
        p = plan()
        d = evaluate_swing_exit(p, fresh(p), current_price=111.0, atr=2.0, days_held=6)
        out = sx.format_exit_alert("TEST", d, days_held=6)
        assert "fecha a posição" in out.lower()

    def test_partial_alert_states_the_fraction(self, monkeypatch):
        monkeypatch.setattr(sx, "PARTIAL_FRACTION", 0.5)
        p = plan()
        d = evaluate_swing_exit(p, fresh(p), current_price=111.0, atr=2.0, days_held=6)
        out = sx.format_exit_alert("TEST", d, days_held=6)
        assert "50%" in out

    def test_raise_stop_alert_gives_the_level(self):
        p = plan()
        d = evaluate_swing_exit(p, fresh(p), current_price=104.5, atr=2.0, days_held=4)
        out = sx.format_exit_alert("TEST", d, days_held=4)
        assert "100.00" in out


class TestRetunedDefaults:
    """Phase 3 changed defaults that live outside swing_exits. These pin the
    new values so a silent revert to the multi-month calibration is caught."""

    def test_conviction_atr_multiplier_is_swing_scale(self, monkeypatch):
        monkeypatch.delenv("CONVICTION_ATR_MULTIPLIER", raising=False)
        import importlib
        from core import conviction_radar
        importlib.reload(conviction_radar)
        assert conviction_radar._ATR_MULTIPLIER == pytest.approx(1.5)
        assert conviction_radar._ATR_MIN_STOP_PCT == pytest.approx(0.04)
        assert conviction_radar._DIP_TIME_DECAY_DAYS == 21

    def test_swing_is_a_valid_position_type(self):
        from core import conviction_radar
        assert "SWING" in conviction_radar.VALID_TYPES

    def test_mechanical_triggers_bypass_never_sell(self):
        """A stop the user can opt out of is not a stop."""
        from core import conviction_radar as cr
        ev = {
            "ticker": "TEST",
            "triggers": [{"kind": "SWING_STOP", "severity": "high",
                          "message": "stop accionado"}],
        }
        pos = cr.ConvictionPosition(
            ticker="TEST", entry_price=100.0, shares=1.0,
            entry_date="2026-07-01", position_type="SWING", never_sell=True,
        )
        verdict = cr._hold_close_verdict(ev, pos)
        assert "FECHAR" in verdict

    def test_mechanical_outranks_structural(self):
        from core import conviction_radar as cr
        ev = {
            "ticker": "TEST",
            "triggers": [
                {"kind": "FUNDAMENTAL_BREAKDOWN", "severity": "high", "message": "x"},
                {"kind": "SWING_TIME_STOP", "severity": "med", "message": "time"},
            ],
        }
        assert "TIME_STOP" in cr._hold_close_verdict(ev, None)


class TestConvictionIntegration:
    """End-to-end through conviction_radar.evaluate_conviction — the path
    the 22h05 daily check and the hourly stop sweep actually take."""

    @pytest.fixture
    def cr_stub(self, monkeypatch):
        from core import conviction_radar as cr
        # Neutralise every network call. get_fundamentals returning skip
        # short-circuits the ML/quality block without touching yfinance.
        monkeypatch.setattr(cr, "_fetch_price_history", lambda *a, **k: None)
        monkeypatch.setattr(cr, "_detect_event_driven_signals", lambda t: ([], []))
        import sys, types
        mc = types.ModuleType("data.market_client")
        mc.get_fundamentals = lambda *a, **k: {"skip": True}
        monkeypatch.setitem(sys.modules, "data.market_client", mc)
        return cr

    def _pos(self, cr, **kw):
        base = dict(
            ticker="TEST", entry_price=100.0, shares=10.0,
            entry_date="2026-07-20", position_type="SWING",
            plan_entry=100.0, plan_stop=96.0, plan_target=110.0,
            plan_time_stop_days=15, setup_type="PULLBACK",
            swing_stop=96.0, swing_high_water=100.0,
        )
        base.update(kw)
        return cr.ConvictionPosition(**base)

    def test_stop_break_produces_high_severity_trigger(self, cr_stub, monkeypatch):
        monkeypatch.setattr(cr_stub, "_fetch_price", lambda *a, **k: 95.0)
        ev = cr_stub.evaluate_conviction(self._pos(cr_stub))
        kinds = [t["kind"] for t in ev["triggers"]]
        assert "SWING_STOP" in kinds
        assert ev["swing_r_multiple"] < -1.0

    def test_ratchet_is_written_back_to_the_position(self, cr_stub, monkeypatch):
        """evaluate_conviction must mutate the record so the caller's save
        persists the new stop — otherwise the ratchet resets every tick."""
        monkeypatch.setattr(cr_stub, "_fetch_price", lambda *a, **k: 105.0)
        pos = self._pos(cr_stub)
        cr_stub.evaluate_conviction(pos)
        assert pos.swing_stop == pytest.approx(100.0)   # breakeven
        assert pos.swing_high_water == pytest.approx(105.0)

    def test_partial_flag_persists(self, cr_stub, monkeypatch):
        monkeypatch.setattr(cr_stub, "_fetch_price", lambda *a, **k: 111.0)
        pos = self._pos(cr_stub)
        cr_stub.evaluate_conviction(pos)
        assert pos.swing_partial_taken is True

    def test_missing_plan_reports_instead_of_guessing(self, cr_stub, monkeypatch):
        monkeypatch.setattr(cr_stub, "_fetch_price", lambda *a, **k: 90.0)
        pos = self._pos(cr_stub, plan_stop=None, plan_target=None, swing_stop=0.0)
        ev = cr_stub.evaluate_conviction(pos)
        kinds = [t["kind"] for t in ev["triggers"]]
        assert "SWING_PLAN_MISSING" in kinds
        assert "SWING_STOP" not in kinds

    def test_swing_ignores_ml_and_fundamental_triggers(self, cr_stub, monkeypatch):
        """A SWING position must not inherit DIP's EDGE_GONE / DETERIORATION
        or CORE's quality triggers — the plan is the whole thesis."""
        monkeypatch.setattr(cr_stub, "_fetch_price", lambda *a, **k: 102.0)
        pos = self._pos(cr_stub, entry_win_prob=0.9, entry_quality_score=0.9)
        ev = cr_stub.evaluate_conviction(pos)
        kinds = {t["kind"] for t in ev["triggers"]}
        assert not (kinds & {"EDGE_GONE", "DETERIORATION", "QUALITY_DECLINE",
                             "TIME_DECAY", "TRAIL_STOP_CONFIRMED"})

    def test_calendar_horizon_used_not_bar_count(self, cr_stub, monkeypatch):
        """15 trading days = 21 calendar days. A position 17 calendar days
        old has only ~12 bars and must NOT hit its time stop yet."""
        from datetime import date, timedelta
        monkeypatch.setattr(cr_stub, "_fetch_price", lambda *a, **k: 101.0)
        entry = (date.today() - timedelta(days=17)).isoformat()
        ev = cr_stub.evaluate_conviction(self._pos(cr_stub, entry_date=entry))
        assert "SWING_TIME_STOP" not in [t["kind"] for t in ev["triggers"]]

        entry = (date.today() - timedelta(days=21)).isoformat()
        ev = cr_stub.evaluate_conviction(self._pos(cr_stub, entry_date=entry))
        assert "SWING_TIME_STOP" in [t["kind"] for t in ev["triggers"]]


class TestPlanAttachment:
    def test_set_swing_plan_rejects_stop_above_entry(self, monkeypatch):
        from core import conviction_radar as cr
        monkeypatch.setattr(cr, "get_by_ticker", lambda t: cr.ConvictionPosition(
            ticker="TEST", entry_price=100.0, shares=1.0,
            entry_date="2026-07-20", position_type="MOMENTUM"))
        monkeypatch.setattr(cr, "update_conviction", lambda p: True)
        with pytest.raises(ValueError, match="abaixo do entry"):
            cr.set_swing_plan("TEST", 100.0, 105.0, 110.0)

    def test_set_swing_plan_rejects_target_below_entry(self, monkeypatch):
        from core import conviction_radar as cr
        monkeypatch.setattr(cr, "get_by_ticker", lambda t: cr.ConvictionPosition(
            ticker="TEST", entry_price=100.0, shares=1.0,
            entry_date="2026-07-20", position_type="MOMENTUM"))
        monkeypatch.setattr(cr, "update_conviction", lambda p: True)
        with pytest.raises(ValueError, match="acima do entry"):
            cr.set_swing_plan("TEST", 100.0, 96.0, 99.0)

    def test_set_swing_plan_switches_type_and_clears_never_sell(self, monkeypatch):
        from core import conviction_radar as cr
        pos = cr.ConvictionPosition(
            ticker="TEST", entry_price=100.0, shares=1.0,
            entry_date="2026-07-20", position_type="CORE", never_sell=True)
        monkeypatch.setattr(cr, "get_by_ticker", lambda t: pos)
        monkeypatch.setattr(cr, "update_conviction", lambda p: True)
        out = cr.set_swing_plan("TEST", 100.0, 96.0, 110.0)
        assert out.position_type == "SWING"
        assert out.never_sell is False
        assert out.swing_stop == pytest.approx(96.0)

    @pytest.fixture
    def proposal(self, monkeypatch, tmp_path):
        """Write a last_cash_proposal.json and point the lookup at it."""
        import json
        from datetime import datetime, timedelta
        from core import conviction_radar as cr

        path = tmp_path / "last_cash_proposal.json"
        monkeypatch.setattr(cr, "_proposal_path", lambda: path)

        def _write(age_days=0, scorer="swing", **overrides):
            action = {"ticker": "TEST", "entry": 100.0, "stop": 96.0,
                      "target": 110.0, "time_stop_days": 15,
                      "setup_type": "PULLBACK"}
            action.update(overrides)
            ts = (datetime.now() - timedelta(days=age_days)).isoformat(timespec="seconds")
            path.write_text(json.dumps({
                "timestamp": ts, "scorer_used": scorer, "actions": [action],
            }))
        return _write

    def test_plan_recovery_reads_a_fresh_proposal(self, proposal):
        """Positive control — without this the staleness test below could
        pass simply because nothing is ever found."""
        from core import conviction_radar as cr
        proposal(age_days=1)
        plan = cr._recover_swing_plan("TEST")
        assert plan is not None
        assert plan["stop"] == 96.0
        assert plan["target"] == 110.0
        assert plan["setup_type"] == "PULLBACK"

    def test_plan_recovery_ignores_stale_proposal(self, proposal):
        """A 30-day-old proposal describes price levels that no longer
        exist. Applying its stop silently would be worse than none."""
        from core import conviction_radar as cr
        proposal(age_days=30)
        assert cr._recover_swing_plan("TEST") is None

    def test_plan_recovery_ignores_non_swing_proposal(self, proposal):
        from core import conviction_radar as cr
        proposal(age_days=1, scorer="watchlist")
        assert cr._recover_swing_plan("TEST") is None

    def test_plan_recovery_ignores_other_tickers(self, proposal):
        from core import conviction_radar as cr
        proposal(age_days=1)
        assert cr._recover_swing_plan("OTHER") is None

    def test_plan_recovery_requires_complete_levels(self, proposal):
        from core import conviction_radar as cr
        proposal(age_days=1, stop=None)
        assert cr._recover_swing_plan("TEST") is None

    def test_plan_recovery_survives_missing_file(self, monkeypatch, tmp_path):
        from core import conviction_radar as cr
        monkeypatch.setattr(cr, "_proposal_path", lambda: tmp_path / "nope.json")
        assert cr._recover_swing_plan("TEST") is None
