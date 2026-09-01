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
import re
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
    """The session's actual official open — the K2 reference price.

    Prefers `open_raw` (unadjusted). Slippage compares two prices and they
    must be the same kind: the fill is what you actually paid, so the
    reference has to be the unadjusted opening print. Dividend-adjusted
    bars are retroactively rescaled every time a distribution goes ex —
    measured on this basket, six-month-old opens shift by up to -1.36% —
    which would fabricate slippage far beyond the 0.25% K2 limit and pause
    trading over a dividend.

    Falls back to `open` for caches written before `open_raw` existed. That
    is correct for any fill reconciled before the next ex-dividend date,
    which is every fill so far, and is why prompt reconciling matters.
    """
    idx = bars.index
    target = pd.Timestamp(date)
    pos = int(np.searchsorted(idx.values, np.datetime64(target)))
    if pos >= len(idx) or idx[pos] != target:
        return None
    for col in ("open_raw", "open"):
        if col in bars.columns:
            v = float(pd.to_numeric(bars[col], errors="coerce").iloc[pos])
            if np.isfinite(v):
                return v
    return None


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


def trading_days_between(bars: pd.DataFrame, start: str, end: str) -> Optional[int]:
    """Sessions from `start` to `end`, the backtest's `bars_held` convention.

    Trading days, never calendar days. A weekend inflates a 3-session hold
    to 5, and `days_held` is what the K3 review compares against the
    backtest's ~4-day expectation — so a calendar count would make the live
    system look slower than it is and could swing a CONTINUE/STOP call.
    """
    idx = bars.index
    a, b = pd.Timestamp(start), pd.Timestamp(end)
    pa = int(np.searchsorted(idx.values, np.datetime64(a)))
    pb = int(np.searchsorted(idx.values, np.datetime64(b)))
    if pa >= len(idx) or pb >= len(idx) or idx[pa] != a or idx[pb] != b:
        return None
    return pb - pa


def validate_log(log_path, positions=None, bars_by_ticker=None) -> list[dict]:
    """Cross-check the hand-maintained log. Returns a list of problems.

    Exists because every error found so far has been a transcription slip
    that the system could not see: shares swapped between two rows, a
    pre-filled slippage reference that blocked reconcile, an empty pending
    row eating a slot. None of them raise; all of them corrupt the record
    the quarter is being judged on.

    Severity: "ERROR" = the number is wrong. "WARN" = suspicious but
    possibly deliberate.
    """
    problems: list[dict] = []

    def add(sev, row, msg):
        problems.append({"severity": sev, "row": row, "message": msg})

    raw = Path(log_path)
    if not raw.exists():
        return [{"severity": "ERROR", "row": None,
                 "message": f"{log_path} not found"}]
    df = read_log(log_path)
    if df.empty:
        return []

    # ── Dates must be unambiguous ISO ────────────────────────────────
    # Excel rewrites 2026-08-25 as 8/25/26 on save. That parses today only
    # because every day-of-month so far exceeds 12; the first trade on the
    # 9th of a month becomes silently ambiguous between US and EU order.
    for i, v in df["date"].items():
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(v).strip()):
            add("ERROR", i + 2,
                f"date {v!r} is not ISO YYYY-MM-DD — a spreadsheet has "
                f"reformatted it; ambiguous once a day-of-month is <= 12")
    for i, v in df["signal_date"].items():
        if pd.notna(v) and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(v).strip()):
            add("ERROR", i + 2, f"signal_date {v!r} is not ISO YYYY-MM-DD")

    # ── Per-row arithmetic ───────────────────────────────────────────
    for i, r in df.iterrows():
        row, tkr = i + 2, str(r.get("ticker", "?"))
        side = str(r.get("side", "")).strip().lower()
        px, sh = r.get("actual_fill_price"), r.get("shares")

        if side not in ("entry", "exit"):
            add("ERROR", row, f"side {r.get('side')!r} must be entry or exit")
        if pd.notna(px) and pd.notna(sh) and pd.notna(r.get("dollar_amount")):
            want = float(px) * float(sh)
            if abs(want - float(r["dollar_amount"])) > 0.05:
                add("ERROR", row,
                    f"{tkr} dollar_amount {r['dollar_amount']} != "
                    f"{sh} x {px} = {want:.2f}")
        if pd.notna(px) and pd.isna(r.get("modeled_open_price")):
            add("WARN", row, f"{tkr} not reconciled yet — run ./c2 --reconcile")
        if side == "exit" and pd.isna(r.get("pl_on_exit")):
            add("ERROR", row, f"{tkr} exit row has no pl_on_exit")
        if side == "exit" and pd.isna(r.get("days_held")):
            add("ERROR", row, f"{tkr} exit row has no days_held")

    # ── Entry/exit pairing ───────────────────────────────────────────
    # An exit sells exactly what the entry bought. A mismatch here is the
    # 2026-08-27 error: two exits filed on the same day with each other's
    # share counts.
    for tkr, g in df.groupby("ticker"):
        ent = g[g["side"].astype(str).str.lower() == "entry"]
        ext = g[g["side"].astype(str).str.lower() == "exit"]
        for k in range(min(len(ent), len(ext))):
            e, x = ent.iloc[k], ext.iloc[k]
            if pd.notna(e["shares"]) and pd.notna(x["shares"]) and \
                    abs(float(e["shares"]) - float(x["shares"])) > 1e-6:
                add("ERROR", None,
                    f"{tkr}: exit sells {x['shares']} shares but the entry "
                    f"bought {e['shares']} — share counts do not match")
            if pd.notna(x.get("pl_on_exit")) and pd.notna(e.get("actual_fill_price")) \
                    and pd.notna(x.get("actual_fill_price")) and pd.notna(x["shares"]):
                want = (float(x["actual_fill_price"])
                        - float(e["actual_fill_price"])) * float(x["shares"])
                if abs(want - float(x["pl_on_exit"])) > 0.05:
                    add("ERROR", None,
                        f"{tkr}: pl_on_exit {x['pl_on_exit']} != "
                        f"({x['actual_fill_price']} - {e['actual_fill_price']}) "
                        f"x {x['shares']} = {want:.2f}")
            # days_held must be TRADING days, matching the backtest.
            if bars_by_ticker is not None and pd.notna(x.get("days_held")):
                b = bars_by_ticker.get(str(tkr).upper())
                if b is not None:
                    n = trading_days_between(b, str(e["date"]), str(x["date"]))
                    if n is not None and int(n) != int(x["days_held"]):
                        add("ERROR", None,
                            f"{tkr}: days_held {int(x['days_held'])} but "
                            f"{e['date']} -> {x['date']} is {n} TRADING day(s) "
                            f"— a weekend was counted; K3 compares this to the "
                            f"backtest's ~4-day hold")

    # ── Cross-check against the positions file ───────────────────────
    if positions is not None:
        logged_open = set()
        for tkr, g in df.groupby("ticker"):
            n_e = (g["side"].astype(str).str.lower() == "entry").sum()
            n_x = (g["side"].astype(str).str.lower() == "exit").sum()
            if n_e > n_x:
                logged_open.add(str(tkr))
        held = {p.ticker for p in positions.open}
        for t in sorted(logged_open - held):
            add("ERROR", None,
                f"{t}: the log shows an unclosed entry but it is not in "
                f"positions.json `open` — either the exit row is missing or "
                f"the position was deleted too early")
        for t in sorted(held - logged_open):
            add("ERROR", None,
                f"{t}: held in positions.json but the log has no open entry "
                f"for it")
    return problems


def render_validation(problems: list[dict]) -> str:
    if not problems:
        return "VALIDATION: clean — positions.json and trade_log.csv agree."
    L = ["VALIDATION FOUND %d PROBLEM(S)" % len(problems), "-" * 66]
    for p in problems:
        where = f"line {p['row']}" if p["row"] else "cross-row"
        L.append(f"  [{p['severity']:5}] {where:11} {p['message']}")
    return "\n".join(L)


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
