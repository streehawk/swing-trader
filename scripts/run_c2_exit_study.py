#!/usr/bin/env python3
"""scripts/run_c2_exit_study.py — C-Series Addendum 2, the exit study.

    --step 1   Implement-and-verify. Runs the E0 CONTROL against the
               registered Addendum-1 and C-series results, and reports the
               shared entry census. Runs NO challenger backtest.

    --step 2   E1-E4, the metrics hierarchy, the replacement gate.
               Requires Step 1 to have passed.

The live system is untouched by either step. An exit change may only reach
live through `live_trading_protocol.md` §10, and only if the replacement
gate passes.
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
    BASKET_TICKERS, CAPPED_SIZING, MAX_CONCURRENT, POST_BOOK_START,
    bars_in_window, build_signals, buy_and_hold_daily, cost_model, load_basket,
    load_single, simulate_portfolio,
)
from core.c_series_backtest import compute_c_stats, replay_variant  # noqa: E402
from core.exit_study import (  # noqa: E402
    aggregate_per_day,
    evaluate_replacement_gate, hold_length_decomposition, matched_entry_subset,
    per_day_stats, pick_winner, restrict_to, split_on, variant_summary,
    window_split_date,
)
from strategies.c2_exits import (  # noqa: E402
    EXIT_IDS, EXITS, INCUMBENT, MA_FAMILY, RSI_FAMILY, exit_variant,
    raw_entry_signals,
)

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "data" / "validation_cache"
BASKET_PARQUET = CACHE / "etf_basket.parquet"
SPY_PARQUET = CACHE / "spy_extended.parquet"
REGISTERED_BASKET = CACHE / "etf_basket_step2_2026-08-03.json"
REGISTERED_SPY = CACHE / "c_series_l1_l2_l3_l4_2026-08-02.json"

STUDY_TICKERS = ("SPY",) + BASKET_TICKERS          # the validated 13

# Compared field-by-field, not just on summary statistics: two different
# trade sets can produce the same mean.
CONTROL_FIELDS = ("entry_date", "exit_date", "entry_index", "exit_index",
                  "bars_held", "entry_price", "exit_price", "pct_return",
                  "mae_pct", "mfe_pct", "exit_reason")

log = logging.getLogger("c2_exit_study")


def load_registered() -> dict[str, dict[str, list[dict]]]:
    """The registered E0 trades, per fill mode, per instrument.

    SPY comes from the C-series artefact (SPY was excluded from the basket)
    and the 12 ETFs from Addendum 1. Both are the STORED trades of the runs
    that produced the published verdicts — comparing against a fresh
    recomputation would let a shared bug agree with itself.
    """
    basket = json.loads(REGISTERED_BASKET.read_text())["trades"]
    spy = json.loads(REGISTERED_SPY.read_text())["results"]["L2"]["C2"]["trades"]
    out: dict[str, dict[str, list[dict]]] = {}
    for fill in ("close", "next_open"):
        out[fill] = dict(basket.get(fill, {}))
    out["close"]["SPY"] = spy          # SPY next-open is not in the artefact
    return out


def compare(got, want: list[dict], label: str) -> list[str]:
    diffs: list[str] = []
    if len(got) != len(want):
        diffs.append(f"{label}: trade count {len(got)} != registered {len(want)}")
    for k, (a, b) in enumerate(zip(got, want)):
        d = a.to_dict()
        for f in CONTROL_FIELDS:
            if f in b and d[f] != b[f]:
                diffs.append(f"{label} trade {k} ({b.get('entry_date')}): "
                             f"{f} {d[f]!r} != {b[f]!r}")
    return diffs


def step1(args) -> int:
    t0 = time.time()
    bars = load_basket(args.basket)
    bars["SPY"] = load_single(args.spy, "SPY")

    print("=" * 100)
    print("C-SERIES ADDENDUM 2 — STEP 1  (exit variants implemented + E0 control)")
    print("=" * 100)
    print(f"Entry      : FROZEN C2 (close > SMA200, CumRSI(2) < 35)")
    print(f"Instruments: {len(STUDY_TICKERS)} — SPY + the 12-ETF basket")
    print(f"Window     : POST-BOOK {POST_BOOK_START} -> present")
    print(f"Costs      : 2bps/side broad, 5bps/side sectors")
    print("NO CHALLENGER BACKTEST IS RUN IN STEP 1.")
    print()

    # ── The registered contenders ────────────────────────────────────
    print("-" * 100)
    print("REGISTERED EXIT CONTENDERS")
    print("-" * 100)
    print(f"| {'ID':<4}| {'Rule':<16}| {'Family':<7}| {'Book rank':<10}| Status")
    print(f"|{'-'*5}|{'-'*17}|{'-'*8}|{'-'*11}|{'-'*32}")
    for eid in EXIT_IDS:
        s = EXITS[eid]
        print(f"| {s.exit_id:<4}| {s.rule:<16}| {s.family:<7}| "
              f"{str(s.book_rank):<10}| {s.status}")
    print()

    # ── The E0 control ───────────────────────────────────────────────
    print("-" * 100)
    print("E0 CONTROL — must reproduce the registered results EXACTLY")
    print("-" * 100)
    registered = load_registered()
    v0 = exit_variant(INCUMBENT)
    all_diffs: list[str] = []
    rows = []
    for fill in ("close", "next_open"):
        for t in STUDY_TICKERS:
            want = registered[fill].get(t)
            if want is None:
                rows.append((fill, t, None, None, "no registered artefact"))
                continue
            got = replay_variant(bars[t], v0, fill_mode=fill,
                                 costs=cost_model(t, costs_on=True),
                                 start=POST_BOOK_START)
            d = compare(got, want, f"{t}/{fill}")
            all_diffs += d
            rows.append((fill, t, len(got), len(want), "OK" if not d else
                         f"{len(d)} MISMATCH"))

    print(f"| {'Fill':<10}| {'Ticker':<7}| {'New':>5}| {'Registered':>11}| Result")
    print(f"|{'-'*11}|{'-'*8}|{'-'*6}|{'-'*12}|{'-'*22}")
    for fill, t, n, w, res in rows:
        print(f"| {fill:<10}| {t:<7}| {str(n):>5}| {str(w):>11}| {res}")

    compared = sum(1 for _, _, n, w, r in rows if r == "OK")
    control_ok = not all_diffs
    print()
    print(f"instrument-runs compared : {compared}")
    print(f"fields per trade         : {len(CONTROL_FIELDS)}")
    print(f"mismatches               : {len(all_diffs)}")
    for m in all_diffs[:15]:
        print(f"   !! {m}")
    print(f"CONTROL                  : {'PASS' if control_ok else 'FAIL'}")

    # ── Shared entry census ──────────────────────────────────────────
    print()
    print("-" * 100)
    print("SHARED ENTRY CENSUS (the addendum's 'shared entry count')")
    print("-" * 100)
    census, total_raw, total_taken = {}, 0, 0
    for t in STUDY_TICKERS:
        b = bars[t]
        closes = pd.to_numeric(b["close"], errors="coerce").to_numpy(dtype=float)
        idx = b.index
        lo = int(np.searchsorted(idx.values,
                                 np.datetime64(pd.Timestamp(POST_BOOK_START))))
        raw = [i for i in raw_entry_signals(closes) if i >= lo]
        taken = len(registered["close"].get(t, []))
        census[t] = {"raw_signals": len(raw), "e0_taken": taken,
                     "skipped_while_held": len(raw) - taken}
        total_raw += len(raw)
        total_taken += taken

    print(f"| {'Ticker':<7}| {'Raw signals':>12}| {'E0 took':>9}| "
          f"{'Skipped (in position)':>22}|")
    print(f"|{'-'*8}|{'-'*13}|{'-'*10}|{'-'*23}|")
    for t in STUDY_TICKERS:
        c = census[t]
        print(f"| {t:<7}| {c['raw_signals']:>12}| {c['e0_taken']:>9}| "
              f"{c['skipped_while_held']:>22}|")
    print(f"|{'-'*8}|{'-'*13}|{'-'*10}|{'-'*23}|")
    print(f"| {'TOTAL':<7}| {total_raw:>12}| {total_taken:>9}| "
          f"{total_raw - total_taken:>22}|")

    print()
    print("The RAW SIGNAL count is identical across all five variants by")
    print("construction — the entry is frozen and evaluated on closes alone.")
    print("The TAKEN set need not be: replay holds one position per")
    print("instrument and resumes scanning only after the exit, so a slower")
    print("exit CAN be mid-trade when a signal fires and skip it. Whether it")
    print("actually does is measured below rather than assumed.")

    # ── Is the addendum's attribution premise true? ──────────────────
    # "Identical entries across all five variants by construction — every
    # difference in results is attributable to the exit alone." That claim
    # is what Step 2's whole attribution rests on, so it is checked BEFORE
    # Step 2 is approved rather than after.
    #
    # ENTRY DATES ONLY. No return, hold, or gate metric is computed or
    # printed here; this is a structural check, not a challenger result.
    print()
    print("-" * 100)
    print("ATTRIBUTION PREMISE CHECK — do the challengers take the same entries?")
    print("-" * 100)
    taken_sets: dict[str, dict[str, set]] = {}
    for eid in EXIT_IDS:
        v = exit_variant(eid)
        taken_sets[eid] = {}
        for t in STUDY_TICKERS:
            trades = replay_variant(bars[t], v, fill_mode="close",
                                    costs=cost_model(t, costs_on=True),
                                    start=POST_BOOK_START)
            taken_sets[eid][t] = {x.entry_date for x in trades}

    base = taken_sets[INCUMBENT]
    print(f"| {'Exit':<5}| {'Entries taken':>14}| {'vs E0: same':>12}| "
          f"{'only E0':>9}| {'only this':>10}| {'instruments differing':>22}|")
    print(f"|{'-'*6}|{'-'*15}|{'-'*13}|{'-'*10}|{'-'*11}|{'-'*23}|")
    premise = {}
    for eid in EXIT_IDS:
        tot = sum(len(s) for s in taken_sets[eid].values())
        same = only_a = only_b = 0
        differing = 0
        for t in STUDY_TICKERS:
            a, b = base[t], taken_sets[eid][t]
            same += len(a & b)
            only_a += len(a - b)
            only_b += len(b - a)
            if a != b:
                differing += 1
        premise[eid] = {"taken": tot, "shared_with_e0": same,
                        "only_e0": only_a, "only_this": only_b,
                        "instruments_differing": differing}
        print(f"| {eid:<5}| {tot:>14}| {same:>12}| {only_a:>9}| {only_b:>10}| "
              f"{differing:>22}|")

    identical = all(p["instruments_differing"] == 0 for p in premise.values())
    print()
    if identical:
        print("PREMISE HOLDS: all five variants take the SAME entries on every")
        print("instrument. Every difference in Step 2's results is therefore")
        print("attributable to the exit alone, exactly as the addendum states.")
    else:
        print("!! PREMISE DOES NOT HOLD EXACTLY. Some challengers take a")
        print("!! different entry set than E0, so part of any performance")
        print("!! difference comes from trading different opportunities, not")
        print("!! purely from the exit. Magnitudes are in the table above.")

    ok = control_ok
    print()
    print("=" * 100)
    print(f"STEP 1 VERDICT : {'READY FOR STEP 2' if ok else 'BLOCKED'}")
    print(f"  E0 control    : {'PASS' if control_ok else 'FAIL'}")
    print(f"  shared signals: {total_raw} raw C2 entry signals across "
          f"{len(STUDY_TICKERS)} instruments")
    print(f"elapsed        : {time.time() - t0:.1f}s")
    print("=" * 100)

    if args.out:
        Path(args.out).write_text(json.dumps({
            "step": 1,
            "run_date": str(pd.Timestamp.now(tz="UTC")),
            "instruments": list(STUDY_TICKERS),
            "post_book_start": POST_BOOK_START,
            "exits": {k: {"rule": v.rule, "status": v.status,
                          "family": v.family, "book_rank": v.book_rank}
                      for k, v in EXITS.items()},
            "control": {"passed": control_ok,
                        "instrument_runs_compared": compared,
                        "fields_per_trade": list(CONTROL_FIELDS),
                        "mismatches": all_diffs[:50],
                        "n_mismatches": len(all_diffs)},
            "entry_census": census,
            "totals": {"raw_signals": total_raw, "e0_taken": total_taken},
            "attribution_premise": {"identical_entries": identical,
                                    "per_exit": premise},
            "verdict": "READY FOR STEP 2" if ok else "BLOCKED",
        }, indent=2, default=str))
        print(f"-> {args.out}")
    return 0 if ok else 1


def _portfolio(bars, by_ticker, *, start, end=None):
    """The registered P-capped fixed-slots sim over the study's 13."""
    sigs = build_signals(by_ticker, bars)
    if not sigs:
        return {"cagr_pct": None, "max_drawdown_pct": None, "mar": None}
    r = simulate_portfolio(sigs, bars, name="P-capped",
                           max_concurrent=MAX_CONCURRENT,
                           sizing=CAPPED_SIZING, start=start, end=end)
    return r.stats


def step2(args) -> int:
    t0 = time.time()
    bars = load_basket(args.basket)
    bars["SPY"] = load_single(args.spy, "SPY")
    bars = {t: bars[t] for t in STUDY_TICKERS}

    # ── Precondition: the control, restated ──────────────────────────
    registered = load_registered()
    v0 = exit_variant(INCUMBENT)
    diffs = []
    for fill in ("close", "next_open"):
        for t in STUDY_TICKERS:
            want = registered[fill].get(t)
            if want is None:
                continue
            got = replay_variant(bars[t], v0, fill_mode=fill,
                                 costs=cost_model(t, costs_on=True),
                                 start=POST_BOOK_START)
            diffs += compare(got, want, f"{t}/{fill}")
    print("=" * 112)
    if diffs:
        print(f"E0 CONTROL FAILED ({len(diffs)} mismatches) — "
              f"STOPPING WITHOUT JUDGING CHALLENGERS.")
        for m in diffs[:10]:
            print(f"   !! {m}")
        return 1
    print("E0 CONTROL: PASS — the incumbent reproduces the registered "
          "Addendum-1 / C-series trades exactly.")
    print("=" * 112)
    print("C-SERIES ADDENDUM 2 — STEP 2  (E1-E4 judged against E0)")
    print("=" * 112)
    print(f"Entry    : FROZEN C2. Instruments: {len(STUDY_TICKERS)}. "
          f"Window: POST-BOOK {POST_BOOK_START} -> present. Costs ON.")
    print(f"Primary  : return per trading day held; portfolio MAR "
          f"(P-capped, {CAPPED_SIZING})")
    print(f"Gate     : challenger needs >= 1.10x E0 on BOTH primaries, in "
          f"BOTH splits, on >= 8/13 instruments, and not execution-fragile.")
    print("LIVE SYSTEM UNTOUCHED regardless of outcome.")

    cut = window_split_date(bars, POST_BOOK_START)
    print(f"Shared 70/30 split date (from the calendar, same for all "
          f"variants): {cut}")

    # ── Run every variant, both fill modes ───────────────────────────
    trades: dict[str, dict[str, dict]] = {"close": {}, "next_open": {}}
    for fill in ("close", "next_open"):
        for eid in EXIT_IDS:
            v = exit_variant(eid)
            trades[fill][eid] = {
                t: replay_variant(bars[t], v, fill_mode=fill,
                                  costs=cost_model(t, costs_on=True),
                                  start=POST_BOOK_START)
                for t in STUDY_TICKERS}

    pooled = {f: {e: [x for ts in trades[f][e].values() for x in ts]
                  for e in EXIT_IDS} for f in trades}
    total_bars = sum(bars_in_window(bars[t], POST_BOOK_START)
                     for t in STUDY_TICKERS)

    # ── A. Pooled table, close fill (the judged window) ──────────────
    print(f"\n{'-' * 112}")
    print("A. POOLED RESULTS — close fill, costs ON, POST-BOOK")
    print(f"{'-' * 112}")
    print("| Exit | Rule            | #T   | Win%  | Avg%/trade | **Per-day%** | "
          "PF    | Hold | %Days | Worst%  | Valve | clust-t |")
    print("|---|---|---|---|---|---|---|---|---|---|---|---|")
    summ = {}
    for eid in EXIT_IDS:
        s = variant_summary(pooled["close"][eid], total_bars=total_bars)
        summ[eid] = s
        print(f"| {eid} | {EXITS[eid].rule:<15} | {s['n_trades']} | "
              f"{s['win_rate']:.1%} | {s['avg_pct']:+.4f} | "
              f"**{s['per_day_pct']:+.5f}** | {s['profit_factor']:.3f} | "
              f"{s['avg_hold_days']:.2f} | "
              f"{s['days_in_market'] / total_bars * 100:.1f}% | "
              f"{s['worst_trade_pct']:+.2f} | {s['valve_firings']} | "
              f"{s['clustered_t']:+.2f} |")

    # ── B. Portfolio sims ────────────────────────────────────────────
    print(f"\n{'-' * 112}")
    print(f"B. PORTFOLIO — P-capped, {CAPPED_SIZING}, {MAX_CONCURRENT} slots, "
          f"daily marked (PRIMARY)")
    print(f"{'-' * 112}")
    print("| Exit | CAGR%  | MaxDD% | **MAR**  | Trades/yr | %Days ≥1 open | "
          "%Signals refused |")
    print("|---|---|---|---|---|---|---|")
    port, port_tr, port_te = {}, {}, {}
    for eid in EXIT_IDS:
        port[eid] = _portfolio(bars, trades["close"][eid], start=POST_BOOK_START)
        port_tr[eid] = _portfolio(bars, trades["close"][eid],
                                  start=POST_BOOK_START, end=cut)
        port_te[eid] = _portfolio(bars, trades["close"][eid], start=cut)
        p = port[eid]
        print(f"| {eid} | {p['cagr_pct']:+.2f} | {p['max_drawdown_pct']:.2f} | "
              f"**{p['mar']}** | {p['trades_per_year']:.1f} | "
              f"{p['pct_days_position_open']}% | "
              f"{p['pct_signals_refused']}% |")
    spy_bh = buy_and_hold_daily(bars["SPY"], POST_BOOK_START)
    print(f"| SPY B&H | {spy_bh['cagr_pct']:+.2f} | "
          f"{spy_bh['max_drawdown_pct']:.2f} | {spy_bh['mar']} | — | 100% | — |")

    # ── C. Splits ────────────────────────────────────────────────────
    print(f"\n{'-' * 112}")
    print(f"C. 70/30 SPLIT (shared cut {cut})")
    print(f"{'-' * 112}")
    print("| Exit | train #T | train per-day% | train MAR | test #T | "
          "test per-day% | test MAR |")
    print("|---|---|---|---|---|---|---|")
    splits = {}
    for eid in EXIT_IDS:
        tr, te = split_on(pooled["close"][eid], cut)
        a, b = per_day_stats(tr), per_day_stats(te)
        splits[eid] = {"train": a, "test": b}
        print(f"| {eid} | {a['n']} | {a['per_day_pct']:+.5f} | "
              f"{port_tr[eid]['mar']} | {b['n']} | {b['per_day_pct']:+.5f} | "
              f"{port_te[eid]['mar']} |")

    # ── D. Per instrument, per-day ───────────────────────────────────
    print(f"\n{'-' * 112}")
    print("D. PER-DAY RETURN BY INSTRUMENT (bold = beats E0)")
    print(f"{'-' * 112}")
    per_inst = {eid: {t: per_day_stats(trades["close"][eid][t])["per_day_pct"]
                      for t in STUDY_TICKERS} for eid in EXIT_IDS}
    print("| Ticker | " + " | ".join(EXIT_IDS) + " |")
    print("|---" * (len(EXIT_IDS) + 1) + "|")
    for t in STUDY_TICKERS:
        cells = []
        for eid in EXIT_IDS:
            v, base = per_inst[eid][t], per_inst["E0"][t]
            mark = "**" if (eid != "E0" and v is not None and base is not None
                            and v > base) else ""
            cells.append(f"{mark}{v:+.5f}{mark}" if v is not None else "—")
        print(f"| {t} | " + " | ".join(cells) + " |")
    beat_counts = {eid: sum(1 for t in STUDY_TICKERS
                            if per_inst[eid][t] is not None
                            and per_inst["E0"][t] is not None
                            and per_inst[eid][t] > per_inst["E0"][t])
                   for eid in EXIT_IDS}
    print(f"| **beats E0** | " + " | ".join(f"{beat_counts[e]}/13"
                                            for e in EXIT_IDS) + " |")

    # ── E. Next open (fragility) ─────────────────────────────────────
    print(f"\n{'-' * 112}")
    print("E. NEXT-OPEN FILL (execution fragility)")
    print(f"{'-' * 112}")
    print("| Exit | #T | Avg%/trade | Per-day% | PF | Hold |")
    print("|---|---|---|---|---|---|")
    nsumm = {}
    for eid in EXIT_IDS:
        s = variant_summary(pooled["next_open"][eid])
        nsumm[eid] = s
        print(f"| {eid} | {s['n_trades']} | {s['avg_pct']:+.4f} | "
              f"{s['per_day_pct']:+.5f} | {s['profit_factor']:.3f} | "
              f"{s['avg_hold_days']:.2f} |")

    # ── F. THE GATE ──────────────────────────────────────────────────
    def pack(eid):
        return {
            "per_day": summ[eid]["per_day_pct"],
            "mar": port[eid]["mar"],
            "train_per_day": splits[eid]["train"]["per_day_pct"],
            "test_per_day": splits[eid]["test"]["per_day_pct"],
            "train_mar": port_tr[eid]["mar"],
            "test_mar": port_te[eid]["mar"],
            "next_open_per_day": nsumm[eid]["per_day_pct"],
            "next_open_pf": nsumm[eid]["profit_factor"],
            "instruments_per_day": per_inst[eid],
        }

    e0pack = pack(INCUMBENT)
    gates = {eid: evaluate_replacement_gate(eid, e0=e0pack, challenger=pack(eid))
             for eid in EXIT_IDS if eid != INCUMBENT}
    winner = pick_winner(gates, {e: port[e]["mar"] for e in gates},
                         {e: summ[e]["avg_hold_days"] for e in gates})

    print(f"\n{'=' * 112}")
    print("F. REPLACEMENT GATE")
    print(f"{'=' * 112}")
    for eid, g in gates.items():
        print(f"\n{eid} ({EXITS[eid].rule}): {g['verdict']}  "
              f"[{g['n_failed']}/{len(g['checks'])} bars failed]")
        for c in g["checks"]:
            print(f"   [{'PASS' if c['pass'] else 'FAIL'}] {c['detail']}")
    print(f"\nOVERALL: "
          f"{('E0 IS REPLACED BY ' + winner) if winner else 'E0 STANDS'}")

    # ── G. Matched-entry diagnostic (does NOT feed the gate) ─────────
    taken = {eid: {t: {x.entry_date for x in trades["close"][eid][t]}
                   for t in STUDY_TICKERS} for eid in EXIT_IDS}
    matched = matched_entry_subset(taken)
    n_matched = sum(len(v) for v in matched.values())
    print(f"\n{'-' * 112}")
    print(f"G. MATCHED-ENTRY DIAGNOSTIC — {n_matched} entries taken by ALL "
          f"five variants  (LABELLED DIAGNOSTIC, NOT PART OF THE GATE)")
    print(f"{'-' * 112}")
    print("| Exit | #T | Avg%/trade | Per-day% | PF | Win% | Hold |")
    print("|---|---|---|---|---|---|---|")
    matched_summ = {}
    for eid in EXIT_IDS:
        sub = [x for t in STUDY_TICKERS
               for x in restrict_to(trades["close"][eid][t], matched[t])]
        s = variant_summary(sub)
        matched_summ[eid] = s
        print(f"| {eid} | {s['n_trades']} | {s['avg_pct']:+.4f} | "
              f"{s['per_day_pct']:+.5f} | {s['profit_factor']:.3f} | "
              f"{s['win_rate']:.1%} | {s['avg_hold_days']:.2f} |")

    # ── H. Attribution ───────────────────────────────────────────────
    print(f"\n{'-' * 112}")
    print("H. ATTRIBUTION")
    print(f"{'-' * 112}")
    order = sorted(EXIT_IDS, key=lambda e: -(summ[e]["per_day_pct"] or -9))
    book = sorted(EXIT_IDS, key=lambda e: EXITS[e].book_rank)
    print(f"  book in-sample ranking : {' > '.join(book)}")
    print(f"  out-of-sample, per-day : {' > '.join(order)}")
    print(f"  out-of-sample, avg%    : "
          f"{' > '.join(sorted(EXIT_IDS, key=lambda e: -summ[e]['avg_pct']))}")

    for label, fam in (("RSI family (E0-E2)", RSI_FAMILY),
                       ("MA family  (E3-E4)", MA_FAMILY)):
        tr = [x for e in fam for x in pooled["close"][e]]
        s = variant_summary(tr)
        print(f"  {label}: pooled per-day {s['per_day_pct']:+.5f}%  "
              f"avg {s['avg_pct']:+.4f}%  PF {s['profit_factor']:.3f}  "
              f"hold {s['avg_hold_days']:.2f}")

    print("\n  Aggregate per-day rate (ratio of sums) — cross-check on the")
    print("  registered mean-of-ratios primary metric:")
    agg = {e: aggregate_per_day(pooled["close"][e]) for e in EXIT_IDS}
    print("    " + "  ".join(f"{e} {agg[e]:+.5f}" for e in EXIT_IDS))
    print(f"    ranking by aggregate rate : "
          f"{' > '.join(sorted(EXIT_IDS, key=lambda e: -agg[e]))}")

    print("\n  Hold-length decomposition of the avg %/trade gap vs E0")
    print("  (exact split: gap = exposure effect + rate effect):")
    decomp = {}
    for eid in EXIT_IDS:
        if eid == INCUMBENT:
            continue
        d = hold_length_decomposition(pooled["close"][INCUMBENT],
                                      pooled["close"][eid])
        decomp[eid] = d
        print(f"    {eid}: gap {d['actual_gap_pct']:+.4f}pp = "
              f"exposure {d['exposure_effect_pct']:+.4f}pp + "
              f"rate {d['rate_effect_pct']:+.4f}pp   "
              f"({d['pct_of_gap_from_hold']}% is exposure; "
              f"identity residual {d['identity_residual']:+.6f})")

    print("\n  Valve firings (25-day) and END_OF_DATA exits by variant:")
    for eid in EXIT_IDS:
        print(f"    {eid}: valve {summ[eid]['valve_firings']:3d}  "
              f"end-of-data {summ[eid]['end_of_data_exits']:2d}  "
              f"max hold {max((x.bars_held for x in pooled['close'][eid]), default=0)}")

    print("\n  Worst 10 pooled trades by variant (MAE in brackets):")
    for eid in EXIT_IDS:
        w = sorted(pooled["close"][eid], key=lambda t: t.pct_return)[:10]
        print(f"    {eid}: " + "  ".join(f"{t.pct_return:+.1f}[{t.mae_pct:+.1f}]"
                                         for t in w))

    print(f"\nelapsed {time.time() - t0:.1f}s")

    if args.out:
        Path(args.out).write_text(json.dumps({
            "step": 2, "run_date": str(pd.Timestamp.now(tz="UTC")),
            "instruments": list(STUDY_TICKERS), "split_cut": cut,
            "control_passed": True,
            "pooled_close": summ, "pooled_next_open": nsumm,
            "portfolio": port, "portfolio_train": port_tr,
            "portfolio_test": port_te, "splits": splits,
            "per_instrument_per_day": per_inst, "beats_e0": beat_counts,
            "gate": gates, "winner": winner,
            "matched_entry": {"n_entries": n_matched, "stats": matched_summ},
            "hold_decomposition": decomp, "spy_buy_hold": spy_bh,
        }, indent=2, default=str))
        print(f"-> {args.out}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--step", type=int, default=1, choices=(1, 2))
    ap.add_argument("--basket", default=str(BASKET_PARQUET))
    ap.add_argument("--spy", default=str(SPY_PARQUET))
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    return step1(args) if args.step == 1 else step2(args)


if __name__ == "__main__":
    raise SystemExit(main())
