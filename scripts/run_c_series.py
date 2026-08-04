#!/usr/bin/env python3
"""scripts/run_c_series.py — the C-series validation ladder.

Registered in `connors_rsi2_spec.md`:

    L0  BOOK (1993-2007)        close      costs off
    L1  POST-BOOK (2008-now)    close      costs off
    L2  POST-BOOK               close      costs on (2bps/side)   <- THE GATE
    L3  POST-BOOK               next open  costs on
    L4  FULL                    close      costs on

L0 is an IMPLEMENTATION CHECK, not evidence. The book's 1993-2007 results
are in-sample — the authors developed the rules on that data — so
reproducing them proves the harness is faithful and nothing else. The
spec is explicit about what to do if L0 disagrees by more than 10
percentage points: "suspect the RSI implementation or fill logic BEFORE
suspecting the book — debug the harness, don't touch the rules."

Usage:
    python scripts/run_c_series.py --runs L0          # Step 1
    python scripts/run_c_series.py --runs L1,L2,L3,L4 # Step 2
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.c_series_backtest import (  # noqa: E402
    C_GATE, FREE, STRESS_WINDOWS, CostModelPct, buy_and_hold_stats,
    compute_c_stats, confirm_no_lookahead, equity_curve_stats,
    evaluate_c_gate, replay_variant, split_by_date, trade_overlap,
    window_slice, worst_trades,
)
from strategies.rsi2 import TREND_SMA, VARIANTS  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "data" / "validation_cache"
SPY = CACHE / "spy_extended.parquet"

# Registered analysis windows.
BOOK_END = "2007-12-31"
POST_START = "2008-01-01"

LADDER = {
    "L0": dict(window="BOOK",      fill="close",     costs=False),
    "L1": dict(window="POST-BOOK", fill="close",     costs=False),
    "L2": dict(window="POST-BOOK", fill="close",     costs=True),
    "L3": dict(window="POST-BOOK", fill="next_open", costs=True),
    "L4": dict(window="FULL",      fill="close",     costs=True),
}


def load_spy(path=SPY) -> pd.DataFrame:
    df = pd.read_parquet(path)
    df["date"] = pd.to_datetime(df["date"])
    return df.sort_values("date").set_index("date")[
        ["open", "high", "low", "close", "volume"]]


def window_bounds(bars: pd.DataFrame, name: str) -> tuple[str, str | None]:
    """FULL starts at the first bar with a defined SMA200 — before that no
    variant can fire at all, so counting those bars as 'in the window'
    would only distort the %-days-in-market figure."""
    first_usable = str(bars.index[TREND_SMA - 1].date())
    if name == "BOOK":
        return first_usable, BOOK_END
    if name == "POST-BOOK":
        return POST_START, None
    return first_usable, None


HEADER = ("| Variant | #Trades | Win% | Avg% | AvgWin% | AvgLoss% | Worst% | "
          "PF | MaxDD% | Hold | %Days in mkt |")
SEP = "|---|---|---|---|---|---|---|---|---|---|---|"


def fmt(name: str, s: dict, eq: dict, bars_in_window: int) -> str:
    if not s.get("n_trades"):
        return f"| {name} | 0 | — | — | — | — | — | — | — | — | — |"
    return (
        f"| {name} | {s['n_trades']} | {s['win_rate']:.1%} | "
        f"**{s['avg_pct']:+.3f}** | {s['avg_win_pct']:+.2f} | "
        f"{s['avg_loss_pct']:+.2f} | {s['worst_trade_pct']:+.2f} | "
        f"{s['profit_factor']:.3f} | {eq.get('max_drawdown_pct', float('nan')):.1f} | "
        f"{s['avg_hold_days']:.1f} | "
        f"{s['days_in_market']/bars_in_window*100:.1f}% |"
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--spy", default=str(SPY))
    ap.add_argument("--runs", default="L0")
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    t0 = time.time()

    bars = load_spy(args.spy)
    runs = [r.strip().upper() for r in args.runs.split(",") if r.strip()]
    for r in runs:
        if r not in LADDER:
            raise SystemExit(f"unknown run {r!r} — choose from {', '.join(LADDER)}")

    print("=" * 78)
    print("C-SERIES — CONNORS/ALVAREZ RSI-2 ON SPY")
    print("=" * 78)
    print(f"Instrument : SPY only — single continuously-listed ETF, so NO "
          f"survivorship caveat")
    print(f"Bars       : {len(bars):,}  ({bars.index[0].date()} -> "
          f"{bars.index[-1].date()})")
    print(f"First usable bar (SMA200 warm-up): "
          f"{bars.index[TREND_SMA - 1].date()}")
    print(f"Windows    : BOOK {bars.index[TREND_SMA-1].date()}..{BOOK_END} · "
          f"POST-BOOK {POST_START}..{bars.index[-1].date()}")
    print("Variants   : " + " · ".join(f"{k} ({v.description})"
                                       for k, v in VARIANTS.items()))
    print()

    # ── C-0c precondition: close-fill may only be used if this passes ──
    print("Verifying no look-ahead (close-fill, required by C-0c)...")
    la_all = {}
    for name in VARIANTS:
        la = confirm_no_lookahead(bars, name, fill_mode="close")
        la_all[name] = la
        note = f"  [{la['note']}]" if la.get("note") else ""
        print(f"  {name}: {'PASS' if la['passed'] else 'FAIL'} "
              f"({la['trades_compared']} trades compared){note}")
        for m in la["mismatches"]:
            print(f"    ! {m}")
    if not all(v["passed"] for v in la_all.values()):
        print("\n!! LOOK-AHEAD DETECTED — close-fill is not legitimate. Stopping.")
        return 1
    print()

    results: dict = {}
    for run in runs:
        cfg = LADDER[run]
        start, end = window_bounds(bars, cfg["window"])
        costs = CostModelPct() if cfg["costs"] else FREE
        lo = int(np.searchsorted(bars.index.values,
                                 np.datetime64(pd.Timestamp(start))))
        hi = (len(bars) if end is None
              else int(np.searchsorted(bars.index.values,
                                       np.datetime64(pd.Timestamp(end)),
                                       side="right")))
        bars_in_window = max(1, hi - lo)

        print("=" * 78)
        print(f"{run} — {cfg['window']} · fill={cfg['fill']} · "
              f"costs={'2bps/side' if cfg['costs'] else 'OFF'}")
        print(f"     window {start} -> {end or str(bars.index[-1].date())} "
              f"({bars_in_window:,} bars)")
        print("=" * 78)
        print(HEADER)
        print(SEP)

        run_out = {}
        per_variant_trades = {}
        for name, v in VARIANTS.items():
            trades = replay_variant(bars, v, fill_mode=cfg["fill"], costs=costs,
                                    start=start, end=end)
            trades = window_slice(trades, start, end)
            per_variant_trades[name] = trades
            s = compute_c_stats(trades, total_bars=bars_in_window)
            eq = equity_curve_stats(trades, allocation=1.0)
            eq10 = equity_curve_stats(trades, allocation=0.10)
            print(fmt(name, s, eq, bars_in_window))
            run_out[name] = {
                "stats": s, "equity_100pct": eq, "equity_10pct": eq10,
                "worst_trades": worst_trades(trades, 10),
                "trades": [t.to_dict() for t in trades],
            }
        print()

        if run == "L0":
            print("L0 IMPLEMENTATION CHECK vs the book's published win rates")
            print("| Variant | Book Win% | Ours | Diff (pp) | Within ±10pp? |")
            print("|---|---|---|---|---|")
            for name, v in VARIANTS.items():
                ours = run_out[name]["stats"].get("win_rate")
                if ours is None:
                    print(f"| {name} | {v.book_win_rate:.1f}% | no trades | — | NO |")
                    continue
                ours_pct = ours * 100
                diff = ours_pct - v.book_win_rate
                ok = abs(diff) <= 10.0
                print(f"| {name} | {v.book_win_rate:.1f}% | {ours_pct:.1f}% | "
                      f"{diff:+.1f} | {'YES' if ok else '**NO**'} |")
                run_out[name]["book_check"] = {
                    "book_win_rate": v.book_win_rate,
                    "our_win_rate": round(ours_pct, 2),
                    "diff_pp": round(diff, 2), "within_10pp": bool(ok),
                }
            print()

        # Exit-reason and valve reporting — mandatory (no stops).
        for name in VARIANTS:
            s = run_out[name]["stats"]
            if s.get("n_trades"):
                eb = s["exit_breakdown"]
                valve = eb.get("TIME_VALVE", 0)
                print(f"  {name}: exits {eb} · 25-day valve fired "
                      f"{valve}x ({valve/s['n_trades']:.1%})")
        print()

        # Sibling overlap — the spec requires the correlation be visible.
        print("  Trade-date overlap (C1-C3 are siblings of one idea, not "
              "three independent hypotheses):")
        for a, b in (("C1", "C2"), ("C1", "C3"), ("C2", "C3")):
            o = trade_overlap(per_variant_trades[a], per_variant_trades[b])
            print(f"    {a} vs {b}: {o['shared_entry_dates']} shared entry dates "
                  f"· Jaccard {o['jaccard']:.3f} "
                  f"({o['pct_of_a_shared']:.1%} of {a}, "
                  f"{o['pct_of_b_shared']:.1%} of {b})")
            run_out.setdefault("_overlap", {})[f"{a}_vs_{b}"] = o
        print()

        bh = buy_and_hold_stats(bars, start, end)
        if bh:
            print(f"  SPY buy-and-hold, same window: "
                  f"total {bh['total_return_pct']:+.1f}% · "
                  f"CAGR {bh['cagr_pct']:+.2f}% · "
                  f"max DD {bh['max_drawdown_pct']:.1f}%")
            run_out["_buy_and_hold"] = bh
        print()

        # ── POST-BOOK extras: split, stress windows, worst trades ────
        if cfg["window"] == "POST-BOOK":
            print("  Train/test split within POST-BOOK (70% of the DATE span; "
                  "the sign of avg %/trade must hold in both halves):")
            print("  | Variant | Cut | Train n | Train avg% | Test n | "
                  "Test avg% | Sign holds |")
            print("  |---|---|---|---|---|---|---|")
            for name in VARIANTS:
                tr_t, te_t, cut = split_by_date(per_variant_trades[name], 0.70)
                s_tr, s_te = compute_c_stats(tr_t), compute_c_stats(te_t)
                a, b = s_tr.get("avg_pct"), s_te.get("avg_pct")
                holds = (a is not None and b is not None
                         and ((a > 0 and b > 0) or (a < 0 and b < 0)))
                print(f"  | {name} | {cut} | {s_tr.get('n_trades',0)} | "
                      f"{a:+.3f}% | {s_te.get('n_trades',0)} | {b:+.3f}% | "
                      f"{'YES' if holds else '**NO**'} |")
                run_out[name]["split"] = {
                    "cut": cut, "train": s_tr, "test": s_te,
                    "sign_holds": bool(holds)}
            print()

            print("  Stress windows (mandatory — this design carries no stop):")
            print("  | Variant | " + " | ".join(STRESS_WINDOWS) + " |")
            print("  |---|" + "---|" * len(STRESS_WINDOWS))
            for name in VARIANTS:
                cells = []
                for label, (ws, we) in STRESS_WINDOWS.items():
                    sub = window_slice(per_variant_trades[name], ws, we)
                    ss = compute_c_stats(sub)
                    eqs = equity_curve_stats(sub)
                    cells.append(
                        f"{ss.get('n_trades',0)}t "
                        f"{ss.get('avg_pct',0):+.2f}%/t "
                        f"tot {eqs.get('total_return_pct',0):+.1f}% "
                        f"DD {eqs.get('max_drawdown_pct',0):.1f}%"
                        if ss.get("n_trades") else "no trades")
                    run_out[name].setdefault("stress", {})[label] = {
                        "stats": ss, "equity": eqs}
                print(f"  | {name} | " + " | ".join(cells) + " |")
            print()

            print("  10 worst trades with max adverse excursion "
                  "(where a no-stop strategy's risk actually lives):")
            for name in VARIANTS:
                wt = run_out[name]["worst_trades"]
                if not wt:
                    continue
                worst_mae = min((x["mae_pct"] for x in wt), default=0.0)
                print(f"    {name}: worst trade {wt[0]['pct']:+.2f}% "
                      f"(MAE {wt[0]['mae_pct']:+.2f}%, {wt[0]['bars']}d, "
                      f"{wt[0]['entry']}) · deepest MAE among them "
                      f"{worst_mae:+.2f}%")
                for x in wt[:5]:
                    print(f"        {x['entry']} -> {x['exit']}  "
                          f"{x['pct']:+7.2f}%  MAE {x['mae_pct']:+7.2f}%  "
                          f"{x['bars']:>3}d  {x['reason']}")
            print()

        results[run] = run_out

    # ── The gate, judged on L2 ───────────────────────────────────────
    if "L2" in results:
        print("=" * 78)
        print("PASS/FAIL GATE — judged on L2 (POST-BOOK, close-fill, costs on)")
        print("=" * 78)
        bh = results["L2"].get("_buy_and_hold", {})
        for name in VARIANTS:
            l2 = results["L2"][name]
            l3 = results.get("L3", {}).get(name, {}).get("stats", {})
            sp = l2.get("split", {})
            verdict = evaluate_c_gate(
                l2["stats"], l2["equity_100pct"],
                sp.get("train", {}), sp.get("test", {}), bh, l3)
            results["L2"][name]["verdict"] = verdict
            print(f"\n{name} — {VARIANTS[name].description}")
            print(f"  VERDICT: {verdict['decision']}")
            for r in verdict["reasons"]:
                print(f"    [{r.split(':')[0]}] {r.split(': ', 1)[1]}")
            for c in verdict["caveats"]:
                print(f"    !! {c}")
        print()

    out = Path(args.out) if args.out else (
        CACHE / f"c_series_{'_'.join(runs).lower()}_"
                f"{pd.Timestamp.today().date()}.json")
    out.write_text(json.dumps({
        "spec": "connors_rsi2_spec.md",
        "run_date": str(pd.Timestamp.today().date()),
        "runs": runs,
        "spy": {"bars": len(bars), "first": str(bars.index[0].date()),
                "last": str(bars.index[-1].date())},
        "lookahead": la_all,
        "results": results,
    }, indent=2, default=str))
    print(f"Elapsed: {time.time() - t0:.0f}s")
    print(f"Full output -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
