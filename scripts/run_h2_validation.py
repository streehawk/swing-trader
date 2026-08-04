#!/usr/bin/env python3
"""scripts/run_h2_validation.py — H2 validation ladder (edge-search protocol).

Runs the hypothesis to its verdict and stops. No threshold is chosen
here: the gate comes from `core.protocol_gate.DEFAULT_GATE`, copied out
of `edge_search_protocol.md` before the first run.

    0. universe provenance      PIT or SURVIVOR — decides the ceiling
    1. look-ahead confirmation  empirical, on THIS run's data
    2. cheap run, costs OFF     kill fast
    3. costed run               the gate's actual input
    4. 70/30 train/test split   by DATE, both halves must agree in sign
    5. verdict

ON SLIPPAGE — protocol rule 4 is a rule, not a preference:

    ">= 0.05-0.15%/side or 1 tick, whichever larger; use the LARGER bound
     for anything under $2B cap or under 1M avg volume."

The 407-name screen is $2B+ by construction, but H2's per-bar liquidity
floor is 200k shares, so a real share of the universe sits under 1M
shares/day. The costed run therefore charges **per ticker**: 15bps for
names whose median 20-day average volume over the window is below 1M,
10bps for the rest. A flat 10bps would undercharge exactly the names most
likely to gap, and H2 buys the OPENING AUCTION of a gap day — the single
worst moment for liquidity in a session. A uniform-15bps run is reported
alongside as a stress bound.

Usage:
    python scripts/run_h2_validation.py
    python scripts/run_h2_validation.py --limit 40      # smoke test
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

from core.h2_backtest import (  # noqa: E402
    CostModel, H2_GATE, compute_stats, confirm_no_lookahead_h2,
    evaluate_h2_gate, run_backtest_h2, simulate_portfolio, split_trades,
    stats_by_year, universe_provenance,
)
from strategies.filters import EarningsCalendar  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "data" / "validation_cache"

# Held constant per protocol global rule 3.
RISK_PCT = 0.0075
MAX_CONCURRENT = 2
TIME_STOP_BARS = 15

LIQUID_BPS = 10.0        # >= 1M shares/day
THIN_BPS = 15.0          # < 1M shares/day — protocol's "larger bound"
LIQUIDITY_CUTOFF = 1_000_000.0


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
    ap.add_argument("--earnings", default=str(CACHE / "earnings_dates.parquet"))
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    t0 = time.time()

    prices = pd.read_parquet(args.prices)
    prices["date"] = pd.to_datetime(prices["date"])

    universe = _load_universe()
    available = set(prices["ticker"].unique())
    tickers = [t for t in universe if t in available]
    no_prices = [t for t in universe if t not in available]

    cal = EarningsCalendar.load(args.earnings)
    uncovered = [t for t in tickers if not cal.covers(t)]
    tickers = [t for t in tickers if cal.covers(t)]
    if args.limit:
        tickers = tickers[: args.limit]

    sub = prices[prices["ticker"].isin(tickers + ["SPY"])].copy()
    prov = universe_provenance(sub[sub["ticker"].isin(tickers)])

    # Per-ticker liquidity bucket — see the module docstring on rule 4.
    med_vol = (sub[sub["ticker"].isin(tickers)]
               .groupby("ticker")["volume"].median())
    thin = sorted(med_vol[med_vol < LIQUIDITY_CUTOFF].index)
    liquid = sorted(med_vol[med_vol >= LIQUIDITY_CUTOFF].index)

    print("=" * 78)
    print("H2 — STRONG-GAP CONTINUATION")
    print("=" * 78)
    print(f"Universe   : {prov['universe_type']} — {len(tickers)} tradeable of "
          f"{len(universe)} registered, {prov['n_delisted']} delisted")
    if no_prices:
        print(f"             no price history : {', '.join(no_prices)}")
    if uncovered:
        print(f"             no earnings cover: {', '.join(uncovered)} "
              f"(excluded — an ungatable name would carry the earnings gaps "
              f"H2 is registered to exclude)")
    if prov.get("warning"):
        print(f"             !! {prov['warning']}")
    print(f"Window     : {prov['dataset_start']} -> {prov['dataset_end']}")
    print(f"Earnings   : PIT calendar from EDGAR 8-K item 2.02 — "
          f"{cal.n_tickers} tickers, gate ON (-2d / +10d)")
    print(f"Held const : risk {RISK_PCT:.2%}/trade · max {MAX_CONCURRENT} concurrent "
          f"· time stop {TIME_STOP_BARS} bars")
    print("Exits      : SMA10 trail · 1.5xATR stop · breakeven ratchet at +1R "
          "(no target — registered)")
    print("Regime gate: ON, SPY > SMA200 (registered, no A/B)")
    print(f"Slippage   : {THIN_BPS:.0f}bps for {len(thin)} names under 1M "
          f"median volume, {LIQUID_BPS:.0f}bps for {len(liquid)} above "
          f"(protocol rule 4)")
    print()

    print("Verifying no look-ahead...")
    la = confirm_no_lookahead_h2(sub, n_tickers=4,
                                 strategy_kwargs={"earnings_calendar": cal})
    note = f"  [{la['note']}]" if la.get("note") else ""
    print(f"  {'PASS' if la['passed'] else 'FAIL'} "
          f"({la['trades_compared']} trades compared){note}")
    for m in la["mismatches"]:
        print(f"    ! {m}")
    print()

    skw = {"earnings_calendar": cal}

    def _run(costs_map, census=None):
        """costs_map: {bucket_name: (CostModel, [tickers])}"""
        out = []
        for _name, (cm, tk) in costs_map.items():
            if not tk:
                continue
            out.extend(run_backtest_h2(
                sub, tickers=tk, costs=cm, time_stop_bars=TIME_STOP_BARS,
                strategy_kwargs=skw, census=census))
        return out

    free = CostModel(commission_per_side=0.0, slippage_bps=0.0, tick_size=0.0)
    print("Running (costs OFF)...")
    census: dict = {}
    trades_free = _run({"all": (free, tickers)}, census=census)
    s_free = compute_stats(trades_free)
    print(f"  {s_free.get('n_trades', 0)} trades, "
          f"E={s_free.get('expectancy_r', 0):+.4f}R")

    print(f"Running (costs ON, {LIQUID_BPS:.0f}/{THIN_BPS:.0f}bps by liquidity)...")
    cm_liquid = CostModel(slippage_bps=LIQUID_BPS, tick_size=0.01)
    cm_thin = CostModel(slippage_bps=THIN_BPS, tick_size=0.01)
    trades = _run({"liquid": (cm_liquid, liquid), "thin": (cm_thin, thin)})
    trades.sort(key=lambda t: t.entry_date)
    s_costed = compute_stats(trades)
    print(f"  {s_costed.get('n_trades', 0)} trades, "
          f"E={s_costed.get('expectancy_r', 0):+.4f}R")

    print(f"Running (stress, uniform {THIN_BPS:.0f}bps)...")
    trades_stress = _run({"all": (cm_thin, tickers)})
    s_stress = compute_stats(trades_stress)
    print(f"  {s_stress.get('n_trades', 0)} trades, "
          f"E={s_stress.get('expectancy_r', 0):+.4f}R")
    print()

    train, test, cut = split_trades(trades, 0.70)
    s_train, s_test = compute_stats(train), compute_stats(test)
    portfolio = simulate_portfolio(trades, risk_pct=RISK_PCT,
                                   max_concurrent=MAX_CONCURRENT)
    by_year = stats_by_year(trades)

    print("=" * 78)
    print("RESULTS")
    print("=" * 78)
    print(HEADER)
    print(SEP)
    print(_fmt_row("costs OFF", s_free))
    print(_fmt_row("costs ON (10/15bps)", s_costed))
    print(_fmt_row("stress (15bps flat)", s_stress))
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
        gaps = pd.Series([t.gap_atr for t in trades])
        print(f"Gap size (ATR) : median {gaps.median():.2f}, "
              f"p90 {gaps.quantile(0.9):.2f}, max {gaps.max():.2f}")
        print(f"Portfolio      : {portfolio['n_taken']} taken, "
              f"{portfolio['n_rejected']} rejected by the "
              f"{MAX_CONCURRENT}-position cap "
              f"({portfolio['rejection_rate']:.1%}) · "
              f"return {portfolio['total_return_pct']:+.1f}% · "
              f"max DD {portfolio['max_drawdown_pct']:.1f}%")
        print()

    total = census.get("bars", 0)
    if total:
        print(f"CENSUS: bars {total:,} | gap signals {census.get('signal', 0)} "
              f"({census.get('signal', 0)/total:.4%})")
        for k, v in sorted(census.get("reasons", {}).items(),
                           key=lambda kv: -kv[1]):
            print(f"  {k:<22} {v:>9,}  {v/total:>7.3%}")
        print()

    verdict = evaluate_h2_gate(s_costed, s_train, s_test,
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
        CACHE / f"h2_validation_{pd.Timestamp.today().date()}.json")
    out.write_text(json.dumps({
        "hypothesis": "H2 — strong-gap continuation",
        "run_date": str(pd.Timestamp.today().date()),
        "universe": {"registered": len(universe), "tradeable": len(tickers),
                     "no_price_history": no_prices,
                     "no_earnings_coverage": uncovered,
                     "thin_names_15bps": thin, "liquid_names_10bps": liquid},
        "provenance": prov,
        "lookahead": la,
        "gate": H2_GATE,
        "held_constant": {"risk_pct": RISK_PCT, "max_concurrent": MAX_CONCURRENT,
                          "time_stop_bars": TIME_STOP_BARS, "trail_sma": 10,
                          "stop_atr_mult": 1.5, "breakeven_at_r": 1.0},
        "costs": {"liquid": cm_liquid.describe(), "thin": cm_thin.describe(),
                  "cutoff_shares": LIQUIDITY_CUTOFF},
        "earnings_calendar": {"source": "EDGAR 8-K item 2.02",
                              "n_tickers": cal.n_tickers,
                              "window_days": {"before": 2, "after": 10}},
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
