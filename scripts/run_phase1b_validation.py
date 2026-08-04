"""scripts/run_phase1b_validation.py — Phase 1B VALIDATION PROTOCOL (B0-B4).

Runs the five pre-registered configurations from the Phase 1B spec and
emits ONE results table, then the PASS / MARGINAL / FAIL verdict against
the pre-registered gate.

    B0  Phase 1 (trend-pullback)              benchmark   costs+slippage
    B1  Phase 1B, no regime gate              survivor    no costs
    B2  Phase 1B + SPY>200SMA regime gate     survivor    no costs
    B3  better of B1/B2                       PIT*        no costs
    B4  best config so far                    PIT*        costs+slippage

    * A point-in-time universe including delisted tickers is NOT available
      in this repo. B0/B3/B4 therefore run on the SURVIVOR cache and are
      labelled as such. Every result is an UPPER BOUND: the names that
      went bankrupt or fell out of the quality screen — exactly the ones a
      dip-buying strategy would have kept buying into — are absent.

HELD CONSTANT across every run (P1B-7 item 4), so any difference is
attributable to the entry signal alone:
    sizing 0.75% risk/trade · max 2 concurrent
    Phase-3 exits: breakeven ratchet at 1R, trail 1.5xATR from 1.5R,
                   50% partial at target
    15 trading-day time stop
    earnings gate OFF in backtest for BOTH strategies — there is no
    point-in-time earnings calendar, and applying today's calendar to
    2021 would be look-ahead. Phase 1's backtest already omits it, so
    this keeps the comparison honest in both directions.

COSTS: P1B-7 item 2 requires the LARGER slippage assumption for Phase 1B
given its 200k-volume (thinner) names. B0 (Phase 1, 1M+ volume names)
uses 10bps/side; B4 (Phase 1B) uses 15bps/side — the top of the ticket's
0.05-0.15% range. This is deliberately unfavourable to the new strategy
because its universe genuinely is thinner.

No thresholds are tuned. The configs and the gate were fixed before the
first run.

Usage:
    python scripts/run_phase1b_validation.py
    python scripts/run_phase1b_validation.py --max-tickers 40   # smoke
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from core.swing_backtest import (  # noqa: E402
    CostModel, compute_stats, confirm_no_lookahead, run_backtest,
    simulate_portfolio, stats_by_year, universe_provenance,
)

log = logging.getLogger("phase1b")

_PRICES = _ROOT / "data" / "validation_cache" / "prices.parquet"

# ── Pre-registered gate (Phase 1B spec) ──────────────────────────────
GATE_P1B = {
    "min_expectancy_r":  0.10,   # B4, point-in-time + costs
    "min_trades":        300,
    "min_profit_factor": 1.20,
    "must_beat_b0":      True,   # and beat the Phase 1 benchmark
}

# Held constant everywhere (P1B-7 item 4)
RISK_PCT       = 0.0075
MAX_CONCURRENT = 2
TIME_STOP_BARS = 15

EXITS_ENV = {
    "SWING_TRAIL_ENABLED":   "1",
    "SWING_PARTIAL_FRACTION": "0.5",
}


def _run(
    label: str,
    prices: pd.DataFrame,
    tickers: list[str],
    *,
    strategy: str,
    regime: bool,
    costs_on: bool,
    slippage_bps: float,
    min_avg_vol: float,
    min_rvol: float,
) -> dict:
    """Execute one configuration and return its row."""
    import os

    costs = (
        CostModel(commission_per_side=0.0, slippage_bps=slippage_bps, tick_size=0.01)
        if costs_on else
        CostModel(commission_per_side=0.0, slippage_bps=0.0, tick_size=0.0)
    )

    if strategy == "dip_in_uptrend":
        skw = {
            "use_regime_gate": regime,
            "min_avg_vol":     min_avg_vol,
            "min_rvol":        min_rvol,
            # No PIT earnings calendar — see module docstring.
            "check_earnings":  False,
        }
    else:
        # Phase 1 reads its regime gate from the environment. Set it per
        # run rather than leaving whatever the shell had.
        os.environ["SWING_REGIME_GATE"] = "1" if regime else "0"
        skw = {}

    print(f"  [{label}] {strategy} · regime={'on' if regime else 'off'} · "
          f"costs={'on' if costs_on else 'off'}"
          f"{f' ({slippage_bps:.0f}bps)' if costs_on else ''} ...",
          end=" ", flush=True)

    trades = run_backtest(
        prices, tickers=tickers, costs=costs,
        time_stop_bars=TIME_STOP_BARS,
        strategy=strategy, strategy_kwargs=skw, progress_every=0,
    )
    stats = compute_stats(trades)
    by_year = stats_by_year(trades)
    pf = simulate_portfolio(trades, risk_pct=RISK_PCT, max_concurrent=MAX_CONCURRENT)
    print(f"{stats.get('n_trades', 0)} trades, "
          f"E={stats.get('expectancy_r', 0):+.3f}R")

    return {
        "label":     label,
        "strategy":  strategy,
        "regime":    "on" if regime else "off",
        "costs":     f"{slippage_bps:.0f}bps" if costs_on else "none",
        "stats":     stats,
        "by_year":   by_year,
        "portfolio": pf,
    }


def _fmt_row(r: dict) -> str:
    s, pf = r["stats"], r["portfolio"]
    n = s.get("n_trades", 0)
    if not n:
        return (f"| {r['label']} | {r['strategy'][:14]} | {r['regime']} | {r['costs']} "
                f"| 0 | — | — | — | — | — | — | — |")
    bw = s.get("breakeven_win_rate")
    return (
        f"| {r['label']} | {r['strategy'][:14]} | {r['regime']} | {r['costs']} "
        f"| {n} | {s['win_rate']:.1%} | {s['avg_win_r']:+.2f} | {s['avg_loss_r']:+.2f} "
        f"| **{s['expectancy_r']:+.3f}** | {bw:.1%} | {pf['max_drawdown_pct']:.1f}% "
        f"| {s['profit_factor']} |"
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prices", default=str(_PRICES))
    ap.add_argument("--max-tickers", type=int, default=0)
    ap.add_argument("--min-avg-vol", type=float, default=200_000.0)
    ap.add_argument("--min-rvol", type=float, default=1.5)
    ap.add_argument("--b0-slippage-bps", type=float, default=10.0)
    ap.add_argument("--p1b-slippage-bps", type=float, default=15.0,
                    help="P1B-7: larger assumption for thinner names")
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    logging.basicConfig(level=logging.WARNING)

    import os
    for k, v in EXITS_ENV.items():
        os.environ[k] = v

    path = Path(args.prices)
    if not path.exists():
        print(f"ERROR: price cache not found at {path}", file=sys.stderr)
        return 2

    prices = pd.read_parquet(path)
    universe = [t for t in sorted(prices["ticker"].unique()) if t != "SPY"]
    if args.max_tickers:
        universe = universe[: args.max_tickers]

    provenance = universe_provenance(prices)

    print("=" * 78)
    print("PHASE 1B VALIDATION PROTOCOL — runs B0 through B4")
    print("=" * 78)
    print(f"Universe   : {provenance['universe_type']} — {provenance['n_tickers']} tickers, "
          f"{provenance['n_delisted']} delisted")
    if provenance["warning"]:
        print(f"             !! {provenance['warning']}")
        print("             !! B0/B3/B4 cannot be run point-in-time. Labelled SURVIVOR.")
    print(f"Held const : risk {RISK_PCT:.2%}/trade · max {MAX_CONCURRENT} concurrent · "
          f"time stop {TIME_STOP_BARS} bars")
    print(f"Exits      : {EXITS_ENV}")
    print(f"Filters    : min_avg_vol={args.min_avg_vol:,.0f} · min_rvol={args.min_rvol}")
    print("Earnings   : gate OFF in backtest for BOTH strategies (no PIT calendar)")

    print("\nVerifying no look-ahead (both strategies)...")
    la_p1 = confirm_no_lookahead(prices, strategy="trend_pullback")
    la_p1b = confirm_no_lookahead(
        prices, strategy="dip_in_uptrend",
        strategy_kwargs={"check_earnings": False, "use_regime_gate": False},
    )
    print(f"  trend_pullback : {'PASS' if la_p1['passed'] else 'FAIL'} "
          f"({la_p1['trades_compared']} trades compared)")
    print(f"  dip_in_uptrend : {'PASS' if la_p1b['passed'] else 'FAIL'} "
          f"({la_p1b['trades_compared']} trades compared)"
          + ("  [INCONCLUSIVE — no closed trades]" if not la_p1b["trades_compared"] else ""))
    if not (la_p1["passed"] and la_p1b["passed"]):
        print("  !! LOOK-AHEAD DETECTED — results are meaningless", file=sys.stderr)

    started = datetime.now(timezone.utc)
    print("\nRunning...")

    rows = []
    rows.append(_run("B0", prices, universe, strategy="trend_pullback",
                     regime=True, costs_on=True, slippage_bps=args.b0_slippage_bps,
                     min_avg_vol=args.min_avg_vol, min_rvol=args.min_rvol))
    rows.append(_run("B1", prices, universe, strategy="dip_in_uptrend",
                     regime=False, costs_on=False, slippage_bps=0,
                     min_avg_vol=args.min_avg_vol, min_rvol=args.min_rvol))
    rows.append(_run("B2", prices, universe, strategy="dip_in_uptrend",
                     regime=True, costs_on=False, slippage_bps=0,
                     min_avg_vol=args.min_avg_vol, min_rvol=args.min_rvol))

    # B3 = better of B1/B2 on expectancy, re-run on a PIT universe.
    # No PIT universe exists, so B3 is the same configuration on the same
    # SURVIVOR data: it is reported for completeness and is NOT an
    # independent measurement. Stated plainly rather than silently reused.
    b1, b2 = rows[1], rows[2]
    best_regime = b2["regime"] == "on" if (
        b2["stats"].get("expectancy_r", -99) > b1["stats"].get("expectancy_r", -99)
    ) else False
    best_label = "B2" if best_regime else "B1"

    rows.append(_run("B3", prices, universe, strategy="dip_in_uptrend",
                     regime=best_regime, costs_on=False, slippage_bps=0,
                     min_avg_vol=args.min_avg_vol, min_rvol=args.min_rvol))
    rows.append(_run("B4", prices, universe, strategy="dip_in_uptrend",
                     regime=best_regime, costs_on=True,
                     slippage_bps=args.p1b_slippage_bps,
                     min_avg_vol=args.min_avg_vol, min_rvol=args.min_rvol))

    elapsed = (datetime.now(timezone.utc) - started).total_seconds()

    # ── The single results table ─────────────────────────────────────
    print("\n" + "=" * 78)
    print("RESULTS")
    print("=" * 78)
    print("| Run | Strategy | Regime | Costs | #Trades | Win% | AvgWin R | AvgLoss R "
          "| Expectancy | Breakeven Win% | MaxDD | PF |")
    print("|---|---|---|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        print(_fmt_row(r))

    b0, b4 = rows[0], rows[4]
    b0s, b4s = b0["stats"], b4["stats"]

    # ── Verdict ──────────────────────────────────────────────────────
    e4 = b4s.get("expectancy_r", 0.0)
    n4 = b4s.get("n_trades", 0)
    pf4 = b4s.get("profit_factor", 0.0)
    if pf4 == float("inf"):
        pf4 = 999.0
    e0 = b0s.get("expectancy_r", 0.0)

    beats_b0 = e4 > e0
    checks = [
        ("expectancy >= +0.10R", e4 >= GATE_P1B["min_expectancy_r"],
         f"{e4:+.3f}R (need >= +{GATE_P1B['min_expectancy_r']:.2f}R)"),
        ("#trades >= 300", n4 >= GATE_P1B["min_trades"],
         f"{n4} (need >= {GATE_P1B['min_trades']})"),
        ("profit factor >= 1.2", pf4 >= GATE_P1B["min_profit_factor"],
         f"{pf4} (need >= {GATE_P1B['min_profit_factor']})"),
        ("beats B0 on expectancy", beats_b0,
         f"B4 {e4:+.3f}R vs B0 {e0:+.3f}R"),
    ]

    if all(ok for _, ok, _ in checks):
        verdict = "PASS"
    elif e4 <= 0:
        verdict = "FAIL"
    else:
        verdict = "MARGINAL"

    print("\n" + "=" * 78)
    print(f"VERDICT: {verdict}")
    print("=" * 78)
    for name, ok, detail in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail}")

    print("\nAttribution:")
    _e = lambda r: r["stats"].get("expectancy_r", 0.0)
    print(f"  B1 -> B2 (regime gate)   : {_e(b1):+.3f}R -> {_e(b2):+.3f}R "
          f"({_e(b2) - _e(b1):+.3f}R)  — best: {best_label}")
    print(f"  B2 -> B3 (survivorship)  : NOT MEASURABLE — no point-in-time universe")
    print(f"  B3 -> B4 (cost drag)     : {_e(rows[3]):+.3f}R -> {_e(b4):+.3f}R "
          f"({_e(b4) - _e(rows[3]):+.3f}R)")

    print(f"\nElapsed: {elapsed:.0f}s")

    payload = {
        "run_at":      datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "elapsed_sec": round(elapsed, 1),
        "gate":        GATE_P1B,
        "verdict":     verdict,
        "checks":      [{"name": n, "pass": ok, "detail": d} for n, ok, d in checks],
        "universe":    provenance,
        "held_constant": {
            "risk_pct": RISK_PCT, "max_concurrent": MAX_CONCURRENT,
            "time_stop_bars": TIME_STOP_BARS, "exits": EXITS_ENV,
            "earnings_gate": "OFF in backtest (no PIT calendar) for BOTH strategies",
        },
        "no_lookahead": {"trend_pullback": la_p1, "dip_in_uptrend": la_p1b},
        "best_p1b_config": best_label,
        "runs": [
            {k: v for k, v in r.items() if k != "by_year"} | {"by_year": r["by_year"]}
            for r in rows
        ],
    }
    out = Path(args.out) if args.out else (
        _ROOT / "data" / "validation_cache" / f"phase1b_validation_{date.today()}.json"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2, default=str))
    print(f"Full output → {out}")
    return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
