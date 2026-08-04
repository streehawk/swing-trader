"""core/basket_backtest.py — A1-2: multi-instrument runner + portfolio sim.

`c2_etf_basket_addendum.md` limits new code to exactly these two things.
The strategy is NOT re-implemented here: entries, exits, the 25-day valve
and the fill conventions all come from `strategies/rsi2.py` and
`core/c_series_backtest.replay_variant` unchanged. If a number here
disagrees with the registered SPY verdict, the bug is in this file.

WHAT IS ALLOWED TO DIFFER PER INSTRUMENT: the cost model, and nothing
else. The addendum registers 2bps/side for QQQ/DIA/IWM and 5bps/side for
the nine sector SPDRs. Every rule, threshold and exit is identical across
all twelve — a per-instrument rule change would be tuning, and would make
"the edge replicates" unfalsifiable.

THE PORTFOLIO SIMULATOR EXISTS BECAUSE OF H1. H1 measured +0.028R per
trade and died anyway: its signals clustered, the position cap took a
first-come subset rather than the best subset, and the trades actually
reachable expectancy'd out NEGATIVE while the ones the cap refused made
money. Sector ETFs will cluster harder than single stocks — when the
market dips, everything dips — so the capacity autopsy is repeated here
deliberately, and `taken` vs `refused` expectancy is a first-class output
rather than a footnote.

DAILY MARK-TO-MARKET, not trade-close marking. The C-series postmortem
flagged that `equity_curve_stats` marks the strategy only at exits while
buy-and-hold is marked daily, which flatters the strategy's drawdown.
Portfolio equity here is marked every session, so the drawdown comparison
against buy-and-hold is apples to apples.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Iterable, Optional, Sequence

import numpy as np
import pandas as pd

from core.c_series_backtest import (
    FREE, CostModelPct, CTrade, compute_c_stats, replay_variant,
)
from strategies.rsi2 import Indicators

log = logging.getLogger("basket")

# ── Registered in c2_etf_basket_addendum.md. Frozen. ─────────────────
BASKET_COSTS_BPS = {
    "QQQ": 2.0, "DIA": 2.0, "IWM": 2.0,
    "XLB": 5.0, "XLE": 5.0, "XLF": 5.0, "XLI": 5.0, "XLK": 5.0,
    "XLP": 5.0, "XLU": 5.0, "XLV": 5.0, "XLY": 5.0,
}
BASKET_TICKERS = tuple(BASKET_COSTS_BPS)

BROAD_INDEX = ("QQQ", "DIA", "IWM")          # for the attribution question
SECTORS = tuple(t for t in BASKET_TICKERS if t not in BROAD_INDEX)

SPY_COSTS_BPS = 2.0        # the control's registered cost, from the C-series
VARIANT = "C2"             # C1 and C3 are explicitly out of scope
MAX_CONCURRENT = 3         # P-capped
POST_BOOK_START = "2008-01-01"

# G1/G2/G3, copied from the addendum before any basket result existed.
BASKET_GATE = {
    "g1_min_positive_instruments": 8,      # of 12
    "g1_partial_floor":            5,      # 5-7 = PARTIAL, <=4 = REFUTED
    "g1_pooled_min_pf":            1.3,
    "g1_pooled_min_avg_pct":       0.20,
    "g1_pooled_min_trades":        400,
    "g2_min_trades_per_year":      25,     # P-capped
    "g3_min_next_open_pf":         1.1,
}

# Ruled by the user 2026-08-03, before any basket result existed:
#
#   P-capped is sized FIXED_SLOTS — 1/3 of equity per slot, remainder in
#   cash — and G2 is judged on it. The reason given, recorded here because
#   it is the justification and not a preference: the gate must judge the
#   system as a real account would run it, and 100% of an account in one
#   sector ETF with no stop is not that. `equal_open` is reported as a
#   labelled diagnostic.
#
#   P-UNCAPPED KEEPS equal_open, necessarily: with no cap there is no slot
#   count, so "1/N of equity" has no N. The two sims therefore differ in
#   sizing as well as in the cap, which is why P-uncapped is context and
#   P-capped is the gate.
CAPPED_SIZING = "fixed_slots"
UNCAPPED_SIZING = "equal_open"


def cost_model(ticker: str, *, costs_on: bool = True) -> CostModelPct:
    """The registered per-instrument cost. Unknown tickers are a bug, not a
    default — silently charging 2bps to something that should pay 5 would
    quietly improve the result."""
    if not costs_on:
        return FREE
    t = ticker.upper()
    if t == "SPY":
        return CostModelPct(slippage_bps=SPY_COSTS_BPS)
    if t not in BASKET_COSTS_BPS:
        raise KeyError(f"{ticker!r} is not in the registered basket; "
                       f"no cost model exists for it")
    return CostModelPct(slippage_bps=BASKET_COSTS_BPS[t])


# ─────────────────────────────────────────────────────────────────────
# Loading
# ─────────────────────────────────────────────────────────────────────

def load_basket(path) -> dict[str, pd.DataFrame]:
    """Long parquet -> {ticker: OHLCV frame indexed by date}.

    Split by ticker with ONE groupby. The obvious `df[df.ticker == t]`
    inside a loop rescans the whole frame per ticker; on H2 that turned a
    20-minute run into a projected 5-hour one.
    """
    df = pd.read_parquet(path)
    df["date"] = pd.to_datetime(df["date"])
    cols = ["open", "high", "low", "close", "volume"]
    out: dict[str, pd.DataFrame] = {}
    for t, g in df.groupby("ticker", sort=True):
        out[str(t)] = (g.sort_values("date").set_index("date")[cols]).copy()
    return out


def load_single(path, ticker: str = "SPY") -> pd.DataFrame:
    """Load a one-ticker cache (the registered `spy_extended.parquet`)."""
    df = pd.read_parquet(path)
    df["date"] = pd.to_datetime(df["date"])
    if "ticker" in df.columns:
        df = df[df["ticker"] == ticker]
    return df.sort_values("date").set_index("date")[
        ["open", "high", "low", "close", "volume"]]


# ─────────────────────────────────────────────────────────────────────
# A1-3 — per-instrument runs
# ─────────────────────────────────────────────────────────────────────

def run_instrument(
    bars: pd.DataFrame,
    ticker: str,
    *,
    fill_mode: str = "close",
    costs_on: bool = True,
    start: Optional[str] = POST_BOOK_START,
    end: Optional[str] = None,
    variant: str = VARIANT,
) -> list[CTrade]:
    """One instrument, one variant. A pass-through to the registered engine."""
    return replay_variant(
        bars, variant, fill_mode=fill_mode,
        costs=cost_model(ticker, costs_on=costs_on),
        start=start, end=end,
    )


def run_basket(
    bars_by_ticker: dict[str, pd.DataFrame],
    *,
    fill_mode: str = "close",
    costs_on: bool = True,
    start: Optional[str] = POST_BOOK_START,
    end: Optional[str] = None,
    tickers: Sequence[str] = BASKET_TICKERS,
    variant: str = VARIANT,
) -> dict[str, list[CTrade]]:
    out: dict[str, list[CTrade]] = {}
    for t in tickers:
        if t not in bars_by_ticker:
            log.warning("%s missing from the cache — skipped", t)
            continue
        out[t] = run_instrument(bars_by_ticker[t], t, fill_mode=fill_mode,
                                costs_on=costs_on, start=start, end=end,
                                variant=variant)
    return out


def bars_in_window(bars: pd.DataFrame, start: Optional[str],
                   end: Optional[str] = None) -> int:
    idx = bars.index
    lo = 0 if start is None else int(np.searchsorted(
        idx.values, np.datetime64(pd.Timestamp(start))))
    hi = len(idx) if end is None else int(np.searchsorted(
        idx.values, np.datetime64(pd.Timestamp(end)), side="right"))
    return max(hi - lo, 0)


def pooled_stats(by_ticker: dict[str, list[CTrade]]) -> dict:
    """All trades from all instruments, pooled.

    The addendum is explicit that these instruments are heavily correlated,
    so a pooled t-stat OVERSTATES significance — the effective sample is
    smaller than the trade count. Reported because the gate asks for it;
    read next to the clustering analysis, never alone.
    """
    all_trades = [t for ts in by_ticker.values() for t in ts]
    s = compute_c_stats(all_trades)
    s["n_instruments"] = len(by_ticker)
    s["caveat"] = ("pooled across correlated instruments — the effective "
                   "independent sample is smaller than n_trades, so the "
                   "t-stat is an upper bound on significance")
    return s


# ─────────────────────────────────────────────────────────────────────
# A1-4 — the portfolio simulator
# ─────────────────────────────────────────────────────────────────────

@dataclass
class Signal:
    """One candidate trade, with the tiebreak key attached.

    `cum_rsi` is read at the SIGNAL bar (== the entry bar under close fill),
    which is the value a trader ranking candidates after the close would
    actually have.
    """
    ticker:      str
    entry_date:  str
    exit_date:   str
    entry_index: int
    exit_index:  int
    pct_return:  float
    cum_rsi:     float
    trade:       CTrade = field(repr=False)


def build_signals(
    by_ticker: dict[str, list[CTrade]],
    bars_by_ticker: dict[str, pd.DataFrame],
) -> list[Signal]:
    """Attach each trade's cumulative-RSI reading, then order by date.

    Indicators are rebuilt on the instrument's FULL close history, matching
    `replay_variant` — a warm-up carved out of the analysis window would
    give a different SMA200 than the one the signal actually fired on.
    """
    sigs: list[Signal] = []
    for t, trades in by_ticker.items():
        if not trades:
            continue
        closes = pd.to_numeric(bars_by_ticker[t]["close"],
                               errors="coerce").to_numpy(dtype=float)
        ind = Indicators.build(closes)
        for tr in trades:
            i = tr.entry_index
            cr = float(ind.cum_rsi[i]) if 0 <= i < len(ind.cum_rsi) else float("nan")
            sigs.append(Signal(
                ticker=t, entry_date=tr.entry_date, exit_date=tr.exit_date,
                entry_index=tr.entry_index, exit_index=tr.exit_index,
                pct_return=tr.pct_return, cum_rsi=cr, trade=tr,
            ))
    # Deterministic order: date, then most-oversold, then ticker. The final
    # key removes any dependence on dict iteration order, so two runs of the
    # same data can never disagree.
    sigs.sort(key=lambda s: (s.entry_date,
                             s.cum_rsi if np.isfinite(s.cum_rsi) else 1e9,
                             s.ticker))
    return sigs


def _value_path(bars: pd.DataFrame, tr: CTrade) -> tuple[list, np.ndarray]:
    """Daily marked value of one position, entry bar through exit bar.

    The endpoints are the COST-ADJUSTED fills and the interior is the raw
    close, so entry slippage shows up as a drag on the first held day and
    exit slippage on the last. Charging both at the close would double-count
    the spread into the middle of the trade.
    """
    idx = bars.index
    closes = pd.to_numeric(bars["close"], errors="coerce").to_numpy(dtype=float)
    e, x = tr.entry_index, tr.exit_index
    vals = closes[e:x + 1].astype(float).copy()
    vals[0] = tr.entry_price
    vals[-1] = tr.exit_price
    return list(idx[e:x + 1]), vals


@dataclass
class PortfolioResult:
    name:            str
    equity:          pd.Series
    taken:           list[Signal]
    refused:         list[Signal]
    stats:           dict
    daily_open:      pd.Series
    cluster:         dict


def simulate_portfolio(
    signals: list[Signal],
    bars_by_ticker: dict[str, pd.DataFrame],
    *,
    name: str,
    max_concurrent: Optional[int] = None,
    sizing: str = "equal_open",
    priority: str = "most_oversold",
    start: str = POST_BOOK_START,
    end: Optional[str] = None,
    seed: int = 20260803,
) -> PortfolioResult:
    """Walk the calendar day by day, accepting signals under the cap.

    SLOT OCCUPANCY, registered here because the addendum does not say and
    the choice is not neutral: a position holds its slot from its entry bar
    THROUGH its exit bar inclusive. A signal on the same day another
    position exits therefore cannot take that slot. The alternative —
    recycling a slot within the same session — assumes you can sell at the
    close and buy at the same close, which is one trade too many for one
    print.

    SIZING:
      "equal_open"  capital split equally across whatever is open, so the
                    account is fully invested whenever >= 1 position is
                    open. This is the addendum's literal wording.
      "fixed_slots" each of `max_concurrent` slots holds 1/N of equity, so
                    the invested fraction is k/N and the rest sits in cash.
                    Reported as a DIAGNOSTIC only.

    `priority="random"` is likewise a diagnostic: the addendum requires the
    most-oversold rule to stand for this run whatever the comparison shows.
    """
    if sizing not in ("equal_open", "fixed_slots"):
        raise ValueError(f"unknown sizing {sizing!r}")
    if priority not in ("most_oversold", "random"):
        raise ValueError(f"unknown priority {priority!r}")
    if sizing == "fixed_slots" and not max_concurrent:
        raise ValueError("fixed_slots sizing needs a slot count")

    # One calendar across every instrument in the window. Union, not
    # intersection: a session is a session even if one ETF had not listed.
    lo, hi = pd.Timestamp(start), (pd.Timestamp(end) if end else None)
    all_dates = sorted({d for t in bars_by_ticker
                        for d in bars_by_ticker[t].index
                        if d >= lo and (hi is None or d <= hi)})
    if not all_dates:
        raise ValueError("no sessions in the requested window")
    pos_of = {d: i for i, d in enumerate(all_dates)}

    by_entry: dict[str, list[Signal]] = {}
    for s in signals:
        by_entry.setdefault(s.entry_date, []).append(s)

    rng = np.random.default_rng(seed)
    taken: list[Signal] = []
    refused: list[Signal] = []
    open_until: dict[str, str] = {}     # ticker -> exit_date of a live position
    cluster_sizes: dict[str, int] = {}

    for d in all_dates:
        ds = str(d.date())
        # Release slots whose exit bar is behind us.
        for t in [t for t, x in open_until.items() if x < ds]:
            open_until.pop(t)

        cands = [s for s in by_entry.get(ds, []) if s.ticker not in open_until]
        if not cands:
            continue
        cluster_sizes[ds] = len(cands)

        if priority == "most_oversold":
            cands.sort(key=lambda s: (s.cum_rsi if np.isfinite(s.cum_rsi) else 1e9,
                                      s.ticker))
        else:
            rng.shuffle(cands)

        for s in cands:
            if max_concurrent is not None and len(open_until) >= max_concurrent:
                refused.append(s)
                continue
            open_until[s.ticker] = s.exit_date
            taken.append(s)

    # ── Daily mark-to-market ─────────────────────────────────────────
    n = len(all_dates)
    ret_sum = np.zeros(n)
    ret_cnt = np.zeros(n)
    held = np.zeros(n)                # positions occupying a slot that day

    for s in taken:
        bars = bars_by_ticker[s.ticker]
        dates, vals = _value_path(bars, s.trade)
        prev = vals[0]
        for k in range(len(dates)):
            p = pos_of.get(dates[k])
            if p is None:
                continue
            held[p] += 1
            if k > 0:
                ret_sum[p] += vals[k] / prev - 1.0
                ret_cnt[p] += 1
            prev = vals[k]

    if sizing == "equal_open":
        denom = np.where(ret_cnt > 0, ret_cnt, 1.0)
    else:
        denom = np.full(n, float(max_concurrent))
    daily = ret_sum / denom

    equity = pd.Series(np.cumprod(1.0 + daily), index=pd.DatetimeIndex(all_dates))
    stats = _equity_stats(equity)

    r = np.array([s.pct_return for s in taken], dtype=float)
    rr = np.array([s.pct_return for s in refused], dtype=float)
    years = max((all_dates[-1] - all_dates[0]).days / 365.25, 1e-9)

    stats.update({
        "n_signals":        len(taken) + len(refused),
        "n_taken":          len(taken),
        "n_refused":        len(refused),
        "pct_signals_refused": (round(len(refused) / (len(taken) + len(refused)) * 100, 2)
                                if (taken or refused) else 0.0),
        "trades_per_year":  round(len(taken) / years, 2),
        "years":            round(years, 2),
        "pct_days_position_open": round(float((held > 0).sum()) / n * 100, 2),
        "avg_positions_open":     round(float(held.mean()), 3),
        "max_positions_open":     int(held.max()) if n else 0,
        # The H1 autopsy. If refused > taken, the cap is selecting badly.
        "taken_avg_pct":    round(float(r.mean()), 4) if len(r) else None,
        "refused_avg_pct":  round(float(rr.mean()), 4) if len(rr) else None,
        "taken_win_rate":   round(float((r > 0).mean()), 4) if len(r) else None,
        "refused_win_rate": round(float((rr > 0).mean()), 4) if len(rr) else None,
        "sizing":           sizing,
        "priority":         priority,
        "max_concurrent":   max_concurrent,
    })

    biggest = max(cluster_sizes.items(), key=lambda kv: kv[1]) if cluster_sizes else None
    cluster = {
        "largest_single_day_cluster": biggest[1] if biggest else 0,
        "largest_cluster_date": biggest[0] if biggest else None,
        "days_with_signals": len(cluster_sizes),
        "days_with_multi_signals": sum(1 for v in cluster_sizes.values() if v > 1),
        "signal_days_over_cap": sum(1 for v in cluster_sizes.values()
                                    if max_concurrent and v > max_concurrent),
        "cluster_histogram": {
            str(k): sum(1 for v in cluster_sizes.values() if v == k)
            for k in sorted(set(cluster_sizes.values()))
        },
    }

    return PortfolioResult(
        name=name, equity=equity, taken=taken, refused=refused,
        stats=stats, daily_open=pd.Series(held, index=pd.DatetimeIndex(all_dates)),
        cluster=cluster,
    )


def _equity_stats(equity: pd.Series) -> dict:
    """CAGR / max drawdown / MAR from a DAILY-marked curve."""
    arr = equity.to_numpy(dtype=float)
    if len(arr) < 2:
        return {"n_days": len(arr)}
    peak = np.maximum.accumulate(arr)
    dd = (arr - peak) / peak
    years = max((equity.index[-1] - equity.index[0]).days / 365.25, 1e-9)
    cagr = (float(arr[-1]) ** (1.0 / years) - 1.0) * 100.0
    mdd = float(-dd.min()) * 100.0
    return {
        "n_days":           len(arr),
        "start":            str(equity.index[0].date()),
        "end":              str(equity.index[-1].date()),
        "final_multiple":   round(float(arr[-1]), 4),
        "total_return_pct": round((float(arr[-1]) - 1.0) * 100, 2),
        "cagr_pct":         round(cagr, 3),
        "max_drawdown_pct": round(mdd, 2),
        "mar":              round(cagr / mdd, 4) if mdd > 1e-9 else None,
        "marking":          "daily mark-to-market (not trade-close marked)",
    }


def buy_and_hold_daily(bars: pd.DataFrame, start: str,
                       end: Optional[str] = None) -> dict:
    """Daily-marked buy-and-hold on the same calendar — the G2 yardstick."""
    idx = bars.index
    lo = int(np.searchsorted(idx.values, np.datetime64(pd.Timestamp(start))))
    hi = (len(idx) if end is None
          else int(np.searchsorted(idx.values,
                                   np.datetime64(pd.Timestamp(end)), side="right")))
    c = pd.to_numeric(bars["close"], errors="coerce").to_numpy(dtype=float)[lo:hi]
    if len(c) < 2:
        return {}
    eq = pd.Series(c / c[0], index=idx[lo:hi])
    return _equity_stats(eq)


# ─────────────────────────────────────────────────────────────────────
# Clustering / correlation (the addendum's honesty requirement)
# ─────────────────────────────────────────────────────────────────────

def signal_overlap_matrix(by_ticker: dict[str, list[CTrade]]) -> dict:
    """Pairwise Jaccard overlap of ENTRY DATES between instruments.

    Twelve positive results are not twelve confirmations if they all fire on
    the same days. This is the number that decides how much the pooled
    result is allowed to count for.
    """
    dates = {t: {tr.entry_date for tr in trs} for t, trs in by_ticker.items()}
    tickers = sorted(dates)
    matrix, pairs = {}, []
    for a in tickers:
        row = {}
        for b in tickers:
            u = dates[a] | dates[b]
            j = len(dates[a] & dates[b]) / len(u) if u else 0.0
            row[b] = round(j, 4)
            if a < b:
                pairs.append((a, b, j))
        matrix[a] = row
    pairs.sort(key=lambda p: -p[2])
    off = [p[2] for p in pairs]
    return {
        "matrix": matrix,
        "mean_pairwise_jaccard": round(float(np.mean(off)), 4) if off else 0.0,
        "median_pairwise_jaccard": round(float(np.median(off)), 4) if off else 0.0,
        "most_overlapping": [{"a": a, "b": b, "jaccard": round(j, 4)}
                             for a, b, j in pairs[:5]],
        "least_overlapping": [{"a": a, "b": b, "jaccard": round(j, 4)}
                              for a, b, j in pairs[-5:]],
    }


# ─────────────────────────────────────────────────────────────────────
# A1-2 — the CONTROL
# ─────────────────────────────────────────────────────────────────────

def evaluate_gates(
    per_instrument: dict[str, dict],
    pooled_close: dict,
    pooled_next_open: dict,
    train_stats: dict,
    test_stats: dict,
    capped_stats: dict,
    spy_buy_hold: dict,
    gate: Optional[dict] = None,
) -> dict:
    """G1/G2/G3 exactly as registered in the addendum.

    ONE SPEC GAP, handled in the open rather than resolved in whichever
    direction the data happens to favour. G1 defines SUCCESS as
    ">= 8 of 12 positive AND the pooled bar AND the split sign", PARTIAL as
    "5-7 of 12 positive", REFUTED as "<= 4 of 12 positive". A run with >= 8
    positive instruments that MISSES the pooled bar satisfies none of those
    three definitions. It is reported here as PARTIAL with the gap named
    explicitly — calling it SUCCESS would ignore a conjunction the addendum
    wrote down, and calling it REFUTED would ignore the instrument count it
    also wrote down.
    """
    g = dict(BASKET_GATE)
    if gate:
        g.update(gate)

    positive = sorted(t for t, s in per_instrument.items()
                      if s.get("n_trades") and s.get("avg_pct", 0.0) > 0)
    negative = sorted(t for t in per_instrument if t not in positive)
    n_pos, n_inst = len(positive), len(per_instrument)

    pf = pooled_close.get("profit_factor", 0.0)
    avg = pooled_close.get("avg_pct", 0.0)
    n = pooled_close.get("n_trades", 0)
    tr = train_stats.get("avg_pct") if train_stats.get("n_trades") else None
    te = test_stats.get("avg_pct") if test_stats.get("n_trades") else None
    sign_holds = (tr is not None and te is not None
                  and ((tr > 0 and te > 0) or (tr < 0 and te < 0)))

    g1_checks = [
        ("positive_instruments", n_pos >= g["g1_min_positive_instruments"],
         f"{n_pos}/{n_inst} instruments positive after costs "
         f"(need >= {g['g1_min_positive_instruments']})"),
        ("pooled_profit_factor", pf >= g["g1_pooled_min_pf"],
         f"pooled PF {pf:.3f} (need >= {g['g1_pooled_min_pf']})"),
        ("pooled_avg_pct", avg >= g["g1_pooled_min_avg_pct"],
         f"pooled avg {avg:+.4f}%/trade (need >= +{g['g1_pooled_min_avg_pct']:.2f}%)"),
        ("pooled_trades", n >= g["g1_pooled_min_trades"],
         f"{n} pooled trades (need >= {g['g1_pooled_min_trades']})"),
        ("split_sign_holds", sign_holds,
         (f"train {tr:+.4f}% vs test {te:+.4f}%"
          if (tr is not None and te is not None) else "a split is empty")),
    ]
    g1_all = all(ok for _, ok, _ in g1_checks)

    spec_gap = None
    if g1_all:
        g1 = "SUCCESS"
    elif n_pos <= g["g1_partial_floor"] - 1:
        g1 = "REFUTED"
    elif n_pos >= g["g1_min_positive_instruments"]:
        g1 = "PARTIAL"
        spec_gap = (f"{n_pos}/{n_inst} instruments are positive, which meets the "
                    f"SUCCESS count, but a pooled bar failed. The addendum defines "
                    f"PARTIAL by instrument count (5-7) and so covers neither this "
                    f"case nor its opposite; reported as PARTIAL because a "
                    f"registered conjunction was not met.")
    else:
        g1 = "PARTIAL"

    # G2 — judged on P-capped, fixed_slots sizing (user ruling 2026-08-03).
    tpy = capped_stats.get("trades_per_year", 0.0)
    mar = capped_stats.get("mar")
    bh_mar = spy_buy_hold.get("mar")
    mar_ok = (mar is not None and bh_mar is not None and mar >= bh_mar)
    g2_checks = [
        ("trades_per_year", tpy >= g["g2_min_trades_per_year"],
         f"P-capped {tpy:.2f} trades/yr (need >= {g['g2_min_trades_per_year']})"),
        ("mar_vs_buy_hold", mar_ok,
         f"P-capped MAR {mar} vs SPY buy-and-hold MAR {bh_mar}"),
    ]
    g2 = "PASS" if all(ok for _, ok, _ in g2_checks) else "FAIL"

    # G3 — fragility, same bar as the original spec.
    no_pf = pooled_next_open.get("profit_factor", 0.0)
    g3_ok = no_pf >= g["g3_min_next_open_pf"]
    g3_checks = [("pooled_next_open_pf", g3_ok,
                  f"pooled next-open PF {no_pf:.3f} "
                  f"(need >= {g['g3_min_next_open_pf']})")]
    g3 = "PASS" if g3_ok else "FAIL"

    def fmt(checks):
        return [{"name": nm, "pass": ok, "detail": d} for nm, ok, d in checks]

    return {
        "gate": g,
        "G1": {"verdict": g1, "checks": fmt(g1_checks),
               "positive_instruments": positive,
               "negative_instruments": negative,
               "spec_gap": spec_gap},
        "G2": {"verdict": g2, "checks": fmt(g2_checks),
               "sizing": CAPPED_SIZING,
               "note": "judged on P-capped, fixed_slots sizing (user ruling)"},
        "G3": {"verdict": g3, "checks": fmt(g3_checks)},
    }


CONTROL_FIELDS = ("entry_date", "exit_date", "entry_index", "exit_index",
                  "bars_held", "entry_price", "exit_price", "pct_return",
                  "mae_pct", "mfe_pct", "exit_reason")


def spy_control(
    spy_bars: pd.DataFrame,
    registered_trades: Iterable[dict],
    *,
    start: str = POST_BOOK_START,
    end: Optional[str] = None,
) -> dict:
    """Run SPY through the NEW runner; demand the registered C2 result back.

    Compared trade-by-trade against the stored artefact of the run that
    produced the published verdict, not against a fresh recomputation — a
    recomputation could reproduce a shared bug and agree with itself.

    A mismatch means THIS FILE is wrong. The addendum is explicit: fix the
    pipeline, never the strategy.
    """
    got = run_instrument(spy_bars, "SPY", fill_mode="close", costs_on=True,
                         start=start, end=end, variant=VARIANT)
    want = list(registered_trades)

    diffs: list[str] = []
    if len(got) != len(want):
        diffs.append(f"trade count {len(got)} != registered {len(want)}")

    for k, (a, b) in enumerate(zip(got, want)):
        d = a.to_dict()
        for f in CONTROL_FIELDS:
            if f not in b:
                continue
            if d[f] != b[f]:
                diffs.append(f"trade {k} ({b.get('entry_date')}): "
                             f"{f} {d[f]!r} != registered {b[f]!r}")

    got_stats = compute_c_stats(got)
    return {
        "passed": not diffs,
        "n_trades_new": len(got),
        "n_trades_registered": len(want),
        "fields_compared": list(CONTROL_FIELDS),
        "mismatches": diffs[:20],
        "n_mismatches": len(diffs),
        "stats_new_pipeline": got_stats,
        "method": ("SPY replayed through core/basket_backtest.run_instrument and "
                   "compared trade-by-trade against the stored trades of the "
                   "registered C2 verdict run"),
    }
