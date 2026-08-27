"""core/c2_live.py — the C2-live daily report engine. ANALYSIS ONLY.

`live_trading_protocol.md` draws the hard line and this module stays on the
analysis side of it: **no brokerage connection, no order placement, ever.**
It reads prices, reads a positions file the owner maintains from their own
fills, and prints what a person should do. Nothing here can move money.

THE STRATEGY IS NOT REIMPLEMENTED. Entry, exit and the indicators come from
`strategies/rsi2.py` unchanged — the same functions the C-series and
Addendum 1 validated. If this file disagreed with the backtest, the live
system would be an untested system wearing a validated system's name, which
is the one failure mode the protocol exists to prevent.

TWO PLACES WHERE LIVE DIFFERS FROM BACKTEST, both faced rather than fudged:

1. THE MODELLED REFERENCE PRICE. The protocol asks for a "modeled next-open
   reference price" for the slippage log, and K2 trips at 0.25%/side. But
   nobody can know tomorrow's open tonight. Using tonight's close as the
   reference would measure the OVERNIGHT GAP, not execution quality — ETF
   gaps of 0.5% are ordinary, so K2 would fire constantly for reasons that
   have nothing to do with your broker. So the report carries the last close
   as a SIZING ESTIMATE, and true slippage is computed at reconciliation the
   next day against the session's ACTUAL official open. That is the number
   K2 was written to police.

2. FILLS ARE NEVER ASSUMED. A position exists when the owner records a fill,
   not when this file suggests an order. Orders already placed sit in
   `pending` and hold their slot so the same signal is not issued twice, but
   they are not positions and never enter P/L.

VALVE TIMING, matching the backtest exactly: `replay_variant` counts held
bars from the SIGNAL bar, not the entry bar (`held = j - i`, entry at i+1).
So the 25-day valve fires 25 trading days after the signal date — 24 after
the entry fill. That is why the positions file requires the signal date and
not just the entry date.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, asdict, field
from datetime import datetime, time as dtime, timedelta
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from core.basket_backtest import BASKET_TICKERS
from strategies.rsi2 import (
    C2_CUM_ENTRY, CUM_RSI_EXIT, TIME_VALVE_BARS, Indicators, c2_entry,
    cum_exit, uptrend,
)

log = logging.getLogger("c2_live")

# ── Registered in live_trading_protocol.md §1. Frozen while live. ────
LIVE_INSTRUMENTS = ("SPY",) + BASKET_TICKERS          # 13
SLOTS = 3
DEFAULT_SLOT_DOLLARS = 1000.0
SHARE_TOLERANCE_DOLLARS = 5.0                          # §2: within $5

# §6 kill switch and review gates.
K1_EQUITY_FLOOR = 2700.0
K2_SLIPPAGE_LIMIT_PCT = 0.25
K2_WINDOW_TRADES = 10
K3_MIN_TRADES_FOR_VERDICT = 8
K3_STOP_WIN_RATE = 0.55
K3_STOP_MIN_TRADES = 12

# §9 / §6 expectation anchors, for the review table.
BACKTEST_EXPECTATION = {
    "win_rate": "70-75%",
    "avg_pct_per_trade": "+0.33%",
    "avg_hold_days": "~4",
    "trades_per_quarter": "12-13",
    "avg_slippage_per_side_pct": "<= 0.05%",
}

POSITIONS_TEMPLATE = {
    "_README": [
        "You maintain this file from your ACTUAL Fidelity fills. The bot never",
        "writes to it and never assumes a fill happened.",
        "",
        "open    — a position you actually own. Move a pending order here once",
        "          it fills, filling in entry_date, entry_price and shares.",
        "pending — an order you have PLACED but not yet confirmed filled. It",
        "          holds its slot so tonight's report will not re-signal the",
        "          same ticker, but it is not counted in P/L.",
        "",
        "signal_date is REQUIRED and is the date the signal fired (the evening",
        "you placed the order), not the fill date. The 25-day valve counts from",
        "the signal date, exactly as the backtest does.",
    ],
    "account_equity": 3000.0,
    "slot_dollars": DEFAULT_SLOT_DOLLARS,
    "equity_as_of": "YYYY-MM-DD",
    "open": [
        {
            "ticker": "XLK",
            "signal_date": "2026-08-04",
            "entry_date": "2026-08-05",
            "entry_price": 261.34,
            "shares": 3.8265,
            "cum_rsi_at_signal": 21.4,
            "note": "example row — delete before use",
        }
    ],
    "pending": [
        {
            "ticker": "XLV",
            "side": "entry",
            "signal_date": "2026-08-04",
            "order": "MOO next session, $1000 nominal",
            "note": "example row — delete before use",
        }
    ],
}


# ─────────────────────────────────────────────────────────────────────
# Positions file
# ─────────────────────────────────────────────────────────────────────

@dataclass
class Position:
    ticker:            str
    signal_date:       str
    entry_date:        str
    entry_price:       float
    shares:            float
    cum_rsi_at_signal: Optional[float] = None
    note:              str = ""

    @property
    def cost_basis(self) -> float:
        return self.entry_price * self.shares


@dataclass
class PositionsFile:
    account_equity: float = 3000.0
    slot_dollars:   float = DEFAULT_SLOT_DOLLARS
    equity_as_of:   str = ""
    open:           list[Position] = field(default_factory=list)
    pending:        list[dict] = field(default_factory=list)
    path:           Optional[str] = None

    @property
    def pending_entries(self) -> list[dict]:
        """Pending BUY orders — the only kind that claims an extra slot.

        A pending EXIT does not: the position it sells is still sitting in
        `open` and is already holding that slot until the sale fills (§1,
        "a slot frees the session AFTER its exit fills"). Counting both
        would take a book of 2 positions with 1 pending exit to 3 slots
        used and 0 free, suppressing a legitimate new entry signal.

        A row with a missing or unrecognised `side` counts as an entry.
        That errs toward occupying a slot, which suppresses a signal rather
        than over-committing capital — the safe direction to be wrong.
        """
        return [q for q in self.pending
                if not str(q.get("side", "")).strip().lower().startswith("exit")]

    @property
    def pending_exits(self) -> list[dict]:
        return [q for q in self.pending if q not in self.pending_entries]

    @property
    def slots_used(self) -> int:
        """Open positions plus pending BUYS. See `pending_entries`."""
        return len(self.open) + len(self.pending_entries)

    @property
    def slots_free(self) -> int:
        return max(SLOTS - self.slots_used, 0)

    @property
    def held_tickers(self) -> set[str]:
        return ({p.ticker for p in self.open}
                | {str(q.get("ticker", "")).upper() for q in self.pending})


def write_positions_template(path) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(POSITIONS_TEMPLATE, indent=2))
    return p


def load_positions(path) -> PositionsFile:
    """Read the owner's file. Missing file is an ERROR, not an empty portfolio.

    Silently treating an unreadable file as "no positions" would report free
    slots that are not free and re-signal tickers already held — the one
    mistake in this module that could actually cost money.
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(
            f"positions file {p} not found — run with --init-positions to "
            f"create the template, then fill it in from your real fills")
    raw = json.loads(p.read_text())

    open_rows = []
    for r in raw.get("open", []):
        if str(r.get("note", "")).startswith("example row"):
            continue
        missing = [k for k in ("ticker", "signal_date", "entry_date",
                               "entry_price", "shares") if r.get(k) in (None, "")]
        if missing:
            raise ValueError(f"open position {r.get('ticker')!r} is missing "
                             f"{', '.join(missing)} — the report cannot be "
                             f"trusted until it is complete")
        open_rows.append(Position(
            ticker=str(r["ticker"]).upper(),
            signal_date=str(r["signal_date"]),
            entry_date=str(r["entry_date"]),
            entry_price=float(r["entry_price"]),
            shares=float(r["shares"]),
            cum_rsi_at_signal=(float(r["cum_rsi_at_signal"])
                               if r.get("cum_rsi_at_signal") is not None else None),
            note=str(r.get("note", "")),
        ))

    pending = [r for r in raw.get("pending", [])
               if not str(r.get("note", "")).startswith("example row")]

    # A pending row with no ticker is a hand-editing artefact — most often
    # an empty `{}` left behind after deleting a filled order. It is not
    # harmless: `slots_used` counts it, so it silently occupies one of the
    # three slots and suppresses a real signal. Fails loud, like an
    # incomplete open position does, because the quiet version of this bug
    # looks exactly like an ordinary quiet market.
    for k, r in enumerate(pending):
        if not str(r.get("ticker", "")).strip():
            raise ValueError(
                f"pending[{k}] has no ticker: {r!r} — delete the row if the "
                f"order is gone, or fill in the ticker. An empty row still "
                f"holds a slot and would hide a real signal.")

    return PositionsFile(
        account_equity=float(raw.get("account_equity", 3000.0)),
        slot_dollars=float(raw.get("slot_dollars", DEFAULT_SLOT_DOLLARS)),
        equity_as_of=str(raw.get("equity_as_of", "")),
        open=open_rows, pending=pending, path=str(p),
    )


def recommended_slot_dollars(account_equity: float) -> float:
    """§1: account/3, rounded DOWN to the nearest $50. Monthly, not daily."""
    return float(int(account_equity / SLOTS / 50.0) * 50)


# ─────────────────────────────────────────────────────────────────────
# Signal evaluation — reuses the validated functions verbatim
# ─────────────────────────────────────────────────────────────────────

@dataclass
class InstrumentState:
    ticker:      str
    as_of:       str
    close:       float
    sma200:      float
    above_sma:   bool
    rsi2:        float
    cum_rsi:     float
    entry_signal: bool
    exit_signal:  bool
    bars:        int
    bars_dropped: int = 0


ET = ZoneInfo("America/New_York")
REGULAR_CLOSE_ET = dtime(16, 0)
SETTLE_MINUTES = 10          # data needs a few minutes to finalise post-bell


def provisional_bar_date(now: Optional[datetime] = None):
    """The date whose bar is still FORMING right now, or None.

    yfinance returns a row for the current session while it is still
    trading. That row looks exactly like a finished bar — real open, high,
    low, and a "close" that is simply the last trade — so nothing about its
    shape reveals that the day is not over.

    Evaluating it computes RSI(2) on a price that is still moving. The
    2026-08-26 incident: at 15:02 ET, XLK's in-progress bar gave RSI(2)
    70.6 (EXIT) while the last COMPLETED bar gave 46.9 (hold), and the
    value drifted from 66.5 to 70.6 within the same hour. The report was
    inventing an exit that did not exist.

    This is the mirror of the NaN-placeholder bug fixed on 2026-08-03.
    That one failed CLOSED (suppressed real signals and looked like a quiet
    market). This one fails OPEN — it manufactures signals — which is worse,
    because acting on it spends money.

    Conservative by construction: on an early-close day (1pm ET holidays)
    the session is genuinely over but this still calls the bar provisional,
    so the report falls back to the prior close. Being a day late is a
    recoverable error; trading a half-formed bar is not.
    """
    now_et = (now or datetime.now(tz=ET)).astimezone(ET)
    if now_et.weekday() >= 5:            # weekend — no session forming
        return None
    cutoff = now_et.replace(hour=REGULAR_CLOSE_ET.hour,
                            minute=REGULAR_CLOSE_ET.minute,
                            second=0, microsecond=0) + timedelta(
                                minutes=SETTLE_MINUTES)
    return now_et.date() if now_et < cutoff else None


def drop_provisional(bars: pd.DataFrame,
                     now: Optional[datetime] = None) -> tuple[pd.DataFrame, bool]:
    """Remove the still-forming session's bar, if the newest row is one."""
    d = provisional_bar_date(now)
    if d is None or len(bars) == 0:
        return bars, False
    if bars.index[-1].date() == d:
        return bars.iloc[:-1], True
    return bars, False


def clean_bars(bars: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    """Drop rows with no usable close. NOT optional — this is a live hazard.

    yfinance emits a placeholder row for the CURRENT, still-open session:
    NaN open/high/low/close with a partial volume. Feeding that to the
    validated RSI does not raise and does not produce NaN — `rsi_series`
    computes gains via `np.where(diff > 0, diff, 0.0)`, and `NaN > 0` is
    False, so a missing price is silently scored as a FLAT DAY. The result
    is a confident, wrong RSI on a bar that has not happened yet.

    Observed on 2026-08-03: every one of the 13 instruments reported a NaN
    close and "below SMA200", i.e. no signals — a failure that looks exactly
    like a quiet market. That is the worst kind: it fails closed, so it
    would have suppressed real signals for as long as it took someone to
    notice.

    Fixed HERE and not in `strategies/rsi2.py` on purpose. That module is
    the validated system; editing it would mean live is no longer running
    what passed the gates. Data hygiene belongs in the data layer.
    """
    c = pd.to_numeric(bars["close"], errors="coerce")
    keep = c.notna()
    return bars.loc[keep], int((~keep).sum())


def evaluate_instrument(bars: pd.DataFrame, ticker: str) -> InstrumentState:
    """Evaluate one instrument at its last COMPLETE bar."""
    bars, dropped = clean_bars(bars)
    closes = pd.to_numeric(bars["close"], errors="coerce").to_numpy(dtype=float)
    ind = Indicators.build(closes)
    i = len(closes) - 1
    # Belt and braces: if a NaN survived cleaning, refuse rather than guess.
    ready = ind.ready(i) and bool(np.isfinite(closes[max(i - 250, 0):i + 1]).all())
    return InstrumentState(
        ticker=ticker,
        as_of=str(bars.index[i].date()),
        close=float(closes[i]),
        sma200=float(ind.sma200[i]) if np.isfinite(ind.sma200[i]) else float("nan"),
        above_sma=bool(uptrend(ind, i)),
        rsi2=float(ind.rsi2[i]) if np.isfinite(ind.rsi2[i]) else float("nan"),
        cum_rsi=float(ind.cum_rsi[i]) if np.isfinite(ind.cum_rsi[i]) else float("nan"),
        entry_signal=bool(ready and c2_entry(ind, i)),
        exit_signal=bool(ready and cum_exit(ind, i)),
        bars=len(closes),
        bars_dropped=dropped,
    )


def trading_days_since(bars: pd.DataFrame, from_date: str) -> Optional[int]:
    """Sessions elapsed from `from_date` to the last bar, inclusive of neither.

    Returns None when the date is not on the calendar — better to say "I
    cannot count this" than to silently return a wrong valve age.
    """
    idx = bars.index
    target = pd.Timestamp(from_date)
    pos = int(np.searchsorted(idx.values, np.datetime64(target)))
    if pos >= len(idx) or idx[pos] != target:
        return None
    return int(len(idx) - 1 - pos)


def shares_for(dollars: float, price: float) -> float:
    """Fractional shares to hit the slot amount. §2 allows a $5 tolerance."""
    if price <= 0:
        return 0.0
    return round(dollars / price, 4)


# ─────────────────────────────────────────────────────────────────────
# The daily report
# ─────────────────────────────────────────────────────────────────────

MAX_STALE_SESSIONS = 1     # yesterday's close is fine; older is not


def build_report(
    bars_by_ticker: dict[str, pd.DataFrame],
    positions: PositionsFile,
    *,
    instruments: tuple[str, ...] = LIVE_INSTRUMENTS,
    today: Optional[str] = None,
    now: Optional[datetime] = None,
) -> dict:
    """Everything the appendix asks for, as data. Rendering is separate."""
    states: dict[str, InstrumentState] = {}
    stale: list[str] = []
    dropped_rows: dict[str, int] = {}
    provisional_dropped: list[str] = []
    for t in instruments:
        b = bars_by_ticker.get(t)
        if b is not None:
            b, dropped = clean_bars(b)
            if dropped:
                dropped_rows[t] = dropped
            # Drop the still-forming session BEFORE any indicator is built.
            b, was_provisional = drop_provisional(b, now)
            if was_provisional:
                provisional_dropped.append(t)
        if b is None or len(b) < 201:
            stale.append(t)
            continue
        s = evaluate_instrument(b, t)
        # An instrument whose indicators are not finite has not been
        # evaluated, whatever the row count says. Listing it as "no signal"
        # would be a fabricated all-clear.
        if not np.isfinite(s.close) or not np.isfinite(s.sma200) \
                or not np.isfinite(s.cum_rsi):
            stale.append(t)
            continue
        states[t] = s

    as_of_dates = sorted({s.as_of for s in states.values()})
    as_of = as_of_dates[-1] if as_of_dates else None
    lagging = [t for t, s in states.items() if s.as_of != as_of]

    # ── Exits first: they free slots for tonight's entries ────────────
    exits = []
    for p in positions.open:
        s = states.get(p.ticker)
        b = bars_by_ticker.get(p.ticker)
        held_from_signal = (trading_days_since(b, p.signal_date)
                            if b is not None else None)
        held_from_entry = (trading_days_since(b, p.entry_date)
                           if b is not None else None)
        valve = (held_from_signal is not None
                 and held_from_signal >= TIME_VALVE_BARS)
        rsi_exit = bool(s and s.exit_signal)
        last = float(s.close) if s else float("nan")
        exits.append({
            "ticker": p.ticker,
            "signal_date": p.signal_date,
            "entry_date": p.entry_date,
            "entry_price": p.entry_price,
            "shares": p.shares,
            "cost_basis": round(p.cost_basis, 2),
            "last_close": last,
            "market_value": round(last * p.shares, 2) if np.isfinite(last) else None,
            "unrealized_pl": (round((last - p.entry_price) * p.shares, 2)
                              if np.isfinite(last) else None),
            "unrealized_pct": (round((last / p.entry_price - 1) * 100, 2)
                               if np.isfinite(last) and p.entry_price else None),
            "rsi2": s.rsi2 if s else None,
            "days_held_from_entry": held_from_entry,
            "days_from_signal": held_from_signal,
            "valve_at": TIME_VALVE_BARS,
            "exit_now": bool(rsi_exit or valve),
            "exit_reason": ("RSI2 > 65" if rsi_exit else
                            ("25-DAY VALVE" if valve else None)),
            "date_unknown": held_from_signal is None,
        })
    exiting = [e for e in exits if e["exit_now"]]

    # ── Entries. A slot freed by tonight's exit is NOT free tonight ──
    # §1: "A slot frees the session AFTER its exit fills." Tonight's exit
    # order fills at tomorrow's open, so the slot opens the session after
    # that. Counting it now would put four positions on the books.
    held = positions.held_tickers
    candidates = [s for t, s in states.items()
                  if s.entry_signal and t not in held]
    candidates.sort(key=lambda s: (s.cum_rsi, s.ticker))   # §1 tiebreak

    free = positions.slots_free
    entries, deferred = [], []
    for s in candidates:
        row = {
            "ticker": s.ticker,
            "cum_rsi": round(s.cum_rsi, 2),
            "rsi2": round(s.rsi2, 2),
            "close": round(s.close, 4),
            "sma200": round(s.sma200, 4),
            "slot_dollars": positions.slot_dollars,
            "est_shares": shares_for(positions.slot_dollars, s.close),
            "order": (f"BUY {s.ticker} — MARKET ON OPEN, next session, "
                      f"${positions.slot_dollars:,.0f} nominal"),
            "sizing_reference_close": round(s.close, 4),
        }
        if len(entries) < free:
            entries.append(row)
        else:
            row["reason_deferred"] = "no free slot (max 3 concurrent)"
            deferred.append(row)

    eligible = [t for t, s in states.items() if s.above_sma]

    # ── Kill switch / review state ───────────────────────────────────
    equity = positions.account_equity
    k1 = {
        "floor": K1_EQUITY_FLOOR,
        "equity": equity,
        "equity_as_of": positions.equity_as_of,
        "breached": equity < K1_EQUITY_FLOOR,
        "headroom": round(equity - K1_EQUITY_FLOOR, 2),
    }
    rec_slot = recommended_slot_dollars(equity)

    # STALENESS. The report is only actionable on the latest close. Acting
    # on a bar from three sessions ago places orders on signals that have
    # already resolved — and the failure is invisible, because a stale
    # report looks exactly like a fresh one.
    ref = pd.Timestamp(today) if today else pd.Timestamp.now(tz=None).normalize()
    sessions_behind = None
    if as_of:
        sessions_behind = max(
            int(np.busday_count(pd.Timestamp(as_of).date(), ref.date())) - 1, 0)
    is_stale = bool(sessions_behind is not None
                    and sessions_behind > MAX_STALE_SESSIONS)

    # "No signals", "I could not evaluate" and "this is old" must never
    # look the same.
    data_ok = not stale and not lagging and not is_stale

    return {
        "as_of": as_of,
        "generated": str(pd.Timestamp.now(tz="UTC")),
        "n_instruments": len(states),
        "n_expected": len(instruments),
        "data_ok": data_ok,
        "sessions_behind": sessions_behind,
        "is_stale": is_stale,
        "incomplete_bars_dropped": dropped_rows,
        "provisional_bars_dropped": sorted(provisional_dropped),
        "market_still_open": provisional_bar_date(now) is not None,
        "instruments_missing_data": stale,
        "instruments_lagging": lagging,
        "eligible_above_sma200": sorted(eligible),
        "n_eligible": len(eligible),
        "slots": {
            "total": SLOTS,
            "open": len(positions.open),
            "pending": len(positions.pending_entries),
            "pending_exits": len(positions.pending_exits),
            "free": free,
            "slot_dollars": positions.slot_dollars,
            "recommended_slot_dollars": rec_slot,
            "slot_dollars_stale": abs(rec_slot - positions.slot_dollars) >= 50.0,
        },
        "entries": entries,
        "entries_deferred": deferred,
        "positions": exits,
        "exits": exiting,
        "pending_orders": positions.pending,
        "regime": {t: {"close": round(s.close, 2),
                       "sma200": round(s.sma200, 2),
                       "above": s.above_sma,
                       "cum_rsi": round(s.cum_rsi, 2),
                       "rsi2": round(s.rsi2, 2)}
                   for t, s in sorted(states.items())},
        "k1": k1,
        "nothing_to_do": data_ok and not entries and not exiting,
        "config": {
            "cum_rsi_entry": C2_CUM_ENTRY,
            "rsi2_exit": CUM_RSI_EXIT,
            "valve_bars": TIME_VALVE_BARS,
            "slots": SLOTS,
            "instruments": list(instruments),
        },
    }


def render_report(r: dict) -> str:
    """The evening report, as text. Says plainly when there is nothing to do."""
    L: list[str] = []
    A = L.append
    A("=" * 72)
    A(f"C2-LIVE DAILY REPORT — close of {r['as_of']}")
    A("=" * 72)
    A("ANALYSIS ONLY. This report places no orders and has no broker link.")
    A("")

    if r["k1"]["breached"]:
        A("!" * 72)
        A(f"K1 KILL SWITCH BREACHED — equity ${r['k1']['equity']:,.2f} is below "
          f"${r['k1']['floor']:,.0f}.")
        A("Per protocol §6: SELL EVERYTHING at the next open, stop trading,")
        A("write the postmortem. No 'one more trade'.")
        A("!" * 72)
        A("")

    if not r["data_ok"]:
        A("!" * 72)
        A(f"DATA INCOMPLETE — {r['n_instruments']}/{r['n_expected']} instruments "
          f"evaluated. This report is NOT a clean all-clear.")
        if r["instruments_missing_data"]:
            A(f"  NOT EVALUATED: {', '.join(sorted(r['instruments_missing_data']))}")
        if r["instruments_lagging"]:
            A(f"  STALE BARS (not {r['as_of']}): "
              f"{', '.join(sorted(r['instruments_lagging']))}")
        if r["is_stale"]:
            A(f"  OLD DATA: the newest close is {r['as_of']}, "
              f"{r['sessions_behind']} sessions behind. These signals may")
            A("  already have resolved. Re-fetch (drop --offline) before acting.")
        A("  A missing instrument cannot signal, so 'no entries' below may")
        A("  simply mean 'not checked'. Re-run once data is available before")
        A("  concluding there was nothing to do.")
        A("!" * 72)
        A("")
    if r["incomplete_bars_dropped"]:
        A(f"note: dropped incomplete bars (NaN close, usually the still-open "
          f"session): {r['incomplete_bars_dropped']}")
        A("")
    if r["provisional_bars_dropped"]:
        A(f"note: the US market is still open, so today's half-formed bar was "
          f"DROPPED for {len(r['provisional_bars_dropped'])} instrument(s).")
        A(f"      This report is based on the last COMPLETED session "
          f"({r['as_of']}), which is correct — RSI(2) on an unfinished bar")
        A(f"      moves with every tick and invents signals. Re-run after "
          f"16:10 ET to include today.")
        A("")

    # 1 — entries
    A("-" * 72)
    A("1. NEW ENTRY SIGNALS")
    A("-" * 72)
    if not r["entries"] and not r["entries_deferred"]:
        A("   None. No instrument closed below CumRSI(2) 35 while above its SMA200.")
    else:
        for i, e in enumerate(r["entries"], 1):
            A(f"   [{i}] {e['ticker']}  CumRSI {e['cum_rsi']}  "
              f"(RSI2 {e['rsi2']}, close {e['close']} vs SMA200 {e['sma200']})")
            A(f"       ORDER: {e['order']}")
            A(f"       ~{e['est_shares']} shares at last close "
              f"${e['sizing_reference_close']} (sizing estimate only)")
        for e in r["entries_deferred"]:
            A(f"   [--] {e['ticker']}  CumRSI {e['cum_rsi']}  "
              f"DEFERRED: {e['reason_deferred']}")
            A("       Do NOT place this. It is not a queued order; if it still")
            A("       signals when a slot frees, it will appear again.")
    A("")

    # 2 — exits
    A("-" * 72)
    A("2. EXIT FLAGS")
    A("-" * 72)
    if not r["positions"]:
        A("   No open positions.")
    elif not r["exits"]:
        A("   None. Every open position stays on.")
    else:
        for e in r["exits"]:
            A(f"   {e['ticker']}  EXIT — {e['exit_reason']}  "
              f"(RSI2 {e['rsi2']}, day {e['days_from_signal']} from signal)")
            A(f"       ORDER: SELL {e['ticker']} {e['shares']} shares — "
              f"MARKET ON OPEN, next session")
            A(f"       entry ${e['entry_price']} -> last ${e['last_close']}  "
              f"({e['unrealized_pct']:+.2f}%, ${e['unrealized_pl']:+,.2f})")
    A("")

    # 3 — slot state
    A("-" * 72)
    A("3. SLOT STATE")
    A("-" * 72)
    s = r["slots"]
    A(f"   {s['open']} open + {s['pending']} pending = {s['open'] + s['pending']}"
      f"/{s['total']} slots used · {s['free']} free · "
      f"${s['slot_dollars']:,.0f} per slot")
    if s["slot_dollars_stale"]:
        A(f"   !! Monthly resize due: account/3 rounded down is "
          f"${s['recommended_slot_dollars']:,.0f}, you are using "
          f"${s['slot_dollars']:,.0f} (§1 — change at the monthly mark, not today)")
    sell_placed = {str(q.get("ticker", "")).upper()
                   for q in r["pending_orders"]
                   if str(q.get("side", "")).lower().startswith("exit")}
    for p in r["positions"]:
        if p["ticker"] in sell_placed:
            flag = "  <- SELL ORDER PLACED, awaiting fill"
        else:
            flag = "  <- EXIT" if p["exit_now"] else ""
        A(f"   {p['ticker']:5s} in {p['entry_date']} @ ${p['entry_price']:.4f} "
          f"x{p['shares']:.4f}  day {p['days_held_from_entry']}  "
          f"${p['market_value']:,.2f}  {p['unrealized_pct']:+.2f}%{flag}")
        if p["date_unknown"]:
            A(f"         !! signal_date {p['signal_date']} is not a trading day "
              f"on this calendar — valve age cannot be computed")
        elif p["days_from_signal"] is not None:
            left = p["valve_at"] - p["days_from_signal"]
            if left <= 5:
                A(f"         valve fires in {left} session(s)")
    for q in r["pending_orders"]:
        is_exit = str(q.get("side", "")).lower().startswith("exit")
        tail = ("— the position still holds its slot until this fills"
                if is_exit else "— holds a slot, not a position")
        A(f"   PENDING {q.get('ticker','?')} {q.get('side','?')} "
          f"(signal {q.get('signal_date','?')}) {tail}")
    A("")

    # 4 — slippage reference
    A("-" * 72)
    A("4. SLIPPAGE REFERENCE (K2)")
    A("-" * 72)
    if r["entries"] or r["exits"]:
        A("   Reference price for every order above = TOMORROW'S ACTUAL OPEN,")
        A("   not tonight's close. Tonight's close is a sizing estimate only.")
        A("   Run `--reconcile` after tomorrow's open to compute true slippage;")
        A("   comparing your fill to tonight's close would measure the overnight")
        A("   gap, which is not slippage and would trip K2 for nothing.")
    else:
        A("   No orders tonight — nothing to reference.")
    A("")

    # 5 — regime
    A("-" * 72)
    A("5. REGIME")
    A("-" * 72)
    A(f"   {r['n_eligible']}/{r['n_instruments']} instruments above SMA200 "
      f"(eligible to signal at all)")
    A(f"   {'  '.join(sorted(r['eligible_above_sma200'])) or '   none'}")
    below = [t for t in r["regime"] if not r["regime"][t]["above"]]
    if below:
        A(f"   below SMA200 (cannot signal): {'  '.join(sorted(below))}")
    A("")
    A(f"   {'Ticker':<7}{'Close':>10}{'SMA200':>10}{'CumRSI':>9}{'RSI2':>8}  vs SMA")
    for t, g in r["regime"].items():
        A(f"   {t:<7}{g['close']:>10.2f}{g['sma200']:>10.2f}"
          f"{g['cum_rsi']:>9.2f}{g['rsi2']:>8.2f}  "
          f"{'ABOVE' if g['above'] else 'below'}")
    A("")

    A("=" * 72)
    if not r["data_ok"]:
        A(f"CANNOT ADVISE — only {r['n_instruments']}/{r['n_expected']} "
          f"instruments were evaluated.")
        A("Do not read the empty sections above as 'no signals'. Fix the data")
        A("and re-run. If it is still broken by 9:25am ET, skip the session")
        A("and log the skip (§2) — never guess.")
    elif r["nothing_to_do"]:
        A("NOTHING TO DO TONIGHT. No orders to place. Update the log if you")
        A("had fills today, then close the laptop.")
    else:
        n = len(r["entries"]) + len(r["exits"])
        A(f"ACTION REQUIRED: place {n} market-on-open order(s) this evening.")
        A("Then record them under `pending` in your positions file.")
    A("=" * 72)
    return "\n".join(L)
