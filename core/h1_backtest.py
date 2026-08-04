"""core/h1_backtest.py — Event-driven replay for H1 (panic mean reversion).

Why this is a separate module and not a flag on `swing_backtest`:

`swing_backtest.simulate_trade` models a stop-LIMIT entry above the market
and the Phase 3 exit ladder (2R target, partial, break-even ratchet,
trailing stop). H1 inverts the entry — a resting LIMIT below the market —
and pre-registers a different exit ladder with no target and no ratchet.
Bending one simulator to do both would mean a pile of `if strategy ==`
branches through the hottest code in the project, and every one of those
branches is a place for the two paths to quietly diverge. Separate
simulators, shared statistics.

What IS shared, deliberately: `CostModel`, `compute_stats`,
`stats_by_year`, `simulate_portfolio` and `universe_provenance` are
imported from `swing_backtest`, so H1's numbers are computed by the same
code that computed Phase 1's and Phase 1B's and are directly comparable.

No-lookahead contract (identical in spirit to swing_backtest):
  - The signal for bar i is computed from `bars[:i+1]` and nothing after.
  - The order rests on bar i+1 ONLY. It is never assumed filled at the
    signal bar's close, and it does not carry over to bar i+2 — the
    registered rule is "order expires end of day".
  - The SMA5 exit for bar j uses closes up to and including j, and exits
    AT bar j's close. That is legal: a close-based rule is knowable at
    the close it acts on.
  - `confirm_no_lookahead_h1` verifies all of this empirically per run
    rather than asserting it.

Pessimistic fill conventions (intrabar path is unknowable from OHLC):
  - Entry: fills only if the bar's LOW reaches the limit. Fill price is
    min(open, limit) — a gap-down open fills at the open, better than the
    limit, which is what actually happens to a resting buy limit.
  - Stop: fills at min(stop, open) — a gap-down fills at the open, worse.
  - When one bar spans both the stop and the SMA5 exit trigger, the STOP
    is assumed to have hit first.
  - The entry bar itself can stop the trade out.

Costs: a LIMIT entry does not slip — it fills at your price or better, or
not at all. Exits do: the hard stop is a stop-market, and the SMA5 and
time-stop exits are market-on-close. Charging entry slippage here would
overstate costs; charging none on the exits would understate them.

KNOWN DIVERGENCES, all in the optimistic direction:
  1. SURVIVORSHIP. The universe is today's quality list applied backward.
     `universe_provenance()` detects and labels this.
  2. No point-in-time earnings calendar, so the registered "no earnings
     within 10 days" gate cannot be applied in the backtest. It is
     disabled explicitly (not silently) and the run config says so.
  3. No spread data, so slippage is a parameter rather than a measurement.
  4. A resting limit order that fills in a panic is subject to adverse
     selection this simulator cannot see: on daily bars we cannot know
     whether the print at our limit was available in size. Read fill
     counts as an upper bound.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, asdict
from typing import Optional

import numpy as np
import pandas as pd

from core.protocol_gate import (  # noqa: F401  (re-exported for callers)
    DEFAULT_GATE, evaluate_default_gate, split_trades,
)
from core.swing_backtest import (  # noqa: F401  (re-exported for callers)
    CostModel, compute_stats, simulate_portfolio, stats_by_year,
    universe_provenance,
)

log = logging.getLogger("h1_backtest")

_WARMUP_BARS = 210          # SMA200 + ATR14 headroom


@dataclass
class H1Trade:
    ticker:        str
    setup_type:    str
    signal_date:   str
    entry_date:    str
    exit_date:     str
    entry_index:   int
    exit_index:    int
    bars_held:     int

    signal_close:  float
    plan_limit:    float
    fill_price:    float     # after costs (a limit fill has none)
    stop_price:    float     # anchored to the fill
    exit_price:    float     # after costs
    atr14:         float
    risk_per_share: float    # fill - stop = 1R = 2 x ATR

    r_multiple:    float
    mfe_r:         float
    mae_r:         float
    exit_reason:   str
    year:          int
    # Always False: H1 registers no partial and no target. The field
    # exists so `swing_backtest.compute_stats` — shared verbatim with
    # Phase 1 and 1B so the numbers stay comparable — can read it.
    partial_taken: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


def _sma_at(close: np.ndarray, j: int, window: int) -> Optional[float]:
    """SMA over the `window` closes ending at index j (inclusive).

    Ending AT j, never after it — this is the whole no-lookahead
    guarantee of the exit rule, so it lives in one function that the
    tests can point at.
    """
    if j + 1 < window:
        return None
    v = float(close[j + 1 - window: j + 1].mean())
    return v if np.isfinite(v) else None


def simulate_h1_trade(
    bars: pd.DataFrame,
    signal_index: int,
    setup,
    *,
    ticker: str,
    costs: Optional[CostModel] = None,
    time_stop_bars: int = 7,
    sma_exit_window: int = 5,
    stop_atr_mult: float = 2.0,
    max_bars: int = 30,
) -> Optional[H1Trade]:
    """Walk one H1 trade from its resting limit order to its exit.

    `signal_index` is the bar the panic was detected on. The order rests
    on `signal_index + 1` and only that bar. Returns None when it does
    not fill — an unfilled plan is not a trade and must never enter the
    statistics as a scratch, which is how a strategy's win rate gets
    quietly inflated.
    """
    if costs is None:
        costs = CostModel(slippage_bps=0.0, tick_size=0.0)

    n = len(bars)
    entry_index = signal_index + 1
    if entry_index >= n:
        return None

    limit = float(setup.entry_limit if setup.entry_limit is not None else setup.entry)
    atr = float(setup.atr14 or 0.0)
    if atr <= 0 or limit <= 0:
        return None

    eb = bars.iloc[entry_index]
    if float(eb["low"]) > limit:
        return None          # never traded down to our price — no fill, no trade

    # A resting BUY LIMIT at L: if the bar opens below L we are filled at
    # the open (better than L); otherwise price traded down through L and
    # we are filled at L. Either way we never pay more than L, which is
    # the entire premise — the discount IS the edge.
    raw_fill = min(float(eb["open"]), limit)
    fill = costs.fill(raw_fill, buying=True, order_type="limit")

    # The stop is re-anchored to the actual fill, per the registered rule
    # ("2 x ATR below fill"). This keeps 1R == 2 x ATR on every trade
    # regardless of how well the entry filled, so R-multiples stay
    # comparable across trades — a gap-down fill must not be allowed to
    # manufacture a bigger R by widening the denominator.
    stop = fill - stop_atr_mult * atr
    risk = fill - stop
    if risk <= 0:
        return None

    close_arr = pd.to_numeric(bars["close"], errors="coerce").to_numpy(dtype=float)

    realised_r = 0.0
    mfe_r = 0.0
    mae_r = 0.0
    exit_reason = "END_OF_DATA"
    exit_index = entry_index
    exit_px = float("nan")

    for j in range(entry_index, min(n, entry_index + max_bars)):
        b = bars.iloc[j]
        bars_held = j - entry_index
        hi, lo, op, cl = (float(b["high"]), float(b["low"]),
                          float(b["open"]), float(b["close"]))

        # On the entry bar we cannot prove we held through that bar's
        # high: the limit fills somewhere inside the bar and the high may
        # have printed before it. Same reasoning as swing_backtest.
        eff_high = hi if j > entry_index else max(fill, cl)

        mfe_r = max(mfe_r, (eff_high - fill) / risk)
        mae_r = min(mae_r, (lo - fill) / risk)

        # ── Hard stop first: the pessimistic intrabar assumption ─────
        if lo <= stop:
            raw = min(stop, op)                       # gap-down fills at the open
            px = costs.fill(raw, buying=False, order_type="stop")
            realised_r = (px - fill) / risk
            exit_index, exit_px, exit_reason = j, px, "STOP"
            break

        # ── SMA5 exit: the registered primary exit ───────────────────
        # "Exit at close of the first day the stock closes above SMA5."
        # Computed from closes up to and including j, acted on at j's
        # close. Available on the entry bar too: if the panic day's
        # flush reverses hard enough to close above the 5-day mean, the
        # reversion the strategy was paid to supply has happened.
        sma = _sma_at(close_arr, j, sma_exit_window)
        if sma is not None and cl > sma:
            px = costs.fill(cl, buying=False, order_type="market")
            realised_r = (px - fill) / risk
            exit_index, exit_px, exit_reason = j, px, "SMA_EXIT"
            break

        # ── Time stop ────────────────────────────────────────────────
        if bars_held >= time_stop_bars:
            px = costs.fill(cl, buying=False, order_type="market")
            realised_r = (px - fill) / risk
            exit_index, exit_px, exit_reason = j, px, "TIME_STOP"
            break

        exit_index = j

    if exit_reason == "END_OF_DATA":
        # Unresolved — the series ended (or max_bars bound, which the
        # 7-bar time stop makes unreachable; a test asserts that).
        cl = float(bars.iloc[exit_index]["close"])
        px = costs.fill(cl, buying=False, order_type="market")
        realised_r = (px - fill) / risk
        exit_px = px

    entry_date = str(bars.index[entry_index].date())

    return H1Trade(
        ticker=ticker,
        setup_type=setup.setup_type,
        signal_date=str(bars.index[signal_index].date()),
        entry_date=entry_date,
        exit_date=str(bars.index[exit_index].date()),
        entry_index=entry_index,
        exit_index=exit_index,
        bars_held=exit_index - entry_index,
        signal_close=round(float(close_arr[signal_index]), 4),
        plan_limit=round(limit, 4),
        fill_price=round(fill, 4),
        stop_price=round(stop, 4),
        exit_price=round(float(exit_px), 4),
        atr14=round(atr, 4),
        risk_per_share=round(risk, 4),
        r_multiple=round(realised_r, 4),
        mfe_r=round(mfe_r, 3),
        mae_r=round(mae_r, 3),
        exit_reason=exit_reason,
        year=int(entry_date[:4]),
    )


def replay_ticker_h1(
    bars: pd.DataFrame,
    ticker: str,
    *,
    costs: Optional[CostModel] = None,
    time_stop_bars: int = 7,
    warmup: int = _WARMUP_BARS,
    strategy_kwargs: Optional[dict] = None,
    census: Optional[dict] = None,
) -> list[H1Trade]:
    """Replay one ticker end to end.

    One position per ticker at a time: after an exit, scanning resumes on
    the following bar. Averaging down into a falling panic is precisely
    how this strategy family blows up, so the backtest must not do it.

    `census`, when passed, accumulates first-failed-gate counts. That
    diagnostic is what identified Phase 1B as a signal-frequency problem
    rather than an edge problem, and it costs one dict update per bar.
    """
    from strategies.panic_reversion import evaluate_bars

    skw = dict(strategy_kwargs or {})
    skw.setdefault("check_earnings", False)   # no PIT calendar — see module docstring

    lower = {c.lower(): c for c in bars.columns}
    bars_lower = bars.rename(columns={v: k for k, v in lower.items()})

    n = len(bars_lower)
    trades: list[H1Trade] = []
    i = warmup
    while i < n - 1:
        setup = evaluate_bars(ticker, bars_lower.iloc[: i + 1], **skw)
        if census is not None:
            census["bars"] = census.get("bars", 0) + 1
            if setup.qualifies:
                census["signal"] = census.get("signal", 0) + 1
            else:
                key = _census_key(setup.reject_reason)
                census.setdefault("reasons", {})
                census["reasons"][key] = census["reasons"].get(key, 0) + 1
        if not setup.qualifies:
            i += 1
            continue

        trade = simulate_h1_trade(
            bars_lower, i, setup, ticker=ticker,
            costs=costs, time_stop_bars=time_stop_bars,
        )
        if trade is None:
            if census is not None:
                census["unfilled"] = census.get("unfilled", 0) + 1
            i += 1               # order expired unfilled — not a trade
            continue
        if census is not None:
            census["filled"] = census.get("filled", 0) + 1
        trades.append(trade)
        i = trade.exit_index + 1
    return trades


def _census_key(reason: str) -> str:
    """Bucket a rejection message into a stable gate name."""
    r = (reason or "").lower()
    if "histórico" in r or "ohlc" in r:
        return "history"
    if "preço" in r and "mínimo" in r:
        return "price"
    if "volume" in r:
        return "avgvol"
    if "sma200" in r:
        return "below200"
    if "atr" in r and "pânico" in r:
        return "not_deep_enough"
    if "dias em queda" in r:
        return "not_enough_down_days"
    if "exclusão" in r or "alavancado" in r or "closed-end" in r:
        return "excluded"
    if "earnings" in r:
        return "earnings"
    return "other"


def run_backtest_h1(
    prices: pd.DataFrame,
    *,
    tickers: Optional[list[str]] = None,
    costs: Optional[CostModel] = None,
    time_stop_bars: int = 7,
    progress_every: int = 25,
    strategy_kwargs: Optional[dict] = None,
    census: Optional[dict] = None,
) -> list[H1Trade]:
    """Replay every ticker in a long-format OHLCV frame.

    `prices` columns: date, ticker, open, high, low, close, volume.
    SPY is skipped as a tradeable name (H1 uses no benchmark at all —
    the regime gate is registered OFF — but the cache carries it and it
    is not in the registered universe).
    """
    df = prices.copy()
    df["date"] = pd.to_datetime(df["date"])
    universe = tickers if tickers is not None else sorted(df["ticker"].unique())

    all_trades: list[H1Trade] = []
    for k, t in enumerate(universe, 1):
        if t == "SPY":
            continue
        sub = df[df["ticker"] == t].sort_values("date")
        if len(sub) < _WARMUP_BARS + 10:
            continue
        bars = sub.set_index("date")[["open", "high", "low", "close", "volume"]]
        try:
            all_trades.extend(replay_ticker_h1(
                bars, t, costs=costs, time_stop_bars=time_stop_bars,
                strategy_kwargs=strategy_kwargs, census=census,
            ))
        except Exception as e:
            log.warning(f"[h1] {t}: {e}")
            continue
        if progress_every and k % progress_every == 0:
            log.info(f"[h1] {k}/{len(universe)} tickers, {len(all_trades)} trades")
    return all_trades


def confirm_no_lookahead_h1(
    prices: pd.DataFrame,
    *,
    n_tickers: int = 3,
    strategy_kwargs: Optional[dict] = None,
) -> dict:
    """VERIFY no look-ahead for the H1 path, don't just claim it.

    Replay a ticker, then replay it again with 40 bars of a fabricated 3x
    rally appended. Every trade that closed before the original data ended
    must be byte-identical. If the signal, the limit fill or the SMA5 exit
    peeked forward, that appended future would move them.

    Runs at the start of every validation, so the confirmation is a fact
    about THIS run rather than a claim inherited from the test suite.
    """
    df = prices.copy()
    df["date"] = pd.to_datetime(df["date"])
    candidates = [t for t in sorted(df["ticker"].unique()) if t != "SPY"]

    checked, mismatches, n_compared = [], [], 0
    for t in candidates:
        if len(checked) >= n_tickers:
            break
        sub = df[df["ticker"] == t].sort_values("date")
        bars = sub.set_index("date")[["open", "high", "low", "close", "volume"]]
        if len(bars) < _WARMUP_BARS + 40:
            continue

        base = replay_ticker_h1(bars, t, strategy_kwargs=strategy_kwargs)

        tail = pd.concat([bars.iloc[-1:]] * 40)
        tail.index = pd.date_range(bars.index[-1] + pd.Timedelta(days=1),
                                   periods=40, freq="B")
        tail = tail.copy()
        tail[["open", "high", "low", "close"]] *= 3.0
        ext = replay_ticker_h1(pd.concat([bars, tail]), t,
                               strategy_kwargs=strategy_kwargs)

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

    return {
        "passed":          not mismatches,
        "tickers_checked": checked,
        "trades_compared": n_compared,
        "mismatches":      mismatches[:10],
        "method": (
            "appended 40 bars of a fabricated 3x rally; every trade closing "
            "before the original data ended must be unchanged"
        ),
        "note": None if n_compared else "INCONCLUSIVE — no closed trades to compare",
    }


# ─────────────────────────────────────────────────────────────────────
# The pre-registered gate (edge_search_protocol.md, "Default gate")
# ─────────────────────────────────────────────────────────────────────
#
# The thresholds themselves live in `core.protocol_gate`, shared with H2
# and H3 so three hypotheses cannot drift into three definitions of
# "passed". H1 adds one thing of its own: the registered kill-switch on
# average loss size.
H1_GATE = {**DEFAULT_GATE, "avg_loss_r_alarm": 1.4}


def evaluate_h1_gate(
    stats_costed: dict,
    train_stats: dict,
    test_stats: dict,
    *,
    provenance: Optional[dict] = None,
    lookahead: Optional[dict] = None,
    gate: Optional[dict] = None,
) -> dict:
    """The protocol's default gate, plus H1's registered kill-switch.

    "Extra kill-switch: if avg loss R > 1.4 (stops gapping badly), flag
    it prominently — that is this strategy's known failure mode." It is a
    FLAG, not a check: it does not change the verdict, it tells the
    reader why a verdict looks the way it does.
    """
    g = dict(H1_GATE)
    if gate:
        g.update(gate)

    avg_loss = abs(stats_costed.get("avg_loss_r", 0.0))
    extra = []
    if avg_loss > g["avg_loss_r_alarm"]:
        extra.append(
            f"KILL-SWITCH FLAG: average loss {avg_loss:.2f}R exceeds "
            f"{g['avg_loss_r_alarm']:.1f}R — stops are gapping. This is "
            f"H1's registered known failure mode."
        )

    out = evaluate_default_gate(
        stats_costed, train_stats, test_stats,
        provenance=provenance, lookahead=lookahead, gate=g,
        extra_caveats=extra,
    )
    out["gate"] = g
    return out
