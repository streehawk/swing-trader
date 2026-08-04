"""core/protocol_gate.py — the edge-search protocol's shared verdict rules.

`edge_search_protocol.md` registers ONE default gate for H1, H2 and H3:

    expectancy >= +0.10R/trade with costs ON, >= 300 trades, profit
    factor >= 1.2, and the sign of expectancy holds in BOTH the train and
    test splits. On survivor data this reads MARGINAL (upper bound).
    0 to +0.10R = no reliable edge. <= 0 = FAIL.

plus two global rules that decide a verdict before any of that is
consulted: rule 7's trade-count floor (< 100 trades is
INSUFFICIENT-SAMPLE, not FAIL — a small sample is ignorance, not
evidence of absence) and rule 8's look-ahead check (a positive result
from a leaking backtest is not a weak result, it is not a result).

This lives in its own module because it is protocol-level, not
hypothesis-level. Three hypotheses sharing one implementation cannot
drift into three subtly different definitions of "passed", and there is
exactly one place to look to confirm nothing was tuned between runs.
"""
from __future__ import annotations

from typing import Optional

import pandas as pd

# Copied from edge_search_protocol.md. Not chosen here, not tunable here.
DEFAULT_GATE = {
    "min_expectancy_r":  0.10,    # with costs ON
    "min_trades":        300,
    "min_profit_factor": 1.2,
    "sample_floor":      100,     # below this: INSUFFICIENT-SAMPLE
}


def split_trades(trades: list, train_frac: float = 0.70) -> tuple[list, list, str]:
    """Protocol global rule 6 — split by DATE, not by trade count.

    Splitting on the trade index would put a quiet year and a busy year
    on the same side of the line and call it a time split. The cut is the
    calendar date at `train_frac` of the span, so each half is a genuine
    out-of-sample period relative to the other.

    Works on any trade object exposing `.entry_date` as an ISO string.
    """
    if not trades:
        return [], [], "n/a"
    dates = sorted({t.entry_date for t in trades})
    first, last = pd.Timestamp(dates[0]), pd.Timestamp(dates[-1])
    cut = first + (last - first) * train_frac
    cut_s = str(cut.date())
    train = [t for t in trades if t.entry_date <= cut_s]
    test = [t for t in trades if t.entry_date > cut_s]
    return train, test, cut_s


def evaluate_default_gate(
    stats_costed: dict,
    train_stats: dict,
    test_stats: dict,
    *,
    provenance: Optional[dict] = None,
    lookahead: Optional[dict] = None,
    gate: Optional[dict] = None,
    extra_caveats: Optional[list[str]] = None,
) -> dict:
    """Apply the protocol's default gate.

    Verdicts:
      INVALID              look-ahead detected — nothing else matters
      INSUFFICIENT-SAMPLE  < 100 trades (global rule 7)
      FAIL                 expectancy <= 0, or a sign flip between splits
      NO-EDGE              0 < expectancy < +0.10R
      MARGINAL             meets the gate, but on survivor data
      PASS                 meets the gate on point-in-time data
    """
    g = dict(DEFAULT_GATE)
    if gate:
        g.update(gate)

    n = stats_costed.get("n_trades", 0)
    exp_r = stats_costed.get("expectancy_r", 0.0)
    pf = stats_costed.get("profit_factor", 0.0)

    tr_e = train_stats.get("expectancy_r", 0.0) if train_stats.get("n_trades") else None
    te_e = test_stats.get("expectancy_r", 0.0) if test_stats.get("n_trades") else None
    sign_holds = (
        tr_e is not None and te_e is not None
        and ((tr_e > 0 and te_e > 0) or (tr_e < 0 and te_e < 0))
    )

    checks = [
        ("n_trades", n >= g["min_trades"], f"{n} trades (need >= {g['min_trades']})"),
        ("expectancy_r", exp_r >= g["min_expectancy_r"],
         f"expectancy {exp_r:+.3f}R with costs (need >= {g['min_expectancy_r']:+.2f}R)"),
        ("profit_factor", pf >= g["min_profit_factor"],
         f"profit factor {pf:.3f} (need >= {g['min_profit_factor']:.1f})"),
        ("split_sign", sign_holds,
         f"train {tr_e:+.3f}R vs test {te_e:+.3f}R"
         if (tr_e is not None and te_e is not None) else "one split empty"),
    ]
    passed = all(ok for _, ok, _ in checks)

    caveats: list[str] = list(extra_caveats or [])

    if n < g["sample_floor"]:
        decision = "INSUFFICIENT-SAMPLE"
    elif passed:
        decision = "PASS"
    elif exp_r <= 0 or not sign_holds:
        decision = "FAIL"
    elif 0 < exp_r < g["min_expectancy_r"]:
        decision = "NO-EDGE"
    else:
        decision = "FAIL"

    if lookahead is not None and lookahead.get("passed") is False:
        decision = "INVALID"
        caveats.append("LOOK-AHEAD DETECTED — results are meaningless until fixed: "
                       + "; ".join(lookahead.get("mismatches", [])[:3]))

    if provenance is not None and provenance.get("universe_type") != "PIT":
        caveats.append(
            f"SURVIVOR universe ({provenance.get('n_delisted', 0)} delistings in "
            f"{provenance.get('n_tickers', '?')} tickers). Protocol rule 5: the "
            f"best achievable verdict on this data is MARGINAL (upper bound)."
        )
        if decision == "PASS":
            decision = "MARGINAL"

    return {
        "decision": decision,
        "gate": g,
        "checks": [{"name": nm, "pass": ok, "detail": d} for nm, ok, d in checks],
        "reasons": [f"{'PASS' if ok else 'FAIL'}: {d}" for _, ok, d in checks],
        "caveats": caveats,
        "train_expectancy_r": tr_e,
        "test_expectancy_r": te_e,
    }
