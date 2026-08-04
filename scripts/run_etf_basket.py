#!/usr/bin/env python3
"""scripts/run_etf_basket.py — C-Series Addendum 1 runner.

    --step 1   A1-1 coverage recap + the SPY CONTROL + look-ahead checks.
               Runs NO basket backtest. This is the gate the addendum puts
               in front of every result: if the pipeline cannot reproduce
               the registered SPY verdict exactly, nothing downstream of it
               means anything.

    --step 2   A1-3 per-instrument runs + A1-4 portfolio sims + G1/G2/G3.
               Requires Step 1 to have passed.

Usage:
    python scripts/run_etf_basket.py --step 1
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

from core.basket_backtest import (  # noqa: E402
    BASKET_COSTS_BPS, BASKET_TICKERS, BROAD_INDEX, CAPPED_SIZING, MAX_CONCURRENT,
    POST_BOOK_START, SECTORS, UNCAPPED_SIZING, VARIANT, bars_in_window,
    build_signals, buy_and_hold_daily, evaluate_gates, load_basket, load_single,
    pooled_stats, run_basket, signal_overlap_matrix, simulate_portfolio,
    spy_control,
)
from core.c_series_backtest import (  # noqa: E402
    STRESS_WINDOWS, compute_c_stats, confirm_no_lookahead, equity_curve_stats,
    split_by_date, window_slice, worst_trades,
)

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "data" / "validation_cache"
BASKET_PARQUET = CACHE / "etf_basket.parquet"
SPY_PARQUET = CACHE / "spy_extended.parquet"
REGISTERED_C2 = CACHE / "c_series_l1_l2_l3_l4_2026-08-02.json"

# A look-ahead check that compares zero trades proves nothing — that exact
# vacuous pass is what Phase 1B's check returned, and it is why H1 added a
# floor. Same floor here.
MIN_LOOKAHEAD_TRADES = 2


def load_registered_c2_trades(path=REGISTERED_C2) -> list[dict]:
    """The stored trades of the run that produced the published C2 verdict."""
    d = json.loads(Path(path).read_text())
    return d["results"]["L2"]["C2"]["trades"]


def step1(args) -> int:
    t0 = time.time()
    print("=" * 96)
    print("C-SERIES ADDENDUM 1 — STEP 1  (A1-1 data + A1-2 pipeline)")
    print("=" * 96)
    print(f"Variant    : {VARIANT} only — C1 and C3 are out of scope")
    print(f"Basket     : {len(BASKET_TICKERS)} ETFs, SPY EXCLUDED (control only)")
    print(f"Costs      : {BASKET_COSTS_BPS}")
    print(f"Window     : POST-BOOK {POST_BOOK_START} -> present")
    print("NO BASKET BACKTEST IS RUN IN STEP 1.")
    print()

    bars = load_basket(args.basket)
    meta_path = Path(args.basket).with_suffix(".meta.json")
    meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}

    # ── A1-1 recap ───────────────────────────────────────────────────
    print("-" * 96)
    print("A1-1 — DATA COVERAGE")
    print("-" * 96)
    print(f"{'Ticker':<7}{'First bar':<12}{'Last bar':<12}{'Bars':>8}"
          f"{'First usable':>14}{'POST-BOOK bars':>16}{'Gaps>5':>8}")
    cov = {c["ticker"]: c for c in meta.get("coverage", [])}
    for t in BASKET_TICKERS:
        b = bars.get(t)
        if b is None:
            print(f"{t:<7}MISSING FROM CACHE")
            continue
        c = cov.get(t, {})
        print(f"{t:<7}{str(b.index[0].date()):<12}{str(b.index[-1].date()):<12}"
              f"{len(b):>8,}{str(c.get('first_usable_bar','?')):>14}"
              f"{bars_in_window(b, POST_BOOK_START):>16,}"
              f"{len(c.get('gaps_gt_5_sessions', [])):>8}")

    # ── A1-2 control ─────────────────────────────────────────────────
    print()
    print("-" * 96)
    print("A1-2 — SPY CONTROL  (must reproduce the registered C2 verdict exactly)")
    print("-" * 96)
    spy = load_single(args.spy, "SPY")
    registered = load_registered_c2_trades(args.registered)
    ctrl = spy_control(spy, registered, start=POST_BOOK_START)

    print(f"registered trades : {ctrl['n_trades_registered']}")
    print(f"new-pipeline      : {ctrl['n_trades_new']}")
    print(f"fields compared   : {', '.join(ctrl['fields_compared'])}")
    print(f"mismatches        : {ctrl['n_mismatches']}")
    for m in ctrl["mismatches"]:
        print(f"   !! {m}")
    s = ctrl["stats_new_pipeline"]
    if s.get("n_trades"):
        print(f"new-pipeline stats: {s['n_trades']} trades, "
              f"{s['win_rate']:.1%} win, {s['avg_pct']:+.4f}%/trade, "
              f"PF {s['profit_factor']:.3f}, t {s['t_stat']:+.2f}")
    print(f"CONTROL           : {'PASS' if ctrl['passed'] else 'FAIL'}")

    # ── A1-2 look-ahead, per instrument ──────────────────────────────
    print()
    print("-" * 96)
    print("A1-2 — LOOK-AHEAD REPLAY (per instrument, must be non-vacuous)")
    print("-" * 96)
    print(f"{'Ticker':<8}{'Passed':>8}{'Compared':>10}{'Vacuous?':>10}  Note")
    la = {}
    for t in list(BASKET_TICKERS) + ["SPY"]:
        b = spy if t == "SPY" else bars.get(t)
        if b is None:
            continue
        r = confirm_no_lookahead(b, VARIANT, fill_mode="close")
        r["non_vacuous"] = r["trades_compared"] >= MIN_LOOKAHEAD_TRADES
        la[t] = r
        print(f"{t:<8}{str(r['passed']):>8}{r['trades_compared']:>10}"
              f"{('NO' if r['non_vacuous'] else 'YES — INVALID'):>10}  "
              f"{r.get('note') or ''}")
        for m in r["mismatches"][:3]:
            print(f"         !! {m}")

    all_pass = all(r["passed"] for r in la.values())
    all_nonvac = all(r["non_vacuous"] for r in la.values())

    # Both fill modes for the control instrument: next_open is the G3 bar,
    # so its fill path has to be verified too, not assumed.
    la_no = confirm_no_lookahead(spy, VARIANT, fill_mode="next_open")
    print(f"{'SPY(no)':<8}{str(la_no['passed']):>8}{la_no['trades_compared']:>10}"
          f"{('NO' if la_no['trades_compared'] >= MIN_LOOKAHEAD_TRADES else 'YES'):>10}"
          f"  next_open fill mode")

    print()
    print("=" * 96)
    ok = ctrl["passed"] and all_pass and all_nonvac and la_no["passed"]
    print(f"STEP 1 VERDICT    : {'READY FOR STEP 2' if ok else 'BLOCKED'}")
    print(f"  control          : {'PASS' if ctrl['passed'] else 'FAIL'}")
    print(f"  look-ahead       : {'PASS' if all_pass else 'FAIL'} "
          f"({len(la)} instruments)")
    print(f"  non-vacuous      : {'PASS' if all_nonvac else 'FAIL'} "
          f"(min {MIN_LOOKAHEAD_TRADES} trades compared)")
    print(f"elapsed           : {time.time() - t0:.1f}s")
    print("=" * 96)

    out = {
        "step": 1,
        "run_date": str(pd.Timestamp.now(tz="UTC")),
        "variant": VARIANT,
        "basket": list(BASKET_TICKERS),
        "costs_bps": BASKET_COSTS_BPS,
        "post_book_start": POST_BOOK_START,
        "coverage": meta.get("coverage", []),
        "spy_drift_check": meta.get("spy_drift_check"),
        "control": {k: v for k, v in ctrl.items() if k != "stats_new_pipeline"},
        "control_stats": ctrl["stats_new_pipeline"],
        "lookahead": la,
        "lookahead_next_open_spy": la_no,
        "verdict": "READY FOR STEP 2" if ok else "BLOCKED",
    }
    if args.out:
        Path(args.out).write_text(json.dumps(out, indent=2, default=str))
        print(f"-> {args.out}")
    return 0 if ok else 1


def step2(args) -> int:
    """A1-3 + A1-4 + G1/G2/G3.

    The control is re-run FIRST and printed on the first line. The user's
    approval was conditional on it: if SPY does not reproduce the registered
    C2 verdict on the overlapping window, this function stops without
    touching the basket.
    """
    t0 = time.time()
    bars = load_basket(args.basket)
    spy = load_single(args.spy, "SPY")

    # ── The precondition, stated first ───────────────────────────────
    registered = load_registered_c2_trades(args.registered)
    ctrl = spy_control(spy, registered, start=POST_BOOK_START)
    cs = ctrl["stats_new_pipeline"]
    print("=" * 112)
    if ctrl["passed"]:
        print(f"SPY CONTROL: PASS — the new pipeline reproduced the registered C2 "
              f"verdict EXACTLY on the overlapping window "
              f"({ctrl['n_trades_new']}/{ctrl['n_trades_registered']} trades, "
              f"identical entries, exits and per-trade results; 0 mismatches "
              f"across {len(ctrl['fields_compared'])} fields per trade).")
    else:
        print(f"SPY CONTROL: FAIL — {ctrl['n_mismatches']} mismatches. "
              f"STOPPING WITHOUT RUNNING THE BASKET.")
        for m in ctrl["mismatches"]:
            print(f"   !! {m}")
        return 1
    print("=" * 112)
    print("C-SERIES ADDENDUM 1 — STEP 2  (A1-3 per-instrument, A1-4 portfolio)")
    print("=" * 112)
    print(f"Variant  : {VARIANT}   Window: POST-BOOK {POST_BOOK_START} -> present")
    print(f"Costs    : 2bps/side QQQ/DIA/IWM, 5bps/side the nine sector SPDRs")
    print(f"Sizing   : P-capped = {CAPPED_SIZING} (REGISTERED, judged by G2); "
          f"P-uncapped = {UNCAPPED_SIZING}")
    print(f"Slots    : max {MAX_CONCURRENT}, held through the exit bar inclusive, "
          f"freed next session")
    print(f"Priority : most oversold first (lowest cumulative RSI)")
    print(f"Control  : {cs['n_trades']} trades, {cs['win_rate']:.1%} win, "
          f"{cs['avg_pct']:+.4f}%/trade, PF {cs['profit_factor']:.3f}")

    # ── A1-3 ─────────────────────────────────────────────────────────
    results, per_stats, pooled = {}, {}, {}
    for fill in ("close", "next_open"):
        by_t = run_basket(bars, fill_mode=fill, costs_on=True,
                          start=POST_BOOK_START)
        results[fill] = by_t
        print(f"\n{'-' * 112}")
        print(f"A1-3 — PER INSTRUMENT · fill={fill} · costs ON · POST-BOOK")
        print(f"{'-' * 112}")
        print("| Ticker | #T | Win% | Avg% | AvgWin% | AvgLoss% | Worst% | PF | "
              "MaxDD% | Hold | %Days | t | Valve |")
        print("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
        stats = {}
        for t in BASKET_TICKERS:
            tr = by_t.get(t, [])
            s = compute_c_stats(tr, total_bars=bars_in_window(bars[t], POST_BOOK_START))
            eq = equity_curve_stats(tr)
            stats[t] = s
            if not s.get("n_trades"):
                print(f"| {t} | 0 | — | — | — | — | — | — | — | — | — | — | — |")
                continue
            print(f"| {t} | {s['n_trades']} | {s['win_rate']:.1%} | "
                  f"**{s['avg_pct']:+.3f}** | {s['avg_win_pct']:+.2f} | "
                  f"{s['avg_loss_pct']:+.2f} | {s['worst_trade_pct']:+.2f} | "
                  f"{s['profit_factor']:.3f} | {eq['max_drawdown_pct']:.1f} | "
                  f"{s['avg_hold_days']:.1f} | {s['pct_days_in_market']}% | "
                  f"{s['t_stat']:+.2f} | "
                  f"{s['exit_breakdown'].get('TIME_VALVE', 0)} |")
        per_stats[fill] = stats
        p = pooled_stats(by_t)
        pooled[fill] = p
        pos = sum(1 for s in stats.values() if s.get("n_trades") and s["avg_pct"] > 0)
        print(f"\nPOOLED ({fill}): {p['n_trades']} trades, {p['win_rate']:.1%} win, "
              f"**{p['avg_pct']:+.4f}%/trade**, PF {p['profit_factor']:.3f}, "
              f"t {p['t_stat']:+.2f} | positive instruments: {pos}/12")

    # Pooled 70/30 split by DATE (G1's sign-holding bar).
    all_close = [t for ts in results["close"].values() for t in ts]
    train, test, cut = split_by_date(all_close, 0.70)
    tr_s, te_s = compute_c_stats(train), compute_c_stats(test)
    print(f"\nPOOLED 70/30 SPLIT (by date, cut {cut}):")
    print(f"  train {tr_s['n_trades']} trades {tr_s['avg_pct']:+.4f}%  "
          f"PF {tr_s['profit_factor']:.3f} | "
          f"test {te_s['n_trades']} trades {te_s['avg_pct']:+.4f}%  "
          f"PF {te_s['profit_factor']:.3f}")

    # ── A1-4 ─────────────────────────────────────────────────────────
    sigs = build_signals(results["close"], bars)
    sims = {
        "P-uncapped": simulate_portfolio(sigs, bars, name="P-uncapped",
                                         max_concurrent=None,
                                         sizing=UNCAPPED_SIZING),
        "P-capped":   simulate_portfolio(sigs, bars, name="P-capped",
                                         max_concurrent=MAX_CONCURRENT,
                                         sizing=CAPPED_SIZING),
    }
    diag = {
        "P-capped (equal_open diagnostic)": simulate_portfolio(
            sigs, bars, name="P-capped-eo", max_concurrent=MAX_CONCURRENT,
            sizing="equal_open"),
        "P-capped (random priority diagnostic)": simulate_portfolio(
            sigs, bars, name="P-capped-rnd", max_concurrent=MAX_CONCURRENT,
            sizing=CAPPED_SIZING, priority="random"),
    }
    spy_bh = buy_and_hold_daily(spy, POST_BOOK_START)

    print(f"\n{'-' * 112}")
    print("A1-4 — PORTFOLIO SIMULATION · close fill · costs ON · daily marked")
    print(f"{'-' * 112}")
    print("| Sim | Sizing | CAGR% | MaxDD% | MAR | Trades/yr | %Days ≥1 open | "
          "Signals | Taken | Refused | %Refused |")
    print("|---|---|---|---|---|---|---|---|---|---|---|")
    for nm, r in list(sims.items()) + list(diag.items()):
        s = r.stats
        print(f"| {nm} | {s['sizing']} | {s['cagr_pct']:+.2f} | "
              f"{s['max_drawdown_pct']:.2f} | {s['mar']} | "
              f"{s['trades_per_year']:.2f} | {s['pct_days_position_open']}% | "
              f"{s['n_signals']} | {s['n_taken']} | {s['n_refused']} | "
              f"{s['pct_signals_refused']}% |")
    print(f"| SPY buy-and-hold | 100% | {spy_bh['cagr_pct']:+.2f} | "
          f"{spy_bh['max_drawdown_pct']:.2f} | {spy_bh['mar']} | — | 100% | "
          f"— | — | — | — |")

    print("\nCAPACITY AUTOPSY (the H1 question, repeated on purpose):")
    for nm, r in list(sims.items()) + list(diag.items()):
        s = r.stats
        print(f"  {nm:42s} taken {s['n_taken']:4d} @ {s['taken_avg_pct']}%  |  "
              f"refused {s['n_refused']:4d} @ {s['refused_avg_pct']}%")
    print("\nCLUSTERING:")
    for nm, r in sims.items():
        print(f"  {nm}: {json.dumps(r.cluster)}")

    # ── Attribution ──────────────────────────────────────────────────
    print(f"\n{'-' * 112}")
    print("ATTRIBUTION")
    print(f"{'-' * 112}")
    cs_ = per_stats["close"]
    for label, group in (("BROAD (QQQ/DIA/IWM)", BROAD_INDEX),
                         ("SECTORS (9 SPDRs)", SECTORS)):
        tr = [t for g in group for t in results["close"].get(g, [])]
        s = compute_c_stats(tr)
        pos = sum(1 for g in group if cs_[g].get("n_trades") and cs_[g]["avg_pct"] > 0)
        print(f"  {label:22s} {s['n_trades']:4d} trades  {s['avg_pct']:+.4f}%/trade  "
              f"PF {s['profit_factor']:.3f}  t {s['t_stat']:+.2f}  "
              f"positive {pos}/{len(group)}")

    ov = signal_overlap_matrix(results["close"])
    print(f"\n  Signal-date overlap: mean pairwise Jaccard "
          f"{ov['mean_pairwise_jaccard']}, median {ov['median_pairwise_jaccard']}")
    print(f"    most overlapping : {ov['most_overlapping'][:3]}")
    print(f"    least overlapping: {ov['least_overlapping'][:3]}")

    print("\n  CRISIS WINDOWS (pooled, close fill, costs ON):")
    for nm, (a, b) in STRESS_WINDOWS.items():
        w = window_slice(all_close, a, b)
        s = compute_c_stats(w)
        if s.get("n_trades"):
            print(f"    {nm:18s} {s['n_trades']:3d} trades  {s['avg_pct']:+.4f}%  "
                  f"PF {s['profit_factor']:.3f}  worst {s['worst_trade_pct']:+.2f}%")
        else:
            print(f"    {nm:18s} no trades")

    print("\n  WORST 10 POOLED TRADES (by return, with MAE):")
    idx_of = {id(t): tk for tk, ts in results["close"].items() for t in ts}
    for w in sorted(all_close, key=lambda t: t.pct_return)[:10]:
        print(f"    {idx_of.get(id(w), '?'):4s} {w.entry_date} -> {w.exit_date}  "
              f"{w.pct_return:+7.2f}%  MAE {w.mae_pct:+7.2f}%  "
              f"{w.bars_held:3d} bars  {w.exit_reason}")

    # ── Gates ────────────────────────────────────────────────────────
    gates = evaluate_gates(
        per_stats["close"], pooled["close"], pooled["next_open"],
        tr_s, te_s, sims["P-capped"].stats, spy_bh,
    )
    print(f"\n{'=' * 112}")
    print("GATES")
    print(f"{'=' * 112}")
    for key in ("G1", "G2", "G3"):
        blk = gates[key]
        print(f"\n{key}: {blk['verdict']}")
        for c in blk["checks"]:
            print(f"   [{'PASS' if c['pass'] else 'FAIL'}] {c['detail']}")
        if blk.get("spec_gap"):
            print(f"   !! SPEC GAP: {blk['spec_gap']}")
        if key == "G1":
            print(f"   positive: {', '.join(blk['positive_instruments']) or 'none'}")
            print(f"   negative: {', '.join(blk['negative_instruments']) or 'none'}")

    print(f"\nelapsed {time.time() - t0:.1f}s")

    if args.out:
        out = {
            "step": 2, "run_date": str(pd.Timestamp.now(tz="UTC")),
            "variant": VARIANT, "post_book_start": POST_BOOK_START,
            "control": {k: v for k, v in ctrl.items() if k != "stats_new_pipeline"},
            "control_stats": cs,
            "sizing": {"capped": CAPPED_SIZING, "uncapped": UNCAPPED_SIZING},
            "per_instrument": per_stats,
            "pooled": pooled,
            "pooled_split": {"cut": cut, "train": tr_s, "test": te_s},
            "portfolio": {nm: {"stats": r.stats, "cluster": r.cluster}
                          for nm, r in list(sims.items()) + list(diag.items())},
            "spy_buy_hold": spy_bh,
            "overlap": ov,
            "gates": gates,
            "trades": {fill: {t: [x.to_dict() for x in ts]
                              for t, ts in results[fill].items()}
                       for fill in results},
        }
        Path(args.out).write_text(json.dumps(out, indent=2, default=str))
        print(f"-> {args.out}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--step", type=int, default=1, choices=(1, 2))
    ap.add_argument("--basket", default=str(BASKET_PARQUET))
    ap.add_argument("--spy", default=str(SPY_PARQUET))
    ap.add_argument("--registered", default=str(REGISTERED_C2))
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    return step1(args) if args.step == 1 else step2(args)


if __name__ == "__main__":
    raise SystemExit(main())
