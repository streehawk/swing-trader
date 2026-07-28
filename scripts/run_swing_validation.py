"""scripts/run_swing_validation.py — BINDING gate for the swing system (Phase 4).

Replays core/swing_setups (entries) + core/swing_exits (exits) bar by bar
over data/validation_cache/prices.parquet and applies a pre-registered
gate. Same discipline as F.5: the thresholds live in
core.swing_backtest.GATE, were fixed before the first run, and the verdict
is binding rather than advisory.

Gate (all must hold):
    expectancy            >= +0.25R      after costs
    win rate              >= 40%
    t-stat on expectancy  >= 2.0         (distinguishable from zero)
    trades                >= 200
    max portfolio DD      <= 15%         at live risk/concurrency settings
    worst-year expectancy >= -0.10R      (no catastrophic regime)

Verdict semantics:
    GO                  — positive expectancy demonstrated on a PIT universe.
    GO_UPPER_BOUND_ONLY — gate metrics passed but the universe is survivor-
                          biased (P1-7 item 1): an upper bound, not an edge.
    INVALID             — look-ahead detected; numbers are meaningless.
    REVERT              — gate failed.

    GO     — the swing engine has demonstrated positive expectancy on this
             (optimistic) sample. Automated sizing stays on; Phase 4's ML
             relabelling may proceed.
    REVERT — keep the engine as a candidate generator requiring the user's
             judgement. Do NOT layer ML ranking on it. This does not mean
             deleting code; it means no edge claim has been earned.

Usage:
    python scripts/run_swing_validation.py
    python scripts/run_swing_validation.py --max-tickers 40 --slippage-bps 15
    python scripts/run_swing_validation.py --out /tmp/swing_validation.json

Output JSON goes to data/validation_cache/swing_validation_<date>.json and
carries the full trade list, so any statistic can be recomputed without
re-running the replay.
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
    GATE, CostModel, compute_stats, confirm_no_lookahead, evaluate_gate,
    run_backtest, simulate_portfolio, stats_by_year, universe_provenance,
)

log = logging.getLogger("swing_validation")

_PRICES = _ROOT / "data" / "validation_cache" / "prices.parquet"

# Carried into the output so no reader can mistake this for a clean
# estimate. Every item listed inflates results.
_KNOWN_BIASES = [
    "SURVIVORSHIP (largest, UNFIXED): prices.parquet is today's CORE list "
    "applied backward — spine_metadata.json records survivorship_inflation=true "
    "and today_list_applied_backward. ZERO of its 241 tickers delist, so every "
    "company that went bankrupt, was acquired, or deteriorated out of the quality "
    "screen is absent. Those are disproportionately the losers.",
    "no PIT earnings calendar: the live engine REJECTS setups with earnings "
    "inside the holding window, the backtest cannot and so takes trades live would skip",
    "costs are a flat bps assumption on top of adverse gap fills, not measured spread",
    "no borrow/halt/limit-up modelling",
]

# Modelled explicitly (recorded so a reader does not assume otherwise):
_MODELLED = [
    "commission + slippage as SEPARATE charges (P1-7 item 2); slippage on "
    "STOP fills only, never on limit fills, floored at max(bps, 1 tick)",
    "order A modelled as a true STOP-LIMIT: a gap beyond the limit that never "
    "returns is NO TRADE, matching plan §6",
    "entry gap fills at max(trigger, open) capped by the limit; stop gap fills "
    "at min(stop, open) — adverse on both sides",
    "when one bar spans both stop and target, the STOP is assumed to hit first",
    "the entry bar's high does not ratchet the stop (we cannot prove we held it)",
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prices", default=str(_PRICES))
    ap.add_argument("--max-tickers", type=int, default=0,
                    help="limit universe size (smoke runs)")
    ap.add_argument("--slippage-bps", type=float, default=10.0,
                    help="adverse slippage per side on STOP fills (P1-7: 5-15)")
    ap.add_argument("--commission", type=float, default=0.0,
                    help="flat commission per side, USD")
    ap.add_argument("--tick-size", type=float, default=0.01,
                    help="slippage floor: max(bps, 1 tick)")
    ap.add_argument("--time-stop-bars", type=int, default=15)
    ap.add_argument("--risk-pct", type=float, default=0.0075,
                    help="P1-7 item 4: hold at 0.0075 for comparability")
    ap.add_argument("--max-concurrent", type=int, default=2)
    ap.add_argument("--out", default="")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    path = Path(args.prices)
    if not path.exists():
        print(f"ERROR: price cache not found at {path}", file=sys.stderr)
        return 2

    prices = pd.read_parquet(path)
    universe = sorted(prices["ticker"].unique())
    # SPY is the benchmark series, not a tradeable name in this universe.
    universe = [t for t in universe if t != "SPY"]
    if args.max_tickers:
        universe = universe[: args.max_tickers]

    # ── P1-7 acceptance: log universe type + cost model, confirm no look-ahead
    provenance = universe_provenance(prices)
    costs = CostModel(commission_per_side=args.commission,
                      slippage_bps=args.slippage_bps,
                      tick_size=args.tick_size)

    print(f"Universe : {provenance['universe_type']} "
          f"({provenance['n_tickers']} tickers, {provenance['n_delisted']} delisted)")
    if provenance["warning"]:
        print(f"           !! {provenance['warning']}")
    print(f"Costs    : commission ${costs.commission_per_side:.2f}/side · "
          f"slippage {costs.slippage_bps:.0f}bps/side on STOP fills · "
          f"tick floor ${costs.tick_size}")
    print(f"Sizing   : risk {args.risk_pct:.2%}/trade · max {args.max_concurrent} concurrent · "
          f"time stop {args.time_stop_bars} bars")

    print("Verifying no look-ahead...", end=" ", flush=True)
    lookahead = confirm_no_lookahead(prices)
    print(f"{'PASS' if lookahead['passed'] else 'FAIL'} "
          f"({lookahead['trades_compared']} trades compared)")
    if not lookahead["passed"]:
        print("  !! LOOK-AHEAD DETECTED — results are meaningless:", file=sys.stderr)
        for m in lookahead["mismatches"]:
            print(f"     {m}", file=sys.stderr)

    print(f"\nReplaying {len(universe)} tickers "
          f"({prices['date'].min()} → {prices['date'].max()})...")
    started = datetime.now(timezone.utc)

    trades = run_backtest(
        prices, tickers=universe,
        costs=costs, time_stop_bars=args.time_stop_bars,
    )

    stats = compute_stats(trades)
    by_year = stats_by_year(trades)
    portfolio = simulate_portfolio(
        trades, risk_pct=args.risk_pct, max_concurrent=args.max_concurrent,
    )
    verdict = evaluate_gate(stats, by_year, portfolio,
                            provenance=provenance, lookahead=lookahead)

    elapsed = (datetime.now(timezone.utc) - started).total_seconds()

    payload = {
        "run_at":        datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "elapsed_sec":   round(elapsed, 1),
        "config": {
            "n_tickers":       len(universe),
            "universe":        provenance,
            "cost_model":      costs.describe(),
            "no_lookahead":    lookahead,
            "time_stop_bars":  args.time_stop_bars,
            "risk_pct":        args.risk_pct,
            "max_concurrent":  args.max_concurrent,
            "exits":           _exit_config(),
            "date_range":      [str(prices["date"].min()), str(prices["date"].max())],
        },
        "gate":          GATE,
        "decision":      verdict["decision"],
        "verdict":       verdict,
        "stats":         stats,
        "by_year":       by_year,
        "portfolio":     portfolio,
        "known_biases":  _KNOWN_BIASES,
        "modelled":      _MODELLED,
        "trades":        [t.to_dict() for t in trades],
    }

    out = Path(args.out) if args.out else (
        _ROOT / "data" / "validation_cache" / f"swing_validation_{date.today()}.json"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2))

    _print_report(payload)
    print(f"\nFull output → {out}")
    return 0 if verdict["decision"] == "GO" else 1


def _exit_config() -> dict:
    """P1-7 item 4 — record the exit settings so a reader can confirm
    they were held constant between runs."""
    from core import swing_exits as sx
    return {
        "breakeven_at_r":   sx.BREAKEVEN_AT_R,
        "trail_enabled":    sx.TRAIL_ENABLED,
        "trail_at_r":       sx.TRAIL_AT_R,
        "trail_atr_mult":   sx.TRAIL_ATR_MULT,
        "partial_fraction": sx.PARTIAL_FRACTION,
    }


def _print_report(p: dict) -> None:
    s, pf, v = p["stats"], p["portfolio"], p["verdict"]
    print("\n" + "=" * 66)
    print(f"SWING VALIDATION — {p['decision']}")
    print("=" * 66)

    if not s.get("n_trades"):
        print("No trades generated — nothing to evaluate.")
        return

    print(f"\nTrades           {s['n_trades']}")
    print(f"Expectancy       {s['expectancy_r']:+.3f}R   (t={s['t_stat']:.2f})")
    print(f"Win rate         {s['win_rate']:.1%}")
    print(f"Avg win / loss   {s['avg_win_r']:+.2f}R / {s['avg_loss_r']:+.2f}R")
    print(f"Profit factor    {s['profit_factor']}")
    print(f"Total            {s['total_r']:+.1f}R")
    print(f"Max DD (R)       {s['max_drawdown_r']:.1f}R")
    print(f"Avg bars held    {s['avg_bars_held']}")
    print(f"Partial taken    {s['pct_partial']:.1%}")
    print(f"\nExits            {s['exit_breakdown']}")
    print(f"Setups           {s['setup_breakdown']}")

    print(f"\nPortfolio sim (risk {pf['risk_pct']:.2%}, max {pf['max_concurrent']} concurrent):")
    print(f"  taken/rejected {pf['n_taken']}/{pf['n_rejected']} "
          f"(rejection {pf['rejection_rate']:.1%})")
    print(f"  return         {pf['total_return_pct']:+.1f}%")
    print(f"  max drawdown   {pf['max_drawdown_pct']:.1f}%")

    print("\nBy year:")
    for y, ys in p["by_year"].items():
        if ys.get("n_trades"):
            print(f"  {y}  n={ys['n_trades']:>4}  exp={ys['expectancy_r']:+.3f}R  "
                  f"win={ys['win_rate']:.0%}  total={ys['total_r']:+.1f}R")

    print("\nGate:")
    for c in v["checks"]:
        print(f"  [{'PASS' if c['pass'] else 'FAIL'}] {c['detail']}")

    print(f"\nDECISION: {p['decision']}")
    print("\nModelled:")
    for m in p.get("modelled", []):
        print(f"  + {m}")
    print("\nKnown biases (all inflate results):")
    for b in p["known_biases"]:
        print(f"  - {b}")


if __name__ == "__main__":
    raise SystemExit(main())
