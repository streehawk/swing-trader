"""core/h2_backtest.py — Event-driven replay for H2 (strong-gap continuation).

Same machine as `core/h1_backtest.py`, different registered rules. Kept
separate for the same reason: H1 rests a LIMIT below the market and exits
on SMA5; H2 buys the next open AT MARKET and trails out below SMA10 with
a break-even ratchet. Folding both into one simulator would mean a stack
of `if hypothesis ==` branches through the hottest loop in the project,
and each branch is a place the two paths can silently diverge.

Shared verbatim with every other run in this project: `CostModel`,
`compute_stats`, `stats_by_year`, `simulate_portfolio`,
`universe_provenance`, and the verdict rules in `core.protocol_gate`. H2's
numbers are therefore directly comparable to Phase 1's, Phase 1B's and
H1's — the only thing that differs is the strategy.

No-lookahead contract:
  - The signal for bar i is computed from `bars[:i+1]`. The RVOL leg
    needs bar i's volume, which is why the entry is bar i+1's OPEN: by
    the time the signal is knowable, the session is over.
  - Entry fills at bar i+1's actual open. Nothing about bar i+1's later
    path is consulted to decide whether to enter.
  - The SMA10 trail for bar j uses closes up to and including j and exits
    at j's close — legal, because a close-based rule is knowable at the
    close it acts on.
  - The break-even ratchet is computed at the END of bar j and takes
    effect on bar j+1. Ratcheting mid-bar would use that bar's own high
    to justify a stop the same bar then hits.
  - `confirm_no_lookahead_h2` verifies all of it empirically per run.

Pessimistic fill conventions:
  - Entry is a MARKET order, so it slips ADVERSELY (upward). H2's whole
    premise is that the bad fill is the cost of the signal, so this
    charge is the one that matters most to the verdict — undercharging
    it would be arguing the hypothesis rather than testing it.
  - Stop fills at min(stop, open): a gap-down fills at the open, worse.
  - When one bar spans both the stop and the trail trigger, the STOP is
    assumed to have hit first.
  - The entry bar itself can stop the trade out.

KNOWN DIVERGENCES, all optimistic:
  1. SURVIVORSHIP — the universe is today's screen applied backward.
     `universe_provenance()` detects and labels it.
  2. No spread data, so slippage is a parameter, not a measurement. For a
     strategy that buys the open of a gap day this is the single largest
     modelling uncertainty: real opening auctions in a gapping name are
     wider than any flat bps assumption.
  3. Volume is not checked against position size, so the simulator will
     happily "fill" more than the opening auction could absorb.
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

log = logging.getLogger("h2_backtest")

_WARMUP_BARS = 210

H2_GATE = dict(DEFAULT_GATE)      # the protocol's default gate, unmodified


@dataclass
class H2Trade:
    ticker:        str
    setup_type:    str
    signal_date:   str
    entry_date:    str
    exit_date:     str
    entry_index:   int
    exit_index:    int
    bars_held:     int

    signal_close:  float
    gap_atr:       float          # how big the gap was, in ATRs
    rvol:          float
    fill_price:    float          # next open, after adverse slippage
    stop_price:    float          # initial stop, anchored to the fill
    exit_price:    float
    atr14:         float
    risk_per_share: float         # fill - stop = 1R = 1.5 x ATR

    r_multiple:    float
    mfe_r:         float
    mae_r:         float
    exit_reason:   str
    year:          int
    # Always False — H2 registers no partial and no target. Present so
    # the shared `compute_stats` can read it.
    partial_taken: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


def _sma_at(close: np.ndarray, j: int, window: int) -> Optional[float]:
    """SMA over the `window` closes ending at index j (inclusive).

    Ending AT j, never after it. One function, so the tests have a single
    thing to point at when asserting the trail cannot see the future.
    """
    if j + 1 < window:
        return None
    v = float(close[j + 1 - window: j + 1].mean())
    return v if np.isfinite(v) else None


def simulate_h2_trade(
    bars: pd.DataFrame,
    signal_index: int,
    setup,
    *,
    ticker: str,
    costs: Optional[CostModel] = None,
    time_stop_bars: int = 15,
    trail_sma: int = 10,
    stop_atr_mult: float = 1.5,
    breakeven_at_r: float = 1.0,
    max_bars: int = 60,
) -> Optional[H2Trade]:
    """Walk one H2 trade from the next open to its exit.

    `signal_index` is the gap bar. The order executes at
    `signal_index + 1`'s open, at market — there is no trigger to miss
    and no limit to gap through, so unlike H1 this entry always fills.
    Returns None only when there is no next bar or the plan is malformed.
    """
    if costs is None:
        costs = CostModel(slippage_bps=0.0, tick_size=0.0)

    n = len(bars)
    entry_index = signal_index + 1
    if entry_index >= n:
        return None

    atr = float(setup.atr14 or 0.0)
    if atr <= 0:
        return None

    raw_fill = float(bars.iloc[entry_index]["open"])
    if not np.isfinite(raw_fill) or raw_fill <= 0:
        return None
    # MARKET order → slips against us. This is the charge H2 exists to
    # find out about: the hypothesis is that the signal is worth a bad
    # fill, so the fill must actually be modelled as bad.
    fill = costs.fill(raw_fill, buying=True, order_type="market")

    stop = fill - stop_atr_mult * atr
    risk = fill - stop
    if risk <= 0:
        return None

    close_arr = pd.to_numeric(bars["close"], errors="coerce").to_numpy(dtype=float)

    current_stop = stop
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
        # high — we filled at the open, but the ratchet must not be fed
        # by a high we might have printed before being in. Falling back
        # to the close is the conservative reading.
        eff_high = hi if j > entry_index else max(fill, cl)

        mfe_r = max(mfe_r, (eff_high - fill) / risk)
        mae_r = min(mae_r, (lo - fill) / risk)

        # ── Stop first: the pessimistic intrabar assumption ──────────
        if lo <= current_stop:
            raw = min(current_stop, op)          # gap-down fills at the open
            px = costs.fill(raw, buying=False, order_type="stop")
            realised_r = (px - fill) / risk
            exit_index, exit_px = j, px
            exit_reason = ("STOP" if current_stop <= stop + 1e-9
                           else "BREAKEVEN_STOP")
            break

        # ── SMA10 trail: the registered primary exit ─────────────────
        # "no fixed target — trail: exit at close of first day that
        # closes below SMA10". Strength strategies die by capping their
        # winners, so the only thing allowed to end a winning trade is
        # the trend itself rolling over.
        sma = _sma_at(close_arr, j, trail_sma)
        if sma is not None and cl < sma:
            px = costs.fill(cl, buying=False, order_type="market")
            realised_r = (px - fill) / risk
            exit_index, exit_px, exit_reason = j, px, "TRAIL_SMA"
            break

        # ── Time stop ────────────────────────────────────────────────
        if bars_held >= time_stop_bars:
            px = costs.fill(cl, buying=False, order_type="market")
            realised_r = (px - fill) / risk
            exit_index, exit_px, exit_reason = j, px, "TIME_STOP"
            break

        # ── End-of-bar break-even ratchet, effective NEXT bar ────────
        if mfe_r >= breakeven_at_r:
            current_stop = max(current_stop, fill)

        exit_index = j

    if exit_reason == "END_OF_DATA":
        cl = float(bars.iloc[exit_index]["close"])
        px = costs.fill(cl, buying=False, order_type="market")
        realised_r = (px - fill) / risk
        exit_px = px

    entry_date = str(bars.index[entry_index].date())

    return H2Trade(
        ticker=ticker,
        setup_type=setup.setup_type,
        signal_date=str(bars.index[signal_index].date()),
        entry_date=entry_date,
        exit_date=str(bars.index[exit_index].date()),
        entry_index=entry_index,
        exit_index=exit_index,
        bars_held=exit_index - entry_index,
        signal_close=round(float(close_arr[signal_index]), 4),
        gap_atr=round(float(getattr(setup, "_gap_atr", 0.0) or 0.0), 3),
        rvol=round(float(getattr(setup, "volume_ratio_20d", 0.0) or 0.0), 3),
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


def _census_key(reason: str) -> str:
    """Bucket a rejection message into a stable gate name."""
    r = (reason or "").lower()
    if "regime" in r:
        return "regime"
    if "histórico" in r or "ohlc" in r:
        return "history"
    if "preço" in r and "mínimo" in r:
        return "price"
    if "volume médio" in r:
        return "avgvol"
    if "contexto" in r:
        return "trend_context"
    if "gap" in r and "atr" in r:
        return "gap_too_small"
    if "máxima" in r:
        return "not_a_new_high"
    if "rvol" in r:
        return "rvol"
    if "cobertura de earnings" in r:
        return "no_earnings_coverage"
    if "earnings" in r:
        return "earnings_blackout"
    if "exclusão" in r or "alavancado" in r or "closed-end" in r:
        return "excluded"
    return "other"


def replay_ticker_h2(
    bars: pd.DataFrame,
    ticker: str,
    *,
    spy_bars: Optional[pd.DataFrame] = None,
    costs: Optional[CostModel] = None,
    time_stop_bars: int = 15,
    warmup: int = _WARMUP_BARS,
    strategy_kwargs: Optional[dict] = None,
    census: Optional[dict] = None,
) -> list[H2Trade]:
    """Replay one ticker end to end.

    One position per ticker at a time — after an exit, scanning resumes
    on the following bar.

    `spy_bars` drives the regime gate, which H2 registers ON. It is
    REINDEXED onto this ticker's dates, so slicing by position gives SPY
    as of the same bar, never a later one. Passing None makes the gate
    fail closed (no trades), matching live behaviour on a SPY fetch error.
    """
    from strategies.gap_continuation import evaluate_bars

    skw = dict(strategy_kwargs or {})

    lower = {c.lower(): c for c in bars.columns}
    bars_lower = bars.rename(columns={v: k for k, v in lower.items()})

    spy_aligned = None
    if spy_bars is not None:
        sl = {c.lower(): c for c in spy_bars.columns}
        spy_l = spy_bars.rename(columns={v: k for k, v in sl.items()})
        spy_aligned = spy_l.reindex(bars_lower.index).ffill()

    n = len(bars_lower)
    trades: list[H2Trade] = []
    i = warmup
    while i < n - 1:
        setup = evaluate_bars(
            ticker, bars_lower.iloc[: i + 1],
            spy_hist=None if spy_aligned is None else spy_aligned.iloc[: i + 1],
            **skw,
        )
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

        trade = simulate_h2_trade(
            bars_lower, i, setup, ticker=ticker,
            costs=costs, time_stop_bars=time_stop_bars,
        )
        if trade is None:
            i += 1
            continue
        if census is not None:
            census["filled"] = census.get("filled", 0) + 1
        trades.append(trade)
        i = trade.exit_index + 1
    return trades


def run_backtest_h2(
    prices: pd.DataFrame,
    *,
    tickers: Optional[list[str]] = None,
    costs: Optional[CostModel] = None,
    time_stop_bars: int = 15,
    progress_every: int = 25,
    strategy_kwargs: Optional[dict] = None,
    census: Optional[dict] = None,
) -> list[H2Trade]:
    """Replay every ticker in a long-format OHLCV frame.

    `prices` columns: date, ticker, open, high, low, close, volume.
    """
    df = prices.copy()
    df["date"] = pd.to_datetime(df["date"])
    universe = tickers if tickers is not None else sorted(df["ticker"].unique())

    # Split by ticker ONCE. The obvious `df[df["ticker"] == t]` inside the
    # loop rescans the whole frame per ticker, so cost grows with
    # (tickers x total rows): at 30 tickers that is ~1s/ticker, at 395 it
    # is ~17s/ticker, and the run goes from minutes to hours. Grouping is
    # exactly equivalent — groupby preserves row order within a group,
    # and the sort below makes the ordering explicit regardless.
    groups = {t: g for t, g in df.groupby("ticker", sort=False)}

    spy_rows = groups.get("SPY")
    spy_bars = (
        spy_rows.sort_values("date").set_index("date")[
            ["open", "high", "low", "close", "volume"]]
        if spy_rows is not None and len(spy_rows) else None
    )
    if spy_bars is None:
        log.error("[h2] SPY missing — the regime gate fails closed, zero trades.")

    all_trades: list[H2Trade] = []
    for k, t in enumerate(universe, 1):
        if t == "SPY":
            continue
        sub = groups.get(t)
        if sub is None or len(sub) < _WARMUP_BARS + 10:
            continue
        sub = sub.sort_values("date")
        bars = sub.set_index("date")[["open", "high", "low", "close", "volume"]]
        try:
            all_trades.extend(replay_ticker_h2(
                bars, t, spy_bars=spy_bars, costs=costs,
                time_stop_bars=time_stop_bars,
                strategy_kwargs=strategy_kwargs, census=census,
            ))
        except Exception as e:
            log.warning(f"[h2] {t}: {e}")
            continue
        if progress_every and k % progress_every == 0:
            log.info(f"[h2] {k}/{len(universe)} tickers, {len(all_trades)} trades")
    return all_trades


def confirm_no_lookahead_h2(
    prices: pd.DataFrame,
    *,
    n_tickers: int = 3,
    max_scan: int = 120,
    strategy_kwargs: Optional[dict] = None,
) -> dict:
    """VERIFY no look-ahead for the H2 path, don't just claim it.

    Replay a ticker, then replay it again with 40 bars of a fabricated 3x
    rally appended. Every trade that closed before the original data
    ended must be byte-identical. If the gap test, the 20-day high, the
    RVOL denominator, the regime gate, the fill or the SMA10 trail peeked
    forward, that appended future would move them.
    """
    df = prices.copy()
    df["date"] = pd.to_datetime(df["date"])
    groups = {t: g for t, g in df.groupby("ticker", sort=False)}

    spy_rows = groups.get("SPY")
    spy = (spy_rows.sort_values("date").set_index("date")[
               ["open", "high", "low", "close", "volume"]]
           if spy_rows is not None and len(spy_rows) else None)

    candidates = [t for t in sorted(groups) if t != "SPY"]
    checked, mismatches, n_compared = [], [], 0
    scanned = 0

    for t in candidates:
        if len(checked) >= n_tickers or scanned >= max_scan:
            break
        sub = groups[t].sort_values("date")
        bars = sub.set_index("date")[["open", "high", "low", "close", "volume"]]
        if len(bars) < _WARMUP_BARS + 40:
            continue

        scanned += 1
        base = replay_ticker_h2(bars, t, spy_bars=spy,
                                strategy_kwargs=strategy_kwargs)
        # Only tickers that actually TRADE can verify anything. H2's
        # signal is rare, so taking the first N names alphabetically
        # yields an INCONCLUSIVE check — which is not a check. Keep
        # scanning until N names with closed trades are found.
        if not any(a.exit_index < len(bars) - 1 for a in base):
            continue

        tail = pd.concat([bars.iloc[-1:]] * 40)
        tail.index = pd.date_range(bars.index[-1] + pd.Timedelta(days=1),
                                   periods=40, freq="B")
        tail = tail.copy()
        tail[["open", "high", "low", "close"]] *= 3.0
        ext = replay_ticker_h2(pd.concat([bars, tail]), t, spy_bars=spy,
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
        "tickers_scanned": scanned,
        "trades_compared": n_compared,
        "mismatches":      mismatches[:10],
        "method": (
            "appended 40 bars of a fabricated 3x rally; every trade closing "
            "before the original data ended must be unchanged. Tickers are "
            "selected by having closed trades, not alphabetically — a check "
            "over zero trades verifies nothing."
        ),
        "note": None if n_compared else "INCONCLUSIVE — no closed trades to compare",
    }


def evaluate_h2_gate(
    stats_costed: dict,
    train_stats: dict,
    test_stats: dict,
    *,
    provenance: Optional[dict] = None,
    lookahead: Optional[dict] = None,
    gate: Optional[dict] = None,
) -> dict:
    """The protocol's default gate, unmodified. H2 registers no extra
    kill-switch of its own."""
    return evaluate_default_gate(
        stats_costed, train_stats, test_stats,
        provenance=provenance, lookahead=lookahead,
        gate=gate or H2_GATE,
    )
