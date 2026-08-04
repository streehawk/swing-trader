#!/usr/bin/env python3
"""scripts/run_h1_validation.py — H1 validation ladder (edge-search protocol).

Runs the hypothesis to its verdict and stops. Nothing here chooses a
threshold: every number the verdict depends on is read from
`core.h1_backtest.H1_GATE`, which was copied out of
`edge_search_protocol.md` before the first run.

The ladder, in the protocol's registered order:

    0. universe provenance      PIT or SURVIVOR — decides the ceiling
    1. look-ahead confirmation  empirical, on THIS run's data
    2. cheap run, costs OFF     kill fast; if it is not alive here it
                                cannot become alive by adding costs
    3. costed run               the gate's actual input
    4. 70/30 train/test split   by DATE, both halves must agree in sign
    5. verdict

Usage:
    python scripts/run_h1_validation.py
    python scripts/run_h1_validation.py --limit 40        # smoke test
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.h1_backtest import (  # noqa: E402
    CostModel, H1_GATE, compute_stats, confirm_no_lookahead_h1,
    evaluate_h1_gate, run_backtest_h1, simulate_portfolio, split_trades,
    stats_by_year, universe_provenance,
)

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "data" / "validation_cache"

# Held constant per protocol global rule 3.
RISK_PCT = 0.0075
MAX_CONCURRENT = 2

# Protocol rule 4: ">= 0.05-0.15%/side or 1 tick, whichever larger; use
# the larger bound for anything under $2B cap or under 1M avg volume."
# H1's universe is $5B+ names and the entry gate enforces >= 1M shares/day,
# so the lower band applies. 10bps is mid-band and is the same figure
# every earlier run in this project used, which keeps H1 directly
# comparable to Phase 1's B0. 15bps is reported alongside as a
# robustness figure only — it is NOT the gate's input, and it can only
# make the answer worse, never better.
PRIMARY_SLIPPAGE_BPS = 10.0
STRESS_SLIPPAGE_BPS = 15.0


def _load_universe() -> list[str]:
    data = json.loads((ROOT / "core_universe.json").read_text())
    tickers = [t["ticker"] if isinstance(t, dict) else t for t in data["tickers"]]
    ov = data.get("overrides", {})
    for t in ov.get("force_include", []):
        if t not in tickers:
            tickers.append(t)
    excl = set(ov.get("force_exclude", []))
    return sorted(t for t in tickers if t not in excl)


def _fmt_row(label: str, s: dict) -> str:
    if not s.get("n_trades"):
        return f"| {label} | 0 | — | — | — | — | — | — | — |"
    return (
        f"| {label} | {s['n_trades']} | {s['win_rate']:.1%} | "
        f"{s['avg_win_r']:+.2f} | {s['avg_loss_r']:+.2f} | "
        f"**{s['expectancy_r']:+.3f}** | {s['breakeven_win_rate']:.1%} | "
        f"{s['profit_factor']:.3f} | {s['t_stat']:+.2f} |"
    )


HEADER = ("| Run | #Trades | Win% | AvgWin R | AvgLoss R | Expectancy | "
          "Breakeven Win% | PF | t |")
SEP = "|---|---|---|---|---|---|---|---|---|"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prices", default=str(CACHE / "prices_p1b_final.parquet"))
    ap.add_argument("--limit", type=int, default=0, help="first N tickers (smoke test)")
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    t0 = time.time()

    prices = pd.read_parquet(args.prices)
    prices["date"] = pd.to_datetime(prices["date"])

    universe = _load_universe()
    available = set(prices["ticker"].unique())
    tickers = [t for t in universe if t in available]
    missing = [t for t in universe if t not in available]
    if args.limit:
        tickers = tickers[: args.limit]

    sub = prices[prices["ticker"].isin(tickers)].copy()
    prov = universe_provenance(sub)

    print("=" * 78)
    print("H1 — PANIC MEAN-REVERSION IN QUALITY LARGE CAPS")
    print("=" * 78)
    print(f"Universe   : {prov['universe_type']} — {len(tickers)} of "
          f"{len(universe)} registered tickers, {prov['n_delisted']} delisted")
    if missing:
        print(f"             missing from cache: {', '.join(missing)}")
    if prov.get("warning"):
        print(f"             !! {prov['warning']}")
    print(f"Window     : {prov['dataset_start']} -> {prov['dataset_end']}")
    print(f"Held const : risk {RISK_PCT:.2%}/trade · max {MAX_CONCURRENT} concurrent")
    print("Exits      : SMA5 close exit · 2xATR hard stop · 7-bar time stop "
          "(no BE ratchet, no target — registered)")
    print("Regime gate: OFF (registered, no A/B)")
    print("Earnings   : gate OFF in backtest (no point-in-time calendar) — "
          "the live engine would apply it")
    print()

    print("Verifying no look-ahead...")
    la = confirm_no_lookahead_h1(sub, n_tickers=4)
    note = f"  [{la['note']}]" if la.get("note") else ""
    print(f"  {'PASS' if la['passed'] else 'FAIL'} "
          f"({la['trades_compared']} trades compared){note}")
    if not la["passed"]:
        for m in la["mismatches"]:
            print(f"    ! {m}")
    print()

    # ── Rung 2: cheap run, costs OFF ─────────────────────────────────
    print("Running (costs OFF)...")
    census: dict = {}
    free = CostModel(commission_per_side=0.0, slippage_bps=0.0, tick_size=0.0)
    trades_free = run_backtest_h1(sub, tickers=tickers, costs=free,
                                  time_stop_bars=7, census=census)
    s_free = compute_stats(trades_free)
    print(f"  {s_free.get('n_trades', 0)} trades, "
          f"E={s_free.get('expectancy_r', 0):+.4f}R")

    # ── Rung 3: costed run ───────────────────────────────────────────
    print(f"Running (costs ON, {PRIMARY_SLIPPAGE_BPS:.0f}bps)...")
    costed = CostModel(commission_per_side=0.0,
                       slippage_bps=PRIMARY_SLIPPAGE_BPS, tick_size=0.01)
    trades = run_backtest_h1(sub, tickers=tickers, costs=costed, time_stop_bars=7)
    s_costed = compute_stats(trades)
    print(f"  {s_costed.get('n_trades', 0)} trades, "
          f"E={s_costed.get('expectancy_r', 0):+.4f}R")

    print(f"Running (stress, {STRESS_SLIPPAGE_BPS:.0f}bps — robustness only)...")
    stress = CostModel(commission_per_side=0.0,
                       slippage_bps=STRESS_SLIPPAGE_BPS, tick_size=0.01)
    trades_stress = run_backtest_h1(sub, tickers=tickers, costs=stress, time_stop_bars=7)
    s_stress = compute_stats(trades_stress)
    print(f"  {s_stress.get('n_trades', 0)} trades, "
          f"E={s_stress.get('expectancy_r', 0):+.4f}R")
    print()

    # ── Rung 4: train/test split on the costed run ───────────────────
    train, test, cut = split_trades(trades, 0.70)
    s_train, s_test = compute_stats(train), compute_stats(test)

    portfolio = simulate_portfolio(trades, risk_pct=RISK_PCT,
                                   max_concurrent=MAX_CONCURRENT)
    by_year = stats_by_year(trades)

    # ── Report ───────────────────────────────────────────────────────
    print("=" * 78)
    print("RESULTS")
    print("=" * 78)
    print(HEADER)
    print(SEP)
    print(_fmt_row("costs OFF", s_free))
    print(_fmt_row(f"costs ON ({PRIMARY_SLIPPAGE_BPS:.0f}bps)", s_costed))
    print(_fmt_row(f"stress ({STRESS_SLIPPAGE_BPS:.0f}bps)", s_stress))
    print(_fmt_row(f"train (<= {cut})", s_train))
    print(_fmt_row(f"test (> {cut})", s_test))
    print()

    if by_year:
        print("Per year (costed):")
        print(HEADER.replace("| Run |", "| Year |"))
        print(SEP)
        for y, s in by_year.items():
            print(_fmt_row(y, s))
        print()

    if s_costed.get("n_trades"):
        print(f"Exit breakdown : {s_costed['exit_breakdown']}")
        print(f"Avg bars held  : {s_costed['avg_bars_held']}")
        print(f"Portfolio      : {portfolio['n_taken']} taken, "
              f"{portfolio['n_rejected']} rejected by the "
              f"{MAX_CONCURRENT}-position cap "
              f"({portfolio['rejection_rate']:.1%}) · "
              f"return {portfolio['total_return_pct']:+.1f}% · "
              f"max DD {portfolio['max_drawdown_pct']:.1f}%")
        print()

    total = census.get("bars", 0)
    if total:
        print(f"CENSUS: bars {total:,} | panic signals {census.get('signal', 0)} "
              f"({census.get('signal', 0)/total:.4%}) | "
              f"filled {census.get('filled', 0)} | "
              f"expired unfilled {census.get('unfilled', 0)}")
        for k, v in sorted(census.get("reasons", {}).items(),
                           key=lambda kv: -kv[1]):
            print(f"  {k:<22} {v:>8,}  {v/total:>7.3%}")
        print()

    verdict = evaluate_h1_gate(s_costed, s_train, s_test,
                               provenance=prov, lookahead=la)
    print("=" * 78)
    print(f"VERDICT: {verdict['decision']}")
    print("=" * 78)
    for r in verdict["reasons"]:
        print(f"  [{r.split(':')[0]}] {r.split(': ', 1)[1]}")
    for c in verdict["caveats"]:
        print(f"  !! {c}")
    print()

    out = Path(args.out) if args.out else (
        CACHE / f"h1_validation_{pd.Timestamp.today().date()}.json")
    out.write_text(json.dumps({
        "hypothesis": "H1 — panic mean reversion in quality large caps",
        "run_date": str(pd.Timestamp.today().date()),
        "universe": {"registered": len(universe), "used": len(tickers),
                     "missing_from_cache": missing, "tickers": tickers},
        "provenance": prov,
        "lookahead": la,
        "gate": H1_GATE,
        "held_constant": {"risk_pct": RISK_PCT, "max_concurrent": MAX_CONCURRENT,
                          "time_stop_bars": 7, "sma_exit_window": 5,
                          "stop_atr_mult": 2.0},
        "costs": {"primary": costed.describe(), "stress": stress.describe()},
        "stats": {"costs_off": s_free, "costs_on": s_costed,
                  "stress": s_stress, "train": s_train, "test": s_test,
                  "split_date": cut},
        "by_year": by_year,
        "portfolio": portfolio,
        "census": census,
        "verdict": verdict,
        "trades": [t.to_dict() for t in trades],
    }, indent=2, default=str))
    print(f"Elapsed: {time.time() - t0:.0f}s")
    print(f"Full output -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
