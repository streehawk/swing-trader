"""core/c2_live_log.py — the trade log, K2 slippage, K3 review. ANALYSIS ONLY.

`live_trading_protocol.md` §7 calls the log "the real product of the
quarter", and it is right: the P&L on $3,000 over one quarter is noise, but
the answer to "does reality match the backtest" is not. This module owns the
log's schema, the K2 slippage computation and the K3 review table.

WHY SLIPPAGE IS MEASURED AGAINST THE ACTUAL OPEN. K2 trips when realized
slippage exceeds 0.25%/side over 10 trades, on the reasoning that the
backtest assumed 2-5bps and 25bps means reality is 5-10x worse. That
inference only holds if the number measures EXECUTION. Measured against the
prior close it would mostly measure the overnight gap — routinely 0.5% on a
sector ETF — and K2 would fire in a month for reasons no broker controls.
So: the evening report sizes the order off the last close, and `reconcile`
fills in the true reference once the session's official open is known.

Nothing here connects to a broker. Fills are typed in by the owner.
"""
from __future__ import annotations

import csv
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

from core.c2_live import (
    BACKTEST_EXPECTATION, K2_SLIPPAGE_LIMIT_PCT, K2_WINDOW_TRADES,
    K3_MIN_TRADES_FOR_VERDICT, K3_STOP_MIN_TRADES, K3_STOP_WIN_RATE,
    K1_EQUITY_FLOOR,
)

log = logging.getLogger("c2_live_log")

# §7, verbatim. The order is the protocol's; do not reorder — the file is
# the tax record too.
LOG_COLUMNS = [
    "date", "ticker", "side", "signal_date", "cum_rsi_at_signal",
    "modeled_open_price", "actual_fill_price", "slippage_pct", "shares",
    "dollar_amount", "pl_on_exit", "days_held", "notes",
]

SKIP_COLUMNS = ["date", "ticker", "signal_date", "cum_rsi_at_signal",
                "reason", "notes"]

EQUITY_COLUMNS = ["date", "equity", "notes"]


def ensure_log(path, columns=LOG_COLUMNS) -> Path:
    """Create the CSV with headers if absent. Never overwrites."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    if not p.exists():
        with p.open("w", newline="") as f:
            csv.writer(f).writerow(columns)
    return p


def read_log(path) -> pd.DataFrame:
    p = Path(path)
    if not p.exists():
        return pd.DataFrame(columns=LOG_COLUMNS)
    df = pd.read_csv(p)
    for c in ("modeled_open_price", "actual_fill_price", "slippage_pct",
              "shares", "dollar_amount", "pl_on_exit", "days_held",
              "cum_rsi_at_signal"):
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def append_row(path, row: dict, columns=LOG_COLUMNS) -> None:
    ensure_log(path, columns)
    with Path(path).open("a", newline="") as f:
        csv.writer(f).writerow([row.get(c, "") for c in columns])


def slippage_pct(fill: float, reference: float, side: str) -> Optional[float]:
    """Signed cost in percent: positive means the fill was WORSE than the open.

    Buying above the open costs you; selling below the open costs you. A
    single unsigned magnitude would let a systematically favourable fill
    cancel a systematically bad one, which is exactly the pattern K2 needs
    to be able to see.
    """
    if not reference or not np.isfinite(reference) or reference <= 0:
        return None
    if not np.isfinite(fill):
        return None
    d = (fill - reference) / reference * 100.0
    s = side.strip().lower()
    if s.startswith("entry") or s == "buy":
        return round(d, 4)
    if s.startswith("exit") or s == "sell":
        return round(-d, 4)
    return None


def official_open(bars: pd.DataFrame, date: str) -> Optional[float]:
    """The session's actual official open — the K2 reference price."""
    idx = bars.index
    target = pd.Timestamp(date)
    pos = int(np.searchsorted(idx.values, np.datetime64(target)))
    if pos >= len(idx) or idx[pos] != target:
        return None
    v = float(pd.to_numeric(bars["open"], errors="coerce").iloc[pos])
    return v if np.isfinite(v) else None


def reconcile(log_path, bars_by_ticker: dict[str, pd.DataFrame]) -> dict:
    """Fill in `modeled_open_price` and `slippage_pct` for rows missing them.

    Only touches rows where the fill is recorded but the reference is not.
    A row the owner has already reconciled is never rewritten — the log is
    evidence, and evidence that silently changes is not evidence.
    """
    df = read_log(log_path)
    if df.empty:
        return {"rows": 0, "updated": 0, "unresolved": [], "note": "log is empty"}

    updated, unresolved = 0, []
    for i, row in df.iterrows():
        have_fill = pd.notna(row.get("actual_fill_price"))
        need_ref = pd.isna(row.get("modeled_open_price"))
        need_slip = pd.isna(row.get("slippage_pct"))
        if not have_fill or not (need_ref or need_slip):
            continue
        t = str(row.get("ticker", "")).upper()
        b = bars_by_ticker.get(t)
        if b is None:
            unresolved.append(f"{t} {row.get('date')}: no price data")
            continue
        op = official_open(b, str(row.get("date")))
        if op is None:
            unresolved.append(
                f"{t} {row.get('date')}: no bar for that date "
                f"(not a session yet, or a typo)")
            continue
        if need_ref:
            df.at[i, "modeled_open_price"] = round(op, 4)
        ref = float(df.at[i, "modeled_open_price"])
        sl = slippage_pct(float(row["actual_fill_price"]), ref,
                          str(row.get("side", "")))
        if sl is not None:
            df.at[i, "slippage_pct"] = sl
        updated += 1

    if updated:
        df.to_csv(log_path, index=False)
    return {"rows": len(df), "updated": updated, "unresolved": unresolved}


def k2_status(log_path, window: int = K2_WINDOW_TRADES) -> dict:
    """§6 K2: mean realized slippage per side over the last `window` fills."""
    df = read_log(log_path)
    s = df["slippage_pct"].dropna() if "slippage_pct" in df.columns else pd.Series(dtype=float)
    recent = s.tail(window)
    mean = float(recent.mean()) if len(recent) else None
    return {
        "window": window,
        "fills_with_slippage": int(len(s)),
        "fills_in_window": int(len(recent)),
        "mean_slippage_pct": round(mean, 4) if mean is not None else None,
        "limit_pct": K2_SLIPPAGE_LIMIT_PCT,
        "breached": bool(mean is not None and len(recent) >= window
                         and mean > K2_SLIPPAGE_LIMIT_PCT),
        "enough_data": bool(len(recent) >= window),
        "worst_fill_pct": round(float(recent.max()), 4) if len(recent) else None,
    }


def k3_review(log_path, account_equity: float,
              start_date: Optional[str] = None) -> dict:
    """§6 K3: the scheduled 3-month comparison against backtest expectation.

    Verdict rules are the protocol's, applied literally:
      EXTEND   fewer than 8 trades — sample too small
      STOP     K1 fired, or win rate < 55% on 12+ trades, or K2 unresolved
      CONTINUE otherwise, with equity above the floor
    """
    df = read_log(log_path)
    exits = df[df["side"].astype(str).str.lower().str.startswith("exit")] \
        if "side" in df.columns and not df.empty else pd.DataFrame()
    entries = df[df["side"].astype(str).str.lower().str.startswith("entry")] \
        if "side" in df.columns and not df.empty else pd.DataFrame()

    pl = exits["pl_on_exit"].dropna() if "pl_on_exit" in exits.columns else pd.Series(dtype=float)
    held = exits["days_held"].dropna() if "days_held" in exits.columns else pd.Series(dtype=float)
    n = int(len(pl))
    wins = int((pl > 0).sum()) if n else 0
    win_rate = (wins / n) if n else None

    # % per trade needs the capital actually committed, not the account.
    pct = []
    for _, e in exits.iterrows():
        amt = e.get("dollar_amount")
        p = e.get("pl_on_exit")
        if pd.notna(amt) and pd.notna(p) and float(amt) > 0:
            pct.append(float(p) / float(amt) * 100.0)
    avg_pct = float(np.mean(pct)) if pct else None

    k2 = k2_status(log_path)
    k1_fired = account_equity < K1_EQUITY_FLOOR

    if n < K3_MIN_TRADES_FOR_VERDICT:
        verdict = "EXTEND"
        why = (f"only {n} completed trades (need >= "
               f"{K3_MIN_TRADES_FOR_VERDICT}) — sample too small, run one "
               f"more quarter")
    elif k1_fired:
        verdict, why = "STOP", f"K1 fired: equity ${account_equity:,.2f} < ${K1_EQUITY_FLOOR:,.0f}"
    elif (win_rate is not None and n >= K3_STOP_MIN_TRADES
          and win_rate < K3_STOP_WIN_RATE):
        verdict, why = "STOP", (f"win rate {win_rate:.1%} < {K3_STOP_WIN_RATE:.0%} "
                                f"on {n} trades")
    elif k2["breached"]:
        verdict, why = "STOP", (f"K2 unresolved: mean slippage "
                                f"{k2['mean_slippage_pct']}% > {K2_SLIPPAGE_LIMIT_PCT}%")
    else:
        verdict, why = "CONTINUE", "metrics in line and equity above the floor"

    return {
        "start_date": start_date,
        "completed_trades": n,
        "entries_logged": int(len(entries)),
        "win_rate": round(win_rate, 4) if win_rate is not None else None,
        "avg_pct_per_trade": round(avg_pct, 4) if avg_pct is not None else None,
        "avg_hold_days": round(float(held.mean()), 2) if len(held) else None,
        "total_pl": round(float(pl.sum()), 2) if n else 0.0,
        "account_equity": account_equity,
        "k1_fired": k1_fired,
        "k2": k2,
        "expectation": BACKTEST_EXPECTATION,
        "verdict": verdict,
        "verdict_reason": why,
    }


def render_review(r: dict) -> str:
    L, A = [], None
    L = []
    A = L.append
    A("=" * 72)
    A("K3 REVIEW — live vs backtest expectation")
    A("=" * 72)
    A(f"{'Metric':<26}{'Backtest expectation':<24}{'Live'}")
    A("-" * 72)
    e = r["expectation"]
    wr = f"{r['win_rate']:.1%}" if r["win_rate"] is not None else "n/a"
    ap = f"{r['avg_pct_per_trade']:+.3f}%" if r["avg_pct_per_trade"] is not None else "n/a"
    ah = f"{r['avg_hold_days']}" if r["avg_hold_days"] is not None else "n/a"
    sl = (f"{r['k2']['mean_slippage_pct']:+.3f}%"
          if r["k2"]["mean_slippage_pct"] is not None else "n/a")
    A(f"{'Win rate':<26}{e['win_rate']:<24}{wr}")
    A(f"{'Avg %/trade':<26}{e['avg_pct_per_trade']:<24}{ap}")
    A(f"{'Avg hold':<26}{e['avg_hold_days']:<24}{ah}")
    A(f"{'Trades taken':<26}{e['trades_per_quarter']:<24}{r['completed_trades']}")
    A(f"{'Avg slippage/side':<26}{e['avg_slippage_per_side_pct']:<24}{sl}")
    A("-" * 72)
    A(f"Total P/L        : ${r['total_pl']:+,.2f}")
    A(f"Account equity   : ${r['account_equity']:,.2f} "
      f"(K1 floor ${K1_EQUITY_FLOOR:,.0f})")
    k2 = r["k2"]
    A(f"K2 slippage      : {k2['fills_in_window']}/{k2['window']} fills in window"
      f"{'' if k2['enough_data'] else ' — not enough data to judge yet'}")
    A("")
    A(f"VERDICT: {r['verdict']}  — {r['verdict_reason']}")
    A("=" * 72)
    return "\n".join(L)
