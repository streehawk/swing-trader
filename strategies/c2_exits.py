"""strategies/c2_exits.py — Addendum 2: the five registered exit rules.

`c2_exit_study_addendum.md` asks one question: does any of the book's
alternative dynamic exits beat the validated RSI(2) > 65 on the C2 basket,
by enough and consistently enough to matter?

THE ENTRY IS FROZEN. Every variant here uses `c2_entry` — the exact
function the C-series and Addendum 1 validated — imported, not copied. A
re-typed entry that drifted by one character would make the whole study
measure something other than the exit.

E3 IS NOT A NEW RULE. "close > SMA5" is already implemented as `c1_exit`
(it is C1's exit), so E3 reuses it rather than defining a second version
that could diverge. E0 likewise reuses `cum_exit`, which is what live
trades today.

WHY THE THRESHOLD EXITS ARE GENERATED, NOT HAND-WRITTEN: E0/E1/E2 differ
only in a number. Writing three near-identical functions invites the
copy-paste error where E2 silently checks 70. One factory, three
registered constants.

REGISTERED AND FINAL: five variants, five rules, no combinations, no
trailing variants, no interpolation. The addendum's own words — "Five is
already four chances to get fooled by luck."
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

import numpy as np

from strategies.rsi2 import (
    CUM_RSI_EXIT, Indicators, Variant, c1_exit, c2_entry, cum_exit,
    sma_series,
)

# ── The contenders. Registered in the addendum; no additions. ────────
E0_RSI_EXIT = CUM_RSI_EXIT      # 65.0 — imported, so it cannot drift from live
E1_RSI_EXIT = 70.0
E2_RSI_EXIT = 75.0
E4_SMA_WINDOW = 10


def make_rsi_exit(threshold: float) -> Callable[[Indicators, int], bool]:
    """Exit when RSI(2) closes above `threshold`.

    Mirrors `cum_exit` exactly apart from the constant — same finite guard,
    same strict `>` comparison. The strictness matters: `>=` would fire a
    bar earlier on an exact touch, which is a different rule.
    """
    def _exit(ind: Indicators, i: int) -> bool:
        if not (0 <= i < len(ind.closes)) or not np.isfinite(ind.rsi2[i]):
            return False
        return bool(ind.rsi2[i] > threshold)
    _exit.__name__ = f"rsi2_exit_gt_{threshold:g}".replace(".", "_")
    return _exit


def sma10_exit(ind: Indicators, i: int) -> bool:
    """E4 — exit when the close is above its SMA10.

    Falls back to computing the series if `sma10` is absent, so an
    `Indicators` built by older code (or by a test constructing fields by
    hand) cannot silently return False and look like "no exit ever". A
    never-firing exit would make E4 look like a 25-day-valve strategy and
    the study would report a rule nobody proposed.
    """
    if not (0 <= i < len(ind.closes)):
        return False
    s = ind.sma10
    if s is None:
        s = sma_series(ind.closes, E4_SMA_WINDOW)
    if not np.isfinite(s[i]):
        return False
    return bool(ind.closes[i] > s[i])


@dataclass(frozen=True)
class ExitSpec:
    exit_id:     str
    rule:        str
    status:      str
    exit_fn:     Callable[[Indicators, int], bool]
    family:      str          # "RSI" or "MA" — the attribution question
    book_rank:   Optional[int] = None   # in-sample ranking, best = 1


# The book's in-sample ranking (1995-2007, a DIFFERENT entry):
#   RSI>75 >~ RSI>70 ~= MA10 >~ RSI>65 ~= MA5
# Recorded here so the out-of-sample comparison is against a prediction
# written down first, not against whatever ordering the results produce.
EXITS: dict[str, ExitSpec] = {
    "E0": ExitSpec("E0", "RSI(2) > 65", "INCUMBENT (validated, live)",
                   cum_exit, "RSI", book_rank=4),
    "E1": ExitSpec("E1", "RSI(2) > 70", "challenger",
                   make_rsi_exit(E1_RSI_EXIT), "RSI", book_rank=2),
    "E2": ExitSpec("E2", "RSI(2) > 75", "challenger",
                   make_rsi_exit(E2_RSI_EXIT), "RSI", book_rank=1),
    "E3": ExitSpec("E3", "close > SMA5", "challenger",
                   c1_exit, "MA", book_rank=5),
    "E4": ExitSpec("E4", "close > SMA10", "challenger",
                   sma10_exit, "MA", book_rank=3),
}

EXIT_IDS = tuple(EXITS)
INCUMBENT = "E0"
RSI_FAMILY = tuple(k for k, v in EXITS.items() if v.family == "RSI")
MA_FAMILY = tuple(k for k, v in EXITS.items() if v.family == "MA")


def get_exit(exit_id: str) -> ExitSpec:
    key = (exit_id or "").strip().upper()
    if key not in EXITS:
        raise ValueError(f"unknown exit {exit_id!r} — the registered set is "
                         f"{', '.join(EXIT_IDS)}; the addendum forbids additions")
    return EXITS[key]


def exit_variant(exit_id: str) -> Variant:
    """A `Variant` pairing the FROZEN C2 entry with one registered exit.

    `book_win_rate` is None on purpose: the published figure belongs to the
    book's own entry and window, and attaching it here would invite an
    L0-style comparison that means nothing for this study.
    """
    spec = get_exit(exit_id)
    return Variant(
        name=f"C2-{spec.exit_id}",
        description=f"C2 entry (frozen) + exit {spec.exit_id}: {spec.rule}",
        entry_fn=c2_entry,
        exit_fn=spec.exit_fn,
        book_win_rate=None,
    )


def raw_entry_signals(closes) -> list[int]:
    """Every bar where the frozen C2 entry fires, ignoring position state.

    This is the ONE quantity that is genuinely identical across all five
    variants — the addendum's "shared entry count". The count of entries
    actually TAKEN is not identical and cannot be: `replay_variant` holds
    one position per instrument and resumes scanning after the exit, so a
    slower exit necessarily skips signals a faster one caught.
    """
    ind = Indicators.build(closes)
    return [i for i in range(len(ind.closes)) if c2_entry(ind, i)]
