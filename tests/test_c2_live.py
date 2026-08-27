"""tests/test_c2_live.py — acceptance tests for the live report engine.

This module tells a person what to do with real money, so the tests target
the ways it could quietly be WRONG rather than the ways it could crash:
signalling a ticker already held, inventing a free slot, mis-dating the
valve, or measuring slippage against the wrong reference.
"""
from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest

from core.c2_live import (
    K1_EQUITY_FLOOR, LIVE_INSTRUMENTS, SLOTS, Position, PositionsFile,
    build_report, evaluate_instrument, load_positions, recommended_slot_dollars,
    render_report, shares_for, trading_days_since, write_positions_template,
)
from core.c2_live_log import (
    LOG_COLUMNS, append_row, ensure_log, k2_status, k3_review, official_open,
    read_log, reconcile, slippage_pct,
)
from strategies.rsi2 import C2_CUM_ENTRY, CUM_RSI_EXIT, TIME_VALVE_BARS


# ─────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────

def bars_from(closes, start="2025-01-02"):
    idx = pd.date_range(start, periods=len(closes), freq="B")
    c = np.asarray(closes, dtype=float)
    return pd.DataFrame({"open": c, "high": c * 1.001, "low": c * 0.999,
                         "close": c, "volume": np.full(len(c), 1e6)}, index=idx)


def rising_then_dip(n=300, dip_pct=0.06):
    """A long uptrend (so close > SMA200) ending in a sharp two-day drop —
    which is what drives RSI(2) toward zero and fires the C2 entry."""
    base = list(100 * (1.0015 ** np.arange(n - 2)))
    last = base[-1]
    return base + [last * (1 - dip_pct / 2), last * (1 - dip_pct)]


def rising_then_pop(n=300):
    """Uptrend ending in two strong up days — RSI(2) above 65, the exit."""
    base = list(100 * (1.0015 ** np.arange(n - 2)))
    last = base[-1]
    return base + [last * 1.02, last * 1.04]


def flat_positions(**kw):
    return PositionsFile(account_equity=kw.pop("equity", 3000.0),
                         slot_dollars=kw.pop("slot_dollars", 1000.0),
                         equity_as_of="2026-08-04",
                         open=kw.pop("open", []), pending=kw.pop("pending", []))


# ─────────────────────────────────────────────────────────────────────
# Registration — the frozen system
# ─────────────────────────────────────────────────────────────────────

class TestRegistration:
    def test_thirteen_instruments_including_spy(self):
        assert len(LIVE_INSTRUMENTS) == 13
        assert "SPY" in LIVE_INSTRUMENTS

    def test_no_duplicates(self):
        assert len(set(LIVE_INSTRUMENTS)) == 13

    def test_thresholds_come_from_the_validated_module(self):
        # Not re-typed here: a divergent copy is how a live system stops
        # being the system that passed validation.
        assert C2_CUM_ENTRY == 35.0
        assert CUM_RSI_EXIT == 65.0
        assert TIME_VALVE_BARS == 25

    def test_three_slots_and_the_kill_switch(self):
        assert SLOTS == 3
        assert K1_EQUITY_FLOOR == 2700.0

    def test_slot_dollars_rounds_down_to_fifty(self):
        assert recommended_slot_dollars(3000.0) == 1000.0
        assert recommended_slot_dollars(3149.0) == 1000.0   # 1049.67 -> 1000
        assert recommended_slot_dollars(3200.0) == 1050.0   # 1066.67 -> 1050
        assert recommended_slot_dollars(2700.0) == 900.0

    def test_slot_dollars_never_rounds_up(self):
        for eq in (2999.0, 3001.0, 3449.0, 4499.0):
            assert recommended_slot_dollars(eq) <= eq / 3


# ─────────────────────────────────────────────────────────────────────
# Signal evaluation
# ─────────────────────────────────────────────────────────────────────

class TestSignals:
    def test_dip_in_uptrend_fires_entry(self):
        s = evaluate_instrument(bars_from(rising_then_dip()), "XLK")
        assert s.above_sma is True
        assert s.cum_rsi < C2_CUM_ENTRY
        assert s.entry_signal is True

    def test_pop_fires_exit(self):
        s = evaluate_instrument(bars_from(rising_then_pop()), "XLK")
        assert s.rsi2 > CUM_RSI_EXIT
        assert s.exit_signal is True

    def test_below_sma200_cannot_signal_entry(self):
        # A dip is not enough; the trend filter is the first seatbelt.
        closes = list(100 * (0.999 ** np.arange(298)))
        closes += [closes[-1] * 0.97, closes[-1] * 0.94]
        s = evaluate_instrument(bars_from(closes), "XLE")
        assert s.above_sma is False
        assert s.entry_signal is False

    def test_uptrend_without_a_dip_does_not_signal(self):
        s = evaluate_instrument(bars_from(list(100 * (1.0015 ** np.arange(300)))),
                                "SPY")
        assert s.above_sma is True
        assert s.entry_signal is False


class TestTradingDays:
    def test_counts_sessions_not_calendar_days(self):
        b = bars_from(list(range(100, 130)))          # business days
        d0 = str(b.index[0].date())
        assert trading_days_since(b, d0) == len(b) - 1

    def test_same_day_is_zero(self):
        b = bars_from(list(range(100, 130)))
        assert trading_days_since(b, str(b.index[-1].date())) == 0

    def test_unknown_date_returns_none_not_zero(self):
        # Returning 0 would silently read as "entered today" and reset the
        # valve clock every evening.
        b = bars_from(list(range(100, 130)))
        assert trading_days_since(b, "1999-01-04") is None


class TestShareSizing:
    def test_shares_hit_the_slot_amount(self):
        sh = shares_for(1000.0, 261.34)
        assert abs(sh * 261.34 - 1000.0) <= 5.0

    def test_zero_price_is_not_a_division_error(self):
        assert shares_for(1000.0, 0.0) == 0.0


# ─────────────────────────────────────────────────────────────────────
# The report — slot discipline is where money is at stake
# ─────────────────────────────────────────────────────────────────────

class TestReportSlots:
    def _bars(self, signal_tickers, n=300):
        out = {}
        for t in LIVE_INSTRUMENTS:
            out[t] = bars_from(rising_then_dip() if t in signal_tickers
                               else list(100 * (1.0015 ** np.arange(n))))
        return out

    def test_free_slots_limit_entries(self):
        bars = self._bars({"XLK", "XLV", "XLY", "XLU", "XLP"})
        r = build_report(bars, flat_positions())
        assert len(r["entries"]) == SLOTS
        assert len(r["entries_deferred"]) == 2

    def test_deferred_entries_are_not_orders(self):
        bars = self._bars({"XLK", "XLV", "XLY", "XLU"})
        r = build_report(bars, flat_positions())
        for e in r["entries_deferred"]:
            assert "reason_deferred" in e
        txt = render_report(r)
        assert "Do NOT place this" in txt

    def test_entries_are_listed_lowest_cum_rsi_first(self):
        bars = self._bars({"XLK", "XLV", "XLY"})
        r = build_report(bars, flat_positions())
        vals = [e["cum_rsi"] for e in r["entries"]]
        assert vals == sorted(vals)

    def test_held_ticker_is_never_re_signalled(self):
        bars = self._bars({"XLK", "XLV"})
        pos = flat_positions(open=[Position("XLK", "2026-07-20", "2026-07-21",
                                            100.0, 10.0)])
        r = build_report(bars, pos)
        assert "XLK" not in [e["ticker"] for e in r["entries"]]

    def test_pending_order_holds_its_slot(self):
        # The killer bug: an order placed last night but not yet confirmed
        # must not free a slot, or you end up with four positions.
        bars = self._bars({"XLK", "XLV", "XLY", "XLU"})
        pos = flat_positions(pending=[{"ticker": "XLU", "side": "entry",
                                       "signal_date": "2026-08-03"}])
        r = build_report(bars, pos)
        assert r["slots"]["free"] == 2
        assert len(r["entries"]) == 2
        assert "XLU" not in [e["ticker"] for e in r["entries"]]

    def test_exiting_position_does_not_free_a_slot_tonight(self):
        # §1: "a slot frees the session AFTER its exit fills."
        bars = self._bars({"XLV"})
        bars["XLK"] = bars_from(rising_then_pop())
        pos = flat_positions(open=[Position("XLK", "2026-07-20", "2026-07-21",
                                            100.0, 10.0)])
        r = build_report(bars, pos)
        assert len(r["exits"]) == 1
        assert r["slots"]["free"] == 2          # 3 - 1 open, exit not counted
        assert len(r["entries"]) <= 2

    def test_full_book_defers_everything(self):
        bars = self._bars({"XLK", "XLV"})
        pos = flat_positions(open=[
            Position("XLB", "2026-07-20", "2026-07-21", 100.0, 10.0),
            Position("XLE", "2026-07-20", "2026-07-21", 100.0, 10.0),
            Position("XLF", "2026-07-20", "2026-07-21", 100.0, 10.0)])
        r = build_report(bars, pos)
        assert r["slots"]["free"] == 0
        assert r["entries"] == []


class TestReportExits:
    def test_rsi_exit_is_flagged_with_an_order(self):
        bars = {t: bars_from(list(100 * (1.0015 ** np.arange(300))))
                for t in LIVE_INSTRUMENTS}
        bars["XLK"] = bars_from(rising_then_pop())
        entry_date = str(bars["XLK"].index[-6].date())
        sig_date = str(bars["XLK"].index[-7].date())
        pos = flat_positions(open=[Position("XLK", sig_date, entry_date,
                                            100.0, 10.0)])
        r = build_report(bars, pos)
        assert r["exits"][0]["exit_reason"] == "RSI2 > 65"
        assert "MARKET ON OPEN" in render_report(r)

    # NOTE on the fixture: these two use `rising_then_dip`, not the plain
    # uptrend. On a monotonically rising series RSI(2) is 100, so the RSI
    # exit fires first and the valve is never reached — the test would pass
    # or fail for the wrong reason. The dip ending keeps RSI(2) low so the
    # valve is the only thing under test.

    def test_valve_fires_25_sessions_after_the_SIGNAL_date(self):
        # The backtest counts held bars from the signal bar, not the entry
        # bar. Counting from entry would hold every position one day too
        # long.
        bars = {t: bars_from(rising_then_dip()) for t in LIVE_INSTRUMENTS}
        b = bars["XLP"]
        sig = str(b.index[-(TIME_VALVE_BARS + 1)].date())
        entry = str(b.index[-TIME_VALVE_BARS].date())
        pos = flat_positions(open=[Position("XLP", sig, entry, 100.0, 10.0)])
        r = build_report(bars, pos)
        assert r["exits"][0]["exit_reason"] == "25-DAY VALVE"

    def test_valve_does_not_fire_one_session_early(self):
        bars = {t: bars_from(rising_then_dip()) for t in LIVE_INSTRUMENTS}
        b = bars["XLP"]
        sig = str(b.index[-TIME_VALVE_BARS].date())
        entry = str(b.index[-(TIME_VALVE_BARS - 1)].date())
        pos = flat_positions(open=[Position("XLP", sig, entry, 100.0, 10.0)])
        r = build_report(bars, pos)
        assert r["exits"] == []

    def test_rsi_exit_takes_precedence_over_the_valve(self):
        # Both conditions true at once: the report must name the RSI rule,
        # matching the backtest's evaluation order.
        bars = {t: bars_from(rising_then_pop()) for t in LIVE_INSTRUMENTS}
        b = bars["XLP"]
        sig = str(b.index[-(TIME_VALVE_BARS + 1)].date())
        entry = str(b.index[-TIME_VALVE_BARS].date())
        pos = flat_positions(open=[Position("XLP", sig, entry, 100.0, 10.0)])
        r = build_report(bars, pos)
        assert r["exits"][0]["exit_reason"] == "RSI2 > 65"

    def test_unknown_signal_date_is_surfaced_not_silently_zero(self):
        bars = {t: bars_from(list(100 * (1.0015 ** np.arange(300))))
                for t in LIVE_INSTRUMENTS}
        pos = flat_positions(open=[Position("XLP", "1999-01-04", "1999-01-05",
                                            100.0, 10.0)])
        r = build_report(bars, pos)
        assert r["positions"][0]["date_unknown"] is True
        assert "cannot be computed" in render_report(r)

    def test_unrealized_pl_is_reported(self):
        bars = {t: bars_from(list(100 * (1.0015 ** np.arange(300))))
                for t in LIVE_INSTRUMENTS}
        last = float(bars["XLV"]["close"].iloc[-1])
        pos = flat_positions(open=[Position("XLV", str(bars["XLV"].index[-5].date()),
                                            str(bars["XLV"].index[-4].date()),
                                            last / 2, 4.0)])
        p = build_report(bars, pos)["positions"][0]
        assert p["unrealized_pct"] == pytest.approx(100.0, abs=0.5)
        assert p["unrealized_pl"] == pytest.approx(last * 4.0 / 2, rel=0.02)


class TestReportSafety:
    def test_k1_breach_is_loud(self):
        bars = {t: bars_from(list(100 * (1.0015 ** np.arange(300))))
                for t in LIVE_INSTRUMENTS}
        r = build_report(bars, flat_positions(equity=2699.0))
        assert r["k1"]["breached"] is True
        assert "K1 KILL SWITCH BREACHED" in render_report(r)

    def test_nothing_to_do_is_stated_plainly(self):
        bars = {t: bars_from(list(100 * (1.0015 ** np.arange(300))))
                for t in LIVE_INSTRUMENTS}
        # `today` is pinned to the fixture's last bar: otherwise the
        # staleness guard correctly refuses to issue an all-clear.
        last = str(list(bars.values())[0].index[-1].date())
        r = build_report(bars, flat_positions(), today=last)
        assert r["nothing_to_do"] is True
        assert "NOTHING TO DO TONIGHT" in render_report(r)

    def test_missing_instrument_is_reported_not_skipped_silently(self):
        bars = {t: bars_from(list(100 * (1.0015 ** np.arange(300))))
                for t in LIVE_INSTRUMENTS if t != "XLU"}
        r = build_report(bars, flat_positions())
        assert "XLU" in r["instruments_missing_data"]
        txt = render_report(r)
        assert "DATA INCOMPLETE" in txt and "NOT EVALUATED" in txt

    def test_short_history_counts_as_missing(self):
        bars = {t: bars_from(list(100 * (1.0015 ** np.arange(300))))
                for t in LIVE_INSTRUMENTS}
        bars["XLB"] = bars_from(list(range(100, 150)))       # < 201 bars
        r = build_report(bars, flat_positions())
        assert "XLB" in r["instruments_missing_data"]

    def test_stale_slot_dollars_prompts_a_monthly_resize(self):
        bars = {t: bars_from(list(100 * (1.0015 ** np.arange(300))))
                for t in LIVE_INSTRUMENTS}
        r = build_report(bars, flat_positions(equity=4500.0, slot_dollars=1000.0))
        assert r["slots"]["slot_dollars_stale"] is True
        assert "Monthly resize due" in render_report(r)

    def test_incomplete_current_session_bar_is_dropped(self):
        # The real 2026-08-03 failure: yfinance appends a placeholder row for
        # the still-open session with NaN OHLC and partial volume. Because
        # `np.where(NaN > 0, ...)` is False, the validated RSI scores it as a
        # FLAT DAY and returns a confident wrong number instead of NaN.
        good = bars_from(rising_then_dip())
        broken = good.copy()
        broken.loc[broken.index[-1] + pd.Timedelta(days=1)] = {
            "open": np.nan, "high": np.nan, "low": np.nan, "close": np.nan,
            "volume": 51_941_340}
        assert evaluate_instrument(broken, "SPY").close == pytest.approx(
            evaluate_instrument(good, "SPY").close)

    def test_signal_survives_the_placeholder_bar(self):
        # The dangerous half: the bug SUPPRESSED signals, so it looked calm.
        good = bars_from(rising_then_dip())
        broken = good.copy()
        broken.loc[broken.index[-1] + pd.Timedelta(days=1)] = {
            "open": np.nan, "high": np.nan, "low": np.nan, "close": np.nan,
            "volume": 1}
        assert evaluate_instrument(good, "XLK").entry_signal is True
        assert evaluate_instrument(broken, "XLK").entry_signal is True

    def test_clean_bars_reports_what_it_dropped(self):
        from core.c2_live import clean_bars
        b = bars_from([100.0, 101.0, 102.0])
        b.loc[b.index[-1] + pd.Timedelta(days=1)] = {
            "open": np.nan, "high": np.nan, "low": np.nan, "close": np.nan,
            "volume": 5}
        cleaned, dropped = clean_bars(b)
        assert dropped == 1 and len(cleaned) == 3

    def test_unevaluable_instrument_is_not_reported_as_no_signal(self):
        # "I could not check" must never render as "nothing to do".
        bars = {t: bars_from(list(100 * (1.0015 ** np.arange(300))))
                for t in LIVE_INSTRUMENTS}
        bars["XLU"] = bars_from([np.nan] * 300)
        r = build_report(bars, flat_positions())
        assert r["data_ok"] is False
        assert r["nothing_to_do"] is False
        txt = render_report(r)
        assert "CANNOT ADVISE" in txt
        assert "NOTHING TO DO TONIGHT" not in txt

    def test_all_data_present_is_a_clean_all_clear(self):
        bars = {t: bars_from(list(100 * (1.0015 ** np.arange(300))))
                for t in LIVE_INSTRUMENTS}
        last = str(list(bars.values())[0].index[-1].date())
        r = build_report(bars, flat_positions(), today=last)
        assert r["data_ok"] is True
        assert r["n_instruments"] == r["n_expected"] == 13

    def test_yesterdays_close_is_still_actionable(self):
        bars = {t: bars_from(list(100 * (1.0015 ** np.arange(300))))
                for t in LIVE_INSTRUMENTS}
        idx = list(bars.values())[0].index
        r = build_report(bars, flat_positions(), today=str(idx[-1].date()))
        assert r["sessions_behind"] == 0 and r["is_stale"] is False

    def test_old_data_is_flagged_and_blocks_the_all_clear(self):
        # A stale report looks exactly like a fresh one — that is why this
        # has to be an explicit check rather than a reader's job.
        bars = {t: bars_from(list(100 * (1.0015 ** np.arange(300))))
                for t in LIVE_INSTRUMENTS}
        idx = list(bars.values())[0].index
        stale_today = str((idx[-1] + pd.Timedelta(days=7)).date())
        r = build_report(bars, flat_positions(), today=stale_today)
        assert r["is_stale"] is True
        assert r["data_ok"] is False
        assert r["nothing_to_do"] is False
        assert "OLD DATA" in render_report(r)

    def test_provisional_bar_is_dropped_while_the_market_is_open(self):
        # THE 2026-08-26 INCIDENT. At 15:02 ET yfinance served a row for the
        # still-trading session. Its "close" was just the last trade, and
        # RSI(2) computed on it read 70.6 (EXIT) against 46.9 on the last
        # completed bar — a manufactured exit on two live positions.
        from datetime import datetime
        from core.c2_live import ET, drop_provisional
        bars = bars_from(list(100 * (1.0015 ** np.arange(300))))
        # Pretend the newest bar is today and it is 15:02 ET.
        idx = list(bars.index[:-1]) + [pd.Timestamp("2026-08-26")]
        bars.index = pd.DatetimeIndex(idx)
        mid_session = datetime(2026, 8, 26, 15, 2, tzinfo=ET)
        out, dropped = drop_provisional(bars, mid_session)
        assert dropped is True
        assert len(out) == len(bars) - 1

    def test_completed_session_is_kept_after_the_close(self):
        from datetime import datetime
        from core.c2_live import ET, drop_provisional
        bars = bars_from(list(100 * (1.0015 ** np.arange(300))))
        idx = list(bars.index[:-1]) + [pd.Timestamp("2026-08-26")]
        bars.index = pd.DatetimeIndex(idx)
        after_close = datetime(2026, 8, 26, 17, 30, tzinfo=ET)
        out, dropped = drop_provisional(bars, after_close)
        assert dropped is False
        assert len(out) == len(bars)

    def test_settle_buffer_holds_just_after_the_bell(self):
        # 16:00-16:10 ET: the bell has rung but the data has not settled.
        from datetime import datetime
        from core.c2_live import ET, provisional_bar_date
        assert provisional_bar_date(datetime(2026, 8, 26, 16, 5, tzinfo=ET)) is not None
        assert provisional_bar_date(datetime(2026, 8, 26, 16, 11, tzinfo=ET)) is None

    def test_weekend_has_no_provisional_session(self):
        from datetime import datetime
        from core.c2_live import ET, provisional_bar_date
        # 2026-08-29 is a Saturday.
        assert provisional_bar_date(datetime(2026, 8, 29, 12, 0, tzinfo=ET)) is None

    def test_yesterdays_bar_is_never_provisional(self):
        from datetime import datetime
        from core.c2_live import ET, drop_provisional
        bars = bars_from(list(100 * (1.0015 ** np.arange(300))))
        idx = list(bars.index[:-1]) + [pd.Timestamp("2026-08-25")]
        bars.index = pd.DatetimeIndex(idx)
        _, dropped = drop_provisional(bars, datetime(2026, 8, 26, 15, 2, tzinfo=ET))
        assert dropped is False

    def test_report_warns_when_it_dropped_a_provisional_bar(self):
        from datetime import datetime
        from core.c2_live import ET
        bars = {}
        for t in LIVE_INSTRUMENTS:
            b = bars_from(list(100 * (1.0015 ** np.arange(300))))
            b.index = pd.DatetimeIndex(list(b.index[:-1]) +
                                       [pd.Timestamp("2026-08-26")])
            bars[t] = b
        r = build_report(bars, flat_positions(), today="2026-08-26",
                         now=datetime(2026, 8, 26, 15, 2, tzinfo=ET))
        assert len(r["provisional_bars_dropped"]) == 13
        assert r["market_still_open"] is True
        assert "still open" in render_report(r)

    def test_report_never_mentions_placing_orders_itself(self):
        bars = {t: bars_from(list(100 * (1.0015 ** np.arange(300))))
                for t in LIVE_INSTRUMENTS}
        txt = render_report(build_report(bars, flat_positions()))
        assert "ANALYSIS ONLY" in txt
        assert "no broker link" in txt


# ─────────────────────────────────────────────────────────────────────
# Positions file
# ─────────────────────────────────────────────────────────────────────

class TestPositionsFile:
    def test_template_round_trips_with_examples_stripped(self, tmp_path):
        p = write_positions_template(tmp_path / "positions.json")
        pf = load_positions(p)
        assert pf.open == [] and pf.pending == []      # examples dropped
        assert pf.slots_free == SLOTS

    def test_missing_file_raises(self, tmp_path):
        # Treating it as an empty portfolio would invent free slots.
        with pytest.raises(FileNotFoundError):
            load_positions(tmp_path / "nope.json")

    def test_incomplete_position_raises(self, tmp_path):
        p = tmp_path / "positions.json"
        p.write_text(json.dumps({"open": [{"ticker": "XLK",
                                           "signal_date": "2026-08-03"}]}))
        with pytest.raises(ValueError, match="missing"):
            load_positions(p)

    def test_empty_pending_row_raises(self, tmp_path):
        # Real incident 2026-08-26: an empty `{}` left in `pending` after
        # deleting a filled order counted as a slot, taking the book to
        # 4/3 used and 0 free. It would have suppressed the next signal
        # while looking like an ordinary quiet market.
        p = tmp_path / "positions.json"
        p.write_text(json.dumps({"open": [], "pending": [{}]}))
        with pytest.raises(ValueError, match="no ticker"):
            load_positions(p)

    def test_pending_row_with_blank_ticker_raises(self, tmp_path):
        p = tmp_path / "positions.json"
        p.write_text(json.dumps({"open": [],
                                 "pending": [{"ticker": "  ", "side": "entry"}]}))
        with pytest.raises(ValueError, match="no ticker"):
            load_positions(p)

    def test_valid_pending_row_still_loads(self, tmp_path):
        p = tmp_path / "positions.json"
        p.write_text(json.dumps({"open": [],
                                 "pending": [{"ticker": "XLE", "side": "entry"}]}))
        assert load_positions(p).slots_used == 1

    def test_underscore_doc_keys_are_ignored(self, tmp_path):
        # The self-documenting template puts _HOWTO / _EXAMPLE_* keys in the
        # file. They must never be parsed as positions.
        p = tmp_path / "positions.json"
        p.write_text(json.dumps({
            "_HOWTO": ["how to use this file"],
            "_EXAMPLE_open_row": {"ticker": "XLV", "signal_date": "2026-09-14",
                                  "entry_date": "2026-09-15",
                                  "entry_price": 163.55, "shares": 6.1},
            "_EXAMPLE_pending_row": {"ticker": "XLV", "side": "entry"},
            "open": [], "pending": [],
        }))
        pf = load_positions(p)
        assert pf.slots_used == 0
        assert pf.held_tickers == set()

    def test_pending_and_open_both_consume_slots(self):
        pf = PositionsFile(open=[Position("XLK", "a", "b", 1.0, 1.0)],
                           pending=[{"ticker": "XLV"}])
        assert pf.slots_used == 2 and pf.slots_free == 1
        assert pf.held_tickers == {"XLK", "XLV"}

    def test_pending_exit_does_not_consume_an_extra_slot(self):
        # The position being sold is still in `open` and holds its own slot
        # until the sale fills (§1). Counting both would report 3/3 used on
        # a 2-position book and suppress a real entry signal.
        pf = PositionsFile(
            open=[Position("XLK", "a", "b", 1.0, 1.0),
                  Position("XLI", "a", "b", 1.0, 1.0)],
            pending=[{"ticker": "XLK", "side": "exit"}])
        assert pf.slots_used == 2
        assert pf.slots_free == 1

    def test_pending_entry_still_consumes_a_slot(self):
        pf = PositionsFile(open=[Position("XLK", "a", "b", 1.0, 1.0)],
                           pending=[{"ticker": "XLV", "side": "entry"}])
        assert pf.slots_used == 2 and pf.slots_free == 1

    def test_missing_side_counts_as_an_entry(self):
        # Errs toward occupying a slot: suppresses a signal rather than
        # over-committing capital.
        pf = PositionsFile(open=[], pending=[{"ticker": "XLV"}])
        assert pf.slots_used == 1

    def test_entries_and_exits_are_partitioned(self):
        pf = PositionsFile(open=[], pending=[
            {"ticker": "A", "side": "entry"},
            {"ticker": "B", "side": "exit"},
            {"ticker": "C"}])
        assert len(pf.pending_entries) == 2      # A and C
        assert len(pf.pending_exits) == 1        # B

    def test_ticker_with_a_pending_exit_is_not_re_signalled(self):
        # You cannot buy what you still own and are selling.
        pf = PositionsFile(open=[Position("XLK", "a", "b", 1.0, 1.0)],
                           pending=[{"ticker": "XLK", "side": "exit"}])
        assert "XLK" in pf.held_tickers

    def test_report_marks_a_position_with_a_placed_sell_order(self):
        bars = {t: bars_from(list(100 * (1.0015 ** np.arange(300))))
                for t in LIVE_INSTRUMENTS}
        bars["XLK"] = bars_from(rising_then_pop())
        last = str(bars["XLK"].index[-1].date())
        pos = flat_positions(
            open=[Position("XLK", str(bars["XLK"].index[-7].date()),
                           str(bars["XLK"].index[-6].date()), 100.0, 10.0)],
            pending=[{"ticker": "XLK", "side": "exit"}])
        txt = render_report(build_report(bars, pos, today=last))
        assert "SELL ORDER PLACED" in txt


# ─────────────────────────────────────────────────────────────────────
# Slippage, K2, K3
# ─────────────────────────────────────────────────────────────────────

class TestSlippage:
    def test_buying_above_the_open_is_a_cost(self):
        assert slippage_pct(100.5, 100.0, "entry") == pytest.approx(0.5)

    def test_buying_below_the_open_is_a_gain(self):
        assert slippage_pct(99.5, 100.0, "entry") == pytest.approx(-0.5)

    def test_selling_below_the_open_is_a_cost(self):
        # Sign must flip on the sell side, or a bad exit fill looks good.
        assert slippage_pct(99.5, 100.0, "exit") == pytest.approx(0.5)

    def test_selling_above_the_open_is_a_gain(self):
        assert slippage_pct(100.5, 100.0, "exit") == pytest.approx(-0.5)

    def test_bad_reference_returns_none(self):
        assert slippage_pct(100.0, 0.0, "entry") is None
        assert slippage_pct(100.0, float("nan"), "entry") is None

    def test_official_open_reads_that_session(self):
        b = bars_from([100, 101, 102])
        d = str(b.index[1].date())
        assert official_open(b, d) == pytest.approx(float(b["open"].iloc[1]))

    def test_official_open_of_a_non_session_is_none(self):
        assert official_open(bars_from([100, 101]), "2099-01-01") is None


class TestReconcile:
    def _log(self, tmp_path, bars):
        p = tmp_path / "trade_log.csv"
        ensure_log(p, LOG_COLUMNS)
        d = str(bars.index[-1].date())
        op = float(bars["open"].iloc[-1])
        append_row(p, {"date": d, "ticker": "XLK", "side": "entry",
                       "signal_date": d, "actual_fill_price": op * 1.001,
                       "shares": 4, "dollar_amount": 1000}, LOG_COLUMNS)
        return p, d, op

    def test_reconcile_fills_reference_and_slippage(self, tmp_path):
        bars = bars_from([100, 101, 102, 103])
        p, d, op = self._log(tmp_path, bars)
        res = reconcile(p, {"XLK": bars})
        assert res["updated"] == 1
        row = read_log(p).iloc[0]
        assert row["modeled_open_price"] == pytest.approx(op, rel=1e-6)
        assert row["slippage_pct"] == pytest.approx(0.1, abs=0.01)

    def test_reconcile_is_idempotent(self, tmp_path):
        bars = bars_from([100, 101, 102, 103])
        p, _, _ = self._log(tmp_path, bars)
        reconcile(p, {"XLK": bars})
        assert reconcile(p, {"XLK": bars})["updated"] == 0

    def test_unknown_date_is_reported_not_guessed(self, tmp_path):
        bars = bars_from([100, 101, 102])
        p = tmp_path / "trade_log.csv"
        ensure_log(p, LOG_COLUMNS)
        append_row(p, {"date": "2099-01-01", "ticker": "XLK", "side": "entry",
                       "actual_fill_price": 100.0}, LOG_COLUMNS)
        res = reconcile(p, {"XLK": bars})
        assert res["updated"] == 0 and res["unresolved"]

    def test_row_without_a_fill_is_left_alone(self, tmp_path):
        bars = bars_from([100, 101, 102])
        p = tmp_path / "trade_log.csv"
        ensure_log(p, LOG_COLUMNS)
        append_row(p, {"date": str(bars.index[-1].date()), "ticker": "XLK",
                       "side": "entry"}, LOG_COLUMNS)
        assert reconcile(p, {"XLK": bars})["updated"] == 0


class TestK2:
    def _fills(self, tmp_path, slips):
        p = tmp_path / "trade_log.csv"
        ensure_log(p, LOG_COLUMNS)
        for i, s in enumerate(slips):
            append_row(p, {"date": f"2026-08-{i+1:02d}", "ticker": "XLK",
                           "side": "entry", "actual_fill_price": 100.0,
                           "modeled_open_price": 100.0, "slippage_pct": s},
                       LOG_COLUMNS)
        return p

    def test_not_enough_data_is_not_a_breach(self, tmp_path):
        k = k2_status(self._fills(tmp_path, [0.9] * 3))
        assert k["enough_data"] is False and k["breached"] is False

    def test_breach_at_ten_fills_over_the_limit(self, tmp_path):
        k = k2_status(self._fills(tmp_path, [0.30] * 10))
        assert k["enough_data"] is True and k["breached"] is True

    def test_within_limit_is_clean(self, tmp_path):
        k = k2_status(self._fills(tmp_path, [0.04] * 10))
        assert k["breached"] is False
        assert k["mean_slippage_pct"] == pytest.approx(0.04)

    def test_only_the_last_ten_count(self, tmp_path):
        p = self._fills(tmp_path, [5.0] * 5 + [0.01] * 10)
        assert k2_status(p)["breached"] is False


class TestK3:
    def _exits(self, tmp_path, pls, amount=1000.0, held=4):
        p = tmp_path / "trade_log.csv"
        ensure_log(p, LOG_COLUMNS)
        for i, pl in enumerate(pls):
            append_row(p, {"date": f"2026-08-{i+1:02d}", "ticker": "XLK",
                           "side": "exit", "pl_on_exit": pl,
                           "dollar_amount": amount, "days_held": held},
                       LOG_COLUMNS)
        return p

    def test_small_sample_extends(self, tmp_path):
        r = k3_review(self._exits(tmp_path, [3.0] * 5), 3050.0)
        assert r["verdict"] == "EXTEND"

    def test_k1_breach_stops(self, tmp_path):
        r = k3_review(self._exits(tmp_path, [3.0] * 12), 2650.0)
        assert r["verdict"] == "STOP" and r["k1_fired"] is True

    def test_low_win_rate_stops_at_twelve_trades(self, tmp_path):
        pls = [-5.0] * 7 + [3.0] * 5          # 41.7% win on 12
        r = k3_review(self._exits(tmp_path, pls), 3000.0)
        assert r["verdict"] == "STOP"
        assert r["win_rate"] == pytest.approx(5 / 12, abs=0.01)

    def test_healthy_quarter_continues(self, tmp_path):
        pls = [3.0] * 9 + [-4.0] * 3          # 75% win on 12
        r = k3_review(self._exits(tmp_path, pls), 3040.0)
        assert r["verdict"] == "CONTINUE"
        # (9*3 - 3*4) / 12 = $1.25 per trade on $1,000 committed = 0.125%
        assert r["avg_pct_per_trade"] == pytest.approx(0.125, abs=0.005)

    def test_review_renders_the_expectation_table(self, tmp_path):
        from core.c2_live_log import render_review
        txt = render_review(k3_review(self._exits(tmp_path, [3.0] * 12), 3040.0))
        assert "Win rate" in txt and "VERDICT" in txt
