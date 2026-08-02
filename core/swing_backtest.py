"""core/swing_backtest.py — Event-driven replay of the swing system (Phase 4).

Answers the only question that matters about Phases 1-3: does this system
have positive expectancy after costs, or is it a well-organised way to
lose money slowly?

Design principle — REPLAY THE SHIPPING CODE. This module does not
reimplement the setup rules or the exit ladder. It calls
`core.swing_setups.evaluate_bars` and `core.swing_exits.evaluate_swing_exit`
directly, on historical slices. A backtest that reimplements its subject
validates the reimplementation, which is how systems pass validation and
then fail in production.

Why this can be PIT-clean where F.5 could not:
  F.5 failed largely because its signals needed point-in-time fundamentals
  (estimate revisions, forward valuation, guidance) that are not available
  free, so the spine zeroed or proxied most of them. The swing engine's
  gates read ONLY OHLCV bars. There is nothing to leak, nothing to proxy,
  and no restatement risk. Price history is the one dataset we genuinely
  have clean.

No-lookahead contract, enforced by construction and by tests:
  - A setup for bar i is computed from `bars[:i+1]` — that bar's close and
    everything before it. Nothing after.
  - The entry is a BUY STOP, so it can only fill on bar i+1 or later. It
    is never assumed filled at the signal bar's close.
  - The plan expires if bar i+1 does not trade through the entry. This
    matches the live watchdog, which recomputes and re-pushes daily.
  - The stop ratchet for bar j is computed at the END of bar j and takes
    effect on bar j+1. Ratcheting mid-bar would use that bar's high to
    justify a stop that the same bar then hits.

Pessimistic fill conventions (intrabar path is unknowable from OHLC):
  - Entry fills at max(plan_entry, bar_open) — a gap through the stop
    fills at the open, worse than the trigger.
  - Stop fills at min(stop, bar_open) — same logic downward.
  - When one bar spans BOTH stop and target, the STOP is assumed to have
    hit first. This is the standard conservative assumption; the opposite
    assumption inflates results substantially and cannot be justified
    from daily bars.
  - The entry bar itself can stop the trade out.

Costs (P1-7 item 2) — see CostModel. Commission and slippage are separate
charges, and slippage applies to STOP orders only, never to limit fills.
A system that is breakeven before costs is a loser after them.

KNOWN DIVERGENCES from live behaviour — all inflate results, none deflate:
  1. SURVIVORSHIP (P1-7 item 1, UNFIXED). The universe (`prices.parquet`)
     is today's CORE list applied backward. ZERO of its tickers delist.
     Every company that went bankrupt, was acquired, or deteriorated out
     of the quality screen is absent — disproportionately the losers.
     `universe_provenance()` detects this and `evaluate_gate()` refuses
     to issue an unqualified GO on such a universe.
  2. The live engine REJECTS setups with earnings inside the holding
     window. There is no PIT earnings calendar in the cache, so the
     backtest cannot model that filter and includes trades live would skip.
  3. The cache carries no spread data, so slippage is a parameter rather
     than a measurement.
Read every number here as an OPTIMISTIC bound.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, asdict, field
from typing import Optional

import numpy as np
import pandas as pd

log = logging.getLogger("swing_backtest")

_WARMUP_BARS = 210   # evaluate_bars needs >= 210 for MA200


def universe_provenance(prices: pd.DataFrame) -> dict:
    """P1-7 item 1 + acceptance — classify the universe as PIT or SURVIVOR.

    Detection is empirical, not a label we trust someone to set: count
    tickers whose price series ends materially before the dataset does.
    A genuine point-in-time universe contains dead names, so some series
    MUST stop early. Zero early stops across hundreds of tickers over
    five years is only possible if the list was filtered by what is
    healthy today.

    Returns the dict logged in the run config, so any reader can see
    which kind of universe produced the numbers.
    """
    df = prices.copy()
    df["date"] = pd.to_datetime(df["date"])
    last = df.groupby("ticker")["date"].max()
    first = df.groupby("ticker")["date"].min()
    dataset_end = last.max()

    dead = last[last < dataset_end - pd.Timedelta(days=30)]
    dead_rate = len(dead) / max(1, len(last))
    is_pit = len(dead) > 0

    return {
        "universe_type":     "PIT" if is_pit else "SURVIVOR",
        "n_tickers":         int(len(last)),
        "n_delisted":        int(len(dead)),
        "delisting_rate":    round(dead_rate, 4),
        "dataset_start":     str(first.min().date()),
        "dataset_end":       str(dataset_end.date()),
        "detection":         "empirical: tickers whose series ends >30d before the dataset",
        "warning": (
            None if is_pit else
            "SURVIVOR universe: zero delistings detected. Every result is an "
            "UPPER BOUND, not a measured edge (P1-7 item 1)."
        ),
    }


@dataclass
class Trade:
    ticker:        str
    setup_type:    str
    entry_date:    str
    exit_date:     str
    entry_index:   int
    exit_index:    int
    bars_held:     int

    plan_entry:    float
    plan_stop:     float
    plan_target:   float
    fill_price:    float     # after costs
    exit_price:    float     # after costs, weighted if a partial was taken
    risk_per_share: float    # fill_price - plan_stop = actual 1R taken

    r_multiple:    float     # the headline number
    mfe_r:         float     # best unrealised, in R
    mae_r:         float     # worst unrealised, in R
    exit_reason:   str
    partial_taken: bool
    year:          int

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class CostModel:
    """P1-7 item 2 — commission AND slippage, modelled separately.

    Slippage applies to STOP orders only. That distinction is real and
    matters: a stop-market exit is filled by whoever is there when it
    triggers, so it slips; a take-profit LIMIT (order B in the plan's
    bracket) fills at your price or not at all, so it does not.
    Charging slippage on limit exits would overstate costs; charging
    none on stop exits — the v1 behaviour — understates them badly,
    because stops fire precisely when the book is thin.

    `slippage_bps` is per side. P1-7 asks for 0.05-0.15% (5-15bps);
    default 10 sits mid-range. `tick_size` is the floor per the ticket's
    "or 1 tick, whichever larger".
    """
    commission_per_side: float = 0.0     # $ flat (T212 is commission-free)
    slippage_bps:        float = 10.0    # adverse, STOP orders only
    tick_size:           float = 0.01

    def slip(self, price: float) -> float:
        """Adverse move, always positive. max(bps, 1 tick)."""
        return max(price * self.slippage_bps / 10_000.0, self.tick_size)

    def fill(self, price: float, *, buying: bool, order_type: str) -> float:
        """Effective fill price. `order_type` in {"stop", "limit", "market"}.

        stop/market slip against you; limit does not.
        """
        if order_type in ("stop", "market"):
            s = self.slip(price)
            return price + s if buying else price - s
        return price

    def commission_r(self, shares_r: float) -> float:
        """Commission is a $ amount, but the backtest works in R. Callers
        convert via risk_per_share; kept here so the model owns it."""
        return self.commission_per_side * shares_r

    def describe(self) -> dict:
        return {
            "commission_per_side_usd": self.commission_per_side,
            "slippage_bps_per_side":   self.slippage_bps,
            "slippage_applies_to":     "stop and market orders only (not limit fills)",
            "tick_size":               self.tick_size,
        }


def simulate_trade(
    bars: pd.DataFrame,
    entry_index: int,
    setup,
    *,
    ticker: str,
    costs: Optional[CostModel] = None,
    costs_bps: Optional[float] = None,
    time_stop_bars: int = 15,
    max_bars: int = 60,
) -> Optional[Trade]:
    """Walk a single trade forward from its entry bar to its exit.

    `entry_index` is the bar on which the buy stop is tested. Returns None
    if the plan expired unfilled — that is not a trade and must not enter
    the statistics as a scratch.

    `max_bars` is a safety bound; with a 15-bar time stop it should never
    bind, and a test asserts that.
    """
    from core.swing_exits import SwingPlan, SwingState, evaluate_swing_exit

    if costs is None:
        # costs_bps kept for the older call signature used by unit tests:
        # it maps onto slippage with no commission.
        costs = CostModel(slippage_bps=5.0 if costs_bps is None else costs_bps,
                          tick_size=0.0 if costs_bps == 0 else 0.01)

    n = len(bars)
    if entry_index >= n:
        return None

    eb = bars.iloc[entry_index]
    if eb["high"] < setup.entry:
        return None  # never traded through the buy stop → plan expired

    # Order A is a STOP-LIMIT (plan §6): the limit caps what we will pay.
    # Modelling it as a stop-market overstates the trade count and drags
    # in exactly the chased, gapped-up entries the limit exists to refuse.
    #   open <= trigger          -> fills at the trigger
    #   trigger < open <= limit  -> fills at the open (worse, still allowed)
    #   open > limit             -> the stop-limit becomes a resting limit
    #                               at `limit`; it fills only if price trades
    #                               back down to it, otherwise NO TRADE.
    limit = float(getattr(setup, "entry_limit", None) or setup.entry)
    op = float(eb["open"])
    if op <= setup.entry:
        raw_fill = float(setup.entry)
    elif op <= limit:
        raw_fill = op
    elif float(eb["low"]) <= limit:
        raw_fill = limit          # gapped past, then came back to the limit
    else:
        return None               # gapped through the limit — no fill, no trade

    # Entry is a STOP order → slips. But the limit still binds: if slippage
    # would carry the fill past the limit, the order does not fill. That is
    # what the limit is for.
    fill = costs.fill(raw_fill, buying=True, order_type="stop")
    if fill > limit:
        return None

    plan = SwingPlan(
        entry=fill,
        stop=float(setup.stop),
        target=float(setup.target),
        # Backtest counts BARS; the live system counts calendar days and
        # reconciles via swing_exits.trading_days_to_calendar. Passing bars
        # here keeps the horizon honest at 15 sessions either way.
        time_stop_days=time_stop_bars,
    )
    if plan.r <= 0:
        # A gap-up fill can push the entry above... no: it raises risk, never
        # inverts it. This only triggers on malformed input.
        return None

    state = SwingState(current_stop=plan.stop, high_water=fill, partial_taken=False)

    realised_r = 0.0
    remaining = 1.0
    mfe_r = 0.0
    mae_r = 0.0
    # Default reason covers the trade that never resolves — almost always
    # because the price series ended while it was still open. Such trades
    # are marked to the last close, so their R is an estimate, not a
    # realised result. Reported separately so they can be excluded.
    exit_reason = "END_OF_DATA"
    exit_index = entry_index
    exit_price_acc = 0.0

    for j in range(entry_index, min(n, entry_index + max_bars)):
        b = bars.iloc[j]
        bars_held = j - entry_index
        hi, lo, op, cl = (float(b["high"]), float(b["low"]),
                          float(b["open"]), float(b["close"]))

        # On the ENTRY bar we cannot prove we held through that bar's high:
        # the fill happens somewhere inside the bar and the high may have
        # printed before it. Using it would ratchet the stop to breakeven
        # on the strength of a move we might never have been in. Fall back
        # to the close, which we demonstrably held to.
        eff_high = hi if j > entry_index else max(fill, cl)

        # Excursions, measured against the stop in force at the START of
        # this bar (the ratchet from the previous bar's close).
        mfe_r = max(mfe_r, (eff_high - fill) / plan.r)
        mae_r = min(mae_r, (lo - fill) / plan.r)

        stop_now = state.current_stop

        # ── Stop first: the pessimistic intrabar assumption ──────────
        if lo <= stop_now:
            raw = min(stop_now, op)          # gap-down fills at the open
            # Order C is a stop-MARKET → slips, and slips worst exactly when
            # it fires. This is the charge v1 was missing entirely.
            px = costs.fill(raw, buying=False, order_type="stop")
            realised_r += remaining * (px - fill) / plan.r
            exit_price_acc += remaining * px
            exit_index = j
            exit_reason = (
                "STOP" if stop_now <= float(setup.stop) + 1e-9
                else ("BREAKEVEN_STOP" if abs(stop_now - fill) < 1e-6 else "TRAIL_STOP")
            )
            remaining = 0.0
            break

        # ── Target: partial, once ────────────────────────────────────
        if not state.partial_taken and hi >= plan.target:
            from core.swing_exits import PARTIAL_FRACTION
            raw = max(plan.target, op)       # gap-up fills better
            # Order B is a LIMIT → fills at your price or not at all. No
            # adverse slippage; charging it here would overstate costs.
            px = costs.fill(raw, buying=False, order_type="limit")
            sold = remaining * PARTIAL_FRACTION
            realised_r += sold * (px - fill) / plan.r
            exit_price_acc += sold * px
            remaining -= sold
            state.partial_taken = True
            # Locking R means the remainder must not become a loser.
            state.current_stop = max(state.current_stop, fill)

        # ── Time stop: exit the remainder at the close ───────────────
        if bars_held >= time_stop_bars:
            # Time stop is a market order at the close → slips.
            px = costs.fill(cl, buying=False, order_type="market")
            realised_r += remaining * (px - fill) / plan.r
            exit_price_acc += remaining * px
            exit_index = j
            exit_reason = "TIME_STOP" if not state.partial_taken else "TIME_STOP_AFTER_PARTIAL"
            remaining = 0.0
            break

        # ── End-of-bar ratchet, effective NEXT bar ───────────────────
        # ATR from bars up to and including j — the same call the live
        # monitor makes, so a divergence here would be a real bug.
        try:
            from core.swing_setups import _wilder_atr
            atr = _wilder_atr(bars.iloc[: j + 1])
        except Exception:
            atr = None

        state.high_water = max(state.high_water, eff_high)
        decision = evaluate_swing_exit(
            plan, state, current_price=cl, atr=atr, days_held=bars_held,
        )
        # Only the ratchet is consumed here. The exit checks above own the
        # exit decision because they can see intrabar highs and lows, which
        # evaluate_swing_exit (a close-only API) cannot.
        state.current_stop = max(state.current_stop, decision.new_stop)
        state.high_water = decision.high_water
        exit_index = j

    if remaining > 0:
        # Unresolved — series ended (or max_bars bound). Mark to last close.
        b = bars.iloc[exit_index]
        px = costs.fill(float(b["close"]), buying=False, order_type="market")
        realised_r += remaining * (px - fill) / plan.r
        exit_price_acc += remaining * px

    entry_date = str(bars.index[entry_index].date())
    exit_date = str(bars.index[exit_index].date())

    return Trade(
        ticker=ticker,
        setup_type=setup.setup_type,
        entry_date=entry_date,
        exit_date=exit_date,
        entry_index=entry_index,
        exit_index=exit_index,
        bars_held=exit_index - entry_index,
        plan_entry=round(float(setup.entry), 4),
        plan_stop=round(float(setup.stop), 4),
        plan_target=round(float(setup.target), 4),
        fill_price=round(fill, 4),
        exit_price=round(exit_price_acc, 4),
        risk_per_share=round(plan.r, 4),
        r_multiple=round(realised_r, 4),
        mfe_r=round(mfe_r, 3),
        mae_r=round(mae_r, 3),
        exit_reason=exit_reason,
        partial_taken=state.partial_taken,
        year=int(entry_date[:4]),
    )


def replay_ticker(
    bars: pd.DataFrame,
    ticker: str,
    *,
    spy_bars: Optional[pd.DataFrame] = None,
    costs: Optional[CostModel] = None,
    costs_bps: Optional[float] = None,
    time_stop_bars: int = 15,
    warmup: int = _WARMUP_BARS,
    strategy: str = "trend_pullback",
    strategy_kwargs: Optional[dict] = None,
) -> list[Trade]:
    """Replay one ticker end to end.

    One position per ticker at a time — after an exit, scanning resumes on
    the following bar. Pyramiding into the same name is not something the
    live system does, so the backtest must not either.

    `spy_bars` drives the market-regime and relative-strength gates. It is
    REINDEXED onto this ticker's dates, so slicing by position gives SPY
    as of the same bar — never a later one. Passing None makes both gates
    fail closed (no trades), matching live behaviour on a SPY fetch error.

    `strategy` selects the ENTRY only. Everything downstream — sizing,
    the exit ladder, the fill simulator — is identical whichever entry
    produced the plan, which is what makes a strategy comparison
    attributable to the entry signal alone. Defaults to Phase 1 so
    existing callers are unaffected.
    """
    from strategies import get_strategy
    evaluate_bars = get_strategy(strategy)
    skw = strategy_kwargs or {}

    lower = {c.lower(): c for c in bars.columns}
    bars_lower = bars.rename(columns={v: k for k, v in lower.items()})

    spy_aligned = None
    if spy_bars is not None:
        sl = {c.lower(): c for c in spy_bars.columns}
        spy_l = spy_bars.rename(columns={v: k for k, v in sl.items()})
        # Forward-fill onto this ticker's calendar: a date the ticker
        # traded but SPY did not carries SPY's most recent prior close.
        spy_aligned = spy_l.reindex(bars_lower.index).ffill()

    n = len(bars)
    trades: list[Trade] = []
    i = warmup
    while i < n - 1:
        setup = evaluate_bars(
            ticker, bars_lower.iloc[: i + 1],
            spy_hist=None if spy_aligned is None else spy_aligned.iloc[: i + 1],
            **skw,
        )
        if not setup.qualifies:
            i += 1
            continue
        trade = simulate_trade(
            bars_lower, i + 1, setup, ticker=ticker,
            costs=costs, costs_bps=costs_bps, time_stop_bars=time_stop_bars,
        )
        if trade is None:
            i += 1          # plan expired unfilled
            continue
        trades.append(trade)
        i = trade.exit_index + 1
    return trades


def run_backtest(
    prices: pd.DataFrame,
    *,
    tickers: Optional[list[str]] = None,
    costs: Optional[CostModel] = None,
    time_stop_bars: int = 15,
    progress_every: int = 25,
    strategy: str = "trend_pullback",
    strategy_kwargs: Optional[dict] = None,
) -> list[Trade]:
    """Replay every ticker in a long-format OHLCV frame.

    `prices` columns: date, ticker, open, high, low, close, volume.
    """
    df = prices.copy()
    df["date"] = pd.to_datetime(df["date"])
    universe = tickers if tickers is not None else sorted(df["ticker"].unique())

    # SPY is the benchmark for gates 1 and 5, never a tradeable name here.
    spy_rows = df[df["ticker"] == "SPY"].sort_values("date")
    spy_bars = (
        spy_rows.set_index("date")[["open", "high", "low", "close", "volume"]]
        if len(spy_rows) else None
    )
    if spy_bars is None:
        log.error(
            "[backtest] SPY missing from price cache — the regime and "
            "relative-strength gates will fail closed and produce zero trades."
        )

    all_trades: list[Trade] = []
    for k, t in enumerate(universe, 1):
        if t == "SPY":
            continue
        sub = df[df["ticker"] == t].sort_values("date")
        if len(sub) < _WARMUP_BARS + 10:
            continue
        bars = sub.set_index("date")[["open", "high", "low", "close", "volume"]]
        try:
            all_trades.extend(replay_ticker(
                bars, t, spy_bars=spy_bars,
                costs=costs, time_stop_bars=time_stop_bars,
                strategy=strategy, strategy_kwargs=strategy_kwargs,
            ))
        except Exception as e:
            log.warning(f"[backtest] {t}: {e}")
            continue
        if progress_every and k % progress_every == 0:
            log.info(f"[backtest] {k}/{len(universe)} tickers, {len(all_trades)} trades")
    return all_trades


# ─────────────────────────────────────────────────────────────────────
# Statistics
# ─────────────────────────────────────────────────────────────────────

def compute_stats(trades: list[Trade]) -> dict:
    """Per-trade statistics. Expectancy is the headline.

    Expectancy in R is the number that decides whether the system is worth
    running: it is the average outcome of taking one unit of risk, and it
    is invariant to position size, account size and stop width. A system
    with expectancy <= 0 cannot be rescued by sizing.
    """
    if not trades:
        return {"n_trades": 0}

    r = np.array([t.r_multiple for t in trades], dtype=float)
    wins = r[r > 0]
    losses = r[r <= 0]

    gross_win = float(wins.sum()) if len(wins) else 0.0
    gross_loss = float(-losses.sum()) if len(losses) else 0.0

    # Equity curve in R, trades ordered by exit date (the order in which
    # P&L is actually realised).
    ordered = sorted(trades, key=lambda t: t.exit_date)
    curve = np.cumsum([t.r_multiple for t in ordered])
    peak = np.maximum.accumulate(curve)
    drawdown = curve - peak

    # Breakeven win rate = avgLoss / (avgWin + avgLoss). The win rate at
    # which this payoff ratio nets to zero — the number the actual win
    # rate has to beat for the system to make money at all.
    _aw = float(wins.mean()) if len(wins) else 0.0
    _al = float(-losses.mean()) if len(losses) else 0.0
    breakeven_wr = (_al / (_aw + _al)) if (_aw + _al) > 0 else None

    return {
        "n_trades":        len(trades),
        "expectancy_r":    round(float(r.mean()), 4),
        "breakeven_win_rate": round(breakeven_wr, 4) if breakeven_wr is not None else None,
        "median_r":        round(float(np.median(r)), 4),
        "total_r":         round(float(r.sum()), 2),
        "win_rate":        round(float(len(wins) / len(r)), 4),
        "avg_win_r":       round(float(wins.mean()), 4) if len(wins) else 0.0,
        "avg_loss_r":      round(float(losses.mean()), 4) if len(losses) else 0.0,
        "profit_factor":   round(gross_win / gross_loss, 3) if gross_loss > 0 else float("inf"),
        "max_drawdown_r":  round(float(-drawdown.min()), 2),
        "std_r":           round(float(r.std(ddof=1)), 4) if len(r) > 1 else 0.0,
        # t-stat on expectancy: with a few hundred trades, "positive mean"
        # and "distinguishable from zero" are very different claims.
        "t_stat":          round(float(r.mean() / (r.std(ddof=1) / np.sqrt(len(r)))), 3)
                           if len(r) > 1 and r.std(ddof=1) > 0 else 0.0,
        "avg_bars_held":   round(float(np.mean([t.bars_held for t in trades])), 2),
        "pct_partial":     round(float(np.mean([t.partial_taken for t in trades])), 4),
        "exit_breakdown":  {
            k: int(sum(1 for t in trades if t.exit_reason == k))
            for k in sorted({t.exit_reason for t in trades})
        },
        "setup_breakdown": {
            k: int(sum(1 for t in trades if t.setup_type == k))
            for k in sorted({t.setup_type for t in trades})
        },
    }


def stats_by_year(trades: list[Trade]) -> dict:
    """Per-year stats. The worst year is the stability check — F.5 died on
    a single fold (-22.48pp in 2025), and an aggregate that hides one
    catastrophic regime is not a validation."""
    out = {}
    for y in sorted({t.year for t in trades}):
        out[str(y)] = compute_stats([t for t in trades if t.year == y])
    return out


def simulate_portfolio(
    trades: list[Trade],
    *,
    risk_pct: float = 0.0075,
    max_concurrent: int = 6,
    starting_equity: float = 10_000.0,
) -> dict:
    """Equity-curve simulation respecting the live concurrency cap.

    Per-trade expectancy is concurrency-independent, but DRAWDOWN is not:
    six positions stopping out together is a very different experience
    from six sequential losses. This admits trades in entry-date order up
    to `max_concurrent`, risking a fixed fraction of current equity each,
    and reports the resulting equity drawdown.

    Trades rejected by the cap are counted, not silently dropped — if the
    cap is rejecting most signals, per-trade expectancy is describing a
    system you cannot actually run.
    """
    if not trades:
        return {"n_taken": 0, "n_rejected": 0}

    events = []
    for t in trades:
        events.append((t.entry_date, 0, t))   # 0 sorts opens before closes
        events.append((t.exit_date, 1, t))
    events.sort(key=lambda e: (e[0], e[1]))

    equity = starting_equity
    open_trades: dict[int, float] = {}   # id(trade) -> risk amount
    n_taken = n_rejected = 0
    curve = [equity]

    for _date, kind, t in events:
        if kind == 0:
            if len(open_trades) >= max_concurrent:
                n_rejected += 1
                continue
            open_trades[id(t)] = equity * risk_pct
            n_taken += 1
        else:
            risk_amt = open_trades.pop(id(t), None)
            if risk_amt is None:
                continue          # was rejected at open
            equity += risk_amt * t.r_multiple
            curve.append(equity)

    arr = np.array(curve)
    peak = np.maximum.accumulate(arr)
    dd = (arr - peak) / peak
    return {
        "n_taken":            n_taken,
        "n_rejected":         n_rejected,
        "rejection_rate":     round(n_rejected / (n_taken + n_rejected), 4) if (n_taken + n_rejected) else 0.0,
        "starting_equity":    starting_equity,
        "final_equity":       round(float(equity), 2),
        "total_return_pct":   round(float(equity / starting_equity - 1) * 100, 2),
        "max_drawdown_pct":   round(float(-dd.min()) * 100, 2),
        "risk_pct":           risk_pct,
        "max_concurrent":     max_concurrent,
    }


# ─────────────────────────────────────────────────────────────────────
# The pre-registered gate
# ─────────────────────────────────────────────────────────────────────
#
# Registered BEFORE the first run, per the same discipline that made F.5's
# REVERT verdict binding rather than negotiable. Thresholds are not tuned
# to whatever the run produces.
#
# Why these and not F.5's "top-decile 6m excess vs SPY": that gate measures
# a ranker's ability to pick winners over six months. This system does not
# rank and does not hold for six months. The correct question for a
# mechanical trade system is whether one unit of risk pays.
GATE = {
    "min_expectancy_r":   0.25,   # per unit risked, after costs
    "min_win_rate":       0.40,   # below this, drawdowns get psychologically unrunnable
    "min_trades":         200,    # sampling floor for a stable mean
    "max_drawdown_pct":   15.0,   # portfolio sim at the live risk/concurrency settings
    "min_worst_year_expectancy_r": -0.10,   # no single regime may be catastrophic
    "min_t_stat":         2.0,    # expectancy must be distinguishable from zero
}


def confirm_no_lookahead(
    prices: pd.DataFrame,
    *,
    n_tickers: int = 3,
    strategy: str = "trend_pullback",
    strategy_kwargs: Optional[dict] = None,
) -> dict:
    """P1-7 item 3 + acceptance — VERIFY no look-ahead, don't just claim it.

    The check: replay a ticker, then replay it again with 40 bars of a
    violent fabricated rally appended. Every trade that closed before the
    original data ended must be byte-identical. If any filter input (MA,
    RS, pause, regime) or any fill peeked forward, the appended future
    would change those results.

    Runs at the start of every validation so the confirmation is a fact
    about THIS run, not a claim inherited from the test suite.
    """
    df = prices.copy()
    df["date"] = pd.to_datetime(df["date"])
    spy_rows = df[df["ticker"] == "SPY"].sort_values("date")
    spy = (spy_rows.set_index("date")[["open", "high", "low", "close", "volume"]]
           if len(spy_rows) else None)

    candidates = [t for t in sorted(df["ticker"].unique()) if t != "SPY"][:n_tickers]
    checked, mismatches, n_compared = [], [], 0

    for t in candidates:
        sub = df[df["ticker"] == t].sort_values("date")
        bars = sub.set_index("date")[["open", "high", "low", "close", "volume"]]
        if len(bars) < _WARMUP_BARS + 40:
            continue

        base = replay_ticker(bars, t, spy_bars=spy, costs_bps=0,
                             strategy=strategy, strategy_kwargs=strategy_kwargs)

        tail = pd.concat([bars.iloc[-1:]] * 40)
        tail.index = pd.date_range(bars.index[-1] + pd.Timedelta(days=1),
                                   periods=40, freq="B")
        tail = tail.copy()
        tail[["open", "high", "low", "close"]] *= 3.0     # violent fabricated rally
        ext = replay_ticker(pd.concat([bars, tail]), t, spy_bars=spy, costs_bps=0,
                            strategy=strategy, strategy_kwargs=strategy_kwargs)

        cutoff = len(bars) - 1
        for a, b in zip(base, ext):
            if a.exit_index < cutoff:
                n_compared += 1
                if (a.r_multiple != b.r_multiple or a.exit_reason != b.exit_reason
                        or a.fill_price != b.fill_price):
                    mismatches.append(
                        f"{t} {a.entry_date}: {a.exit_reason}/{a.r_multiple} "
                        f"-> {b.exit_reason}/{b.r_multiple}"
                    )
        checked.append(t)

    passed = not mismatches
    return {
        "passed":          passed,
        "tickers_checked": checked,
        "trades_compared": n_compared,
        "mismatches":      mismatches[:10],
        "method": (
            "appended 40 bars of a fabricated 3x rally; every trade closing "
            "before the original data ended must be unchanged"
        ),
        "note": None if n_compared else "INCONCLUSIVE — no closed trades to compare",
    }


def evaluate_gate(
    stats: dict,
    by_year: dict,
    portfolio: dict,
    gate: Optional[dict] = None,
    *,
    provenance: Optional[dict] = None,
    lookahead: Optional[dict] = None,
) -> dict:
    """Apply the pre-registered gate. Returns GO or REVERT with reasons.

    REVERT means: keep the swing engine as a candidate generator requiring
    the user's judgement, and do NOT enable any automated sizing or ML
    ranking on top of it. It does not mean delete the code — it means the
    system has not earned an edge claim.
    """
    g = dict(GATE)
    if gate:
        g.update(gate)

    n = stats.get("n_trades", 0)
    exp_r = stats.get("expectancy_r", 0.0)
    win = stats.get("win_rate", 0.0)
    t_stat = stats.get("t_stat", 0.0)
    dd = portfolio.get("max_drawdown_pct", 100.0)

    year_exps = {y: s.get("expectancy_r", 0.0) for y, s in by_year.items()
                 if s.get("n_trades", 0) >= 10}
    worst_year, worst_exp = (
        min(year_exps.items(), key=lambda kv: kv[1]) if year_exps else ("n/a", -99.0)
    )

    checks = [
        ("n_trades",       n >= g["min_trades"],
         f"{n} trades (need >= {g['min_trades']})"),
        ("expectancy_r",   exp_r >= g["min_expectancy_r"],
         f"expectancy {exp_r:+.3f}R (need >= {g['min_expectancy_r']:+.2f}R)"),
        ("win_rate",       win >= g["min_win_rate"],
         f"win rate {win:.1%} (need >= {g['min_win_rate']:.0%})"),
        ("t_stat",         t_stat >= g["min_t_stat"],
         f"t-stat {t_stat:.2f} (need >= {g['min_t_stat']:.1f})"),
        ("max_drawdown",   dd <= g["max_drawdown_pct"],
         f"max DD {dd:.1f}% (need <= {g['max_drawdown_pct']:.0f}%)"),
        ("worst_year",     worst_exp >= g["min_worst_year_expectancy_r"],
         f"worst year {worst_year} expectancy {worst_exp:+.3f}R "
         f"(need >= {g['min_worst_year_expectancy_r']:+.2f}R)"),
    ]

    passed = all(ok for _, ok, _ in checks)

    # P1-7 items 1 and 3 — these are not scoring criteria, they are
    # PRECONDITIONS on whether the numbers may be believed at all.
    # A GO earned on a survivor universe is an upper bound, and the
    # verdict must say so rather than let a reader infer an edge.
    caveats: list[str] = []
    decision = "GO" if passed else "REVERT"

    if lookahead is not None and lookahead.get("passed") is False:
        decision = "INVALID"
        caveats.append(
            "LOOK-AHEAD DETECTED — results are meaningless until fixed: "
            + "; ".join(lookahead.get("mismatches", [])[:3])
        )

    if provenance is not None and provenance.get("universe_type") != "PIT":
        caveats.append(
            f"SURVIVOR universe ({provenance.get('n_delisted', 0)} delistings in "
            f"{provenance.get('n_tickers', '?')} tickers) — P1-7 item 1 unfixed. "
            f"Any positive result is an UPPER BOUND, not a measured edge."
        )
        if decision == "GO":
            decision = "GO_UPPER_BOUND_ONLY"

    return {
        "decision": decision,
        "gate": g,
        "checks": [
            {"name": name, "pass": ok, "detail": detail}
            for name, ok, detail in checks
        ],
        "reasons": [f"{'PASS' if ok else 'FAIL'}: {detail}" for _, ok, detail in checks],
        "caveats": caveats,
        "worst_year": worst_year,
        "worst_year_expectancy_r": round(worst_exp, 4),
    }
