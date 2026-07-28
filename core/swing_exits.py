"""core/swing_exits.py — Exit state machine for swing positions (Phase 3).

Phase 1 made the system emit trade plans. This module makes the system
MANAGE them. Without it a swing entry was handed to exit logic calibrated
for a different strategy entirely: conviction_radar defaulted to a 2.5×ATR
trailing stop with a 120-day time decay, and position_monitor to a flat
-12% stop. A 15-day plan with a 4% stop managed on a 120-day, 12% leash is
not the trade that was taken.

Pure function, no I/O. Both consumers (core/conviction_radar for
user-held positions, portfolio/position_monitor for bot-created ones)
call the same evaluator, so the two paths cannot drift apart — a real
risk in this codebase, where ATR trailing stops are currently implemented
twice with different multipliers.

Everything is expressed in R, where 1R = the risk taken at entry
(entry − initial stop). R is the only unit in which a 3%-stop trade and
an 8%-stop trade are comparable, and it is what Phase 4's validation gate
(expectancy ≥ 0.25R) will be measured in.

Exit ladder, evaluated in priority order:

  1. STOP        price <= effective stop → close remaining
  2. TARGET      price >= target, once → sell PARTIAL_FRACTION (50%),
                 remainder rides with the stop at least at breakeven
  3. TIME STOP   days_held >= plan.time_stop_days → close remaining
  4. RAISE STOP  stop ratcheted up this tick (not an exit, but the user
                 must move the resting order in T212)
  5. HOLD        nothing to do

The stop RATCHETS and never loosens:
      initial     = plan stop (entry − 1.5 ATR from the setup)
      breakeven   = entry, once MFE >= 1.0R
      trail       = high_water − 1.5×ATR, once MFE >= 1.5R

MFE (max favourable excursion) drives the ratchet, not the current price.
Using current price would let a position that ran to +1.8R and pulled back
to +0.4R silently keep its original stop — the giving-back case the
breakeven rule exists to prevent.

Why partial-at-target rather than all-out: the target is 2.5 ATR, which
the plan can justify; how far a winner runs beyond that is not something
this system claims to know. Taking half locks the R the plan promised and
leaves the unknowable upside on a trailing stop.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional


def _envf(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


# ── Tunables ─────────────────────────────────────────────────────────
# Defaults follow the Stock Swing Trading Plan §6, whose exit model is an
# OTOCO bracket: one entry, one full-size take-profit at 2R, one stop.
# That bracket can express a break-even ratchet (modify order C) but NOT
# a continuous ATR trail and NOT a half-size scale-out — so the trail is
# OFF and the target takes the FULL position by default.
#
# v1 shipped a 50% partial plus a 1.5xATR trail. The Phase 4 diagnostic
# found that choice made no measurable difference (+0.016R vs +0.021R,
# both indistinguishable from zero), so nothing is lost by matching the
# plan's simpler, actually-placeable model. Both remain env-overridable.
BREAKEVEN_AT_R    = _envf("SWING_BREAKEVEN_AT_R", 1.0)
TRAIL_ENABLED     = os.environ.get("SWING_TRAIL_ENABLED", "0").strip() != "0"
TRAIL_AT_R        = _envf("SWING_TRAIL_AT_R", 1.5)
TRAIL_ATR_MULT    = _envf("SWING_TRAIL_ATR_MULT", 1.5)
PARTIAL_FRACTION  = _envf("SWING_PARTIAL_FRACTION", 1.0)   # 1.0 = full exit at target

# ── Trigger vocabulary ───────────────────────────────────────────────
# Distinct from the DIP/MOMENTUM triggers so alerts, close_reason values
# and any later trade-journal grouping can tell a mechanical swing exit
# apart from an ML-driven or discretionary one.
SWING_STOP           = "SWING_STOP"            # stopped at the original plan stop = -1R
SWING_BREAKEVEN_STOP = "SWING_BREAKEVEN_STOP"  # stopped at entry after running to +1R
SWING_TRAIL_STOP     = "SWING_TRAIL_STOP"      # stopped on the ATR trail, in profit
SWING_TARGET_PARTIAL = "SWING_TARGET_PARTIAL"  # target hit → take half
SWING_TIME_STOP      = "SWING_TIME_STOP"       # 15 bars up, no resolution
SWING_RAISE_STOP     = "SWING_RAISE_STOP"      # move the resting order
SWING_HOLD           = "SWING_HOLD"
SWING_PLAN_INVALID   = "SWING_PLAN_INVALID"

# Exits the user pre-committed to when taking the trade. These bypass
# never_sell in conviction_radar: a stop you can opt out of is not a stop.
MECHANICAL_EXITS = frozenset({
    SWING_STOP, SWING_BREAKEVEN_STOP, SWING_TRAIL_STOP, SWING_TIME_STOP,
})


def trading_days_to_calendar(n: int) -> int:
    """Convert a trading-day horizon to calendar days.

    The setup engine expresses the time stop in TRADING days (15 bars);
    both position records measure age in CALENDAR days (`days_held` is
    `date.today() - entry_date`). Comparing the two directly would fire
    the time stop ~30% early — after 15 calendar days only ~11 bars have
    printed. Weekends only; holidays are noise at this horizon.
    """
    return int(round(n * 7 / 5))


@dataclass
class SwingPlan:
    """The trade plan frozen at entry. Mirrors core.swing_setups.SwingSetup
    fields that survive into the position record."""
    entry:          float
    stop:           float   # ORIGINAL plan stop — never mutated
    target:         float
    time_stop_days: int = 15

    @property
    def r(self) -> float:
        """1R in price terms. Non-positive means the plan is malformed."""
        return self.entry - self.stop


@dataclass
class SwingState:
    """Mutable state carried on the position record between ticks."""
    current_stop:  float = 0.0    # ratcheted stop; 0 = never set, use plan.stop
    high_water:    float = 0.0    # highest price seen since entry
    partial_taken: bool = False


@dataclass
class ExitDecision:
    action:         str            # HOLD | EXIT | PARTIAL | RAISE_STOP
    trigger:        str
    severity:       str            # high | med | low
    new_stop:       float
    stop_basis:     str            # initial | breakeven | trail
    stop_moved:     bool
    high_water:     float
    r_multiple:     float          # current unrealised, in R
    mfe_r:          float          # best unrealised seen, in R
    sell_fraction:  float          # of the REMAINING position
    message:        str
    reasons:        list = field(default_factory=list)

    @property
    def is_exit(self) -> bool:
        return self.action in ("EXIT", "PARTIAL")


def evaluate_swing_exit(
    plan: SwingPlan,
    state: SwingState,
    *,
    current_price: float,
    atr: Optional[float] = None,
    days_held: int = 0,
) -> ExitDecision:
    """Decide what to do with an open swing position today.

    `atr` may be None (data failure). The breakeven ratchet still works —
    it needs only entry and R — but the ATR trail is skipped rather than
    guessed at, so a missing ATR can never LOOSEN a stop, only fail to
    tighten it further.
    """
    r = plan.r
    if r <= 0 or plan.entry <= 0:
        return ExitDecision(
            action="HOLD", trigger=SWING_PLAN_INVALID, severity="low",
            new_stop=state.current_stop or plan.stop, stop_basis="initial",
            stop_moved=False, high_water=state.high_water,
            r_multiple=0.0, mfe_r=0.0, sell_fraction=0.0,
            message=(f"Plano inválido (entry {plan.entry:.2f}, stop {plan.stop:.2f}) "
                     f"— 1R não é positivo. Gestão automática suspensa."),
        )

    high_water = max(state.high_water or 0.0, current_price, plan.entry)
    r_multiple = (current_price - plan.entry) / r
    mfe_r = (high_water - plan.entry) / r

    # ── Ratchet the stop ─────────────────────────────────────────────
    prev_stop = state.current_stop if state.current_stop > 0 else plan.stop
    new_stop = prev_stop
    stop_basis = "initial"
    reasons: list[str] = []

    if mfe_r >= BREAKEVEN_AT_R and plan.entry > new_stop:
        new_stop = plan.entry
        stop_basis = "breakeven"
        reasons.append(
            f"MFE {mfe_r:.1f}R >= {BREAKEVEN_AT_R:.1f}R → stop a breakeven ({plan.entry:.2f})"
        )
    elif prev_stop >= plan.entry:
        stop_basis = "breakeven"

    if TRAIL_ENABLED and mfe_r >= TRAIL_AT_R and atr and atr > 0:
        trail_stop = high_water - TRAIL_ATR_MULT * atr
        if trail_stop > new_stop:
            new_stop = trail_stop
            stop_basis = "trail"
            reasons.append(
                f"MFE {mfe_r:.1f}R >= {TRAIL_AT_R:.1f}R → trail {TRAIL_ATR_MULT}×ATR "
                f"abaixo do pico {high_water:.2f} = {trail_stop:.2f}"
            )
        elif new_stop > plan.entry:
            stop_basis = "trail"

    stop_moved = new_stop > prev_stop + 1e-9

    def _decide(action, trigger, severity, sell_fraction, message):
        return ExitDecision(
            action=action, trigger=trigger, severity=severity,
            new_stop=round(new_stop, 4), stop_basis=stop_basis,
            stop_moved=stop_moved, high_water=round(high_water, 4),
            r_multiple=round(r_multiple, 2), mfe_r=round(mfe_r, 2),
            sell_fraction=sell_fraction, message=message, reasons=reasons,
        )

    # ── 1. Stop hit ──────────────────────────────────────────────────
    # Checked first and against the RATCHETED stop. A position that ran
    # to +2R and reversed exits at the trail, not at the original -1R.
    if current_price <= new_stop:
        if new_stop <= plan.stop + 1e-9:
            trig, sev = SWING_STOP, "high"
            msg = (f"Stop inicial accionado @ ${current_price:.2f} "
                   f"(stop ${new_stop:.2f}). Perda de {abs(r_multiple):.1f}R — "
                   f"o custo previsto do plano. Fecha e segue.")
        elif abs(new_stop - plan.entry) < 1e-9:
            trig, sev = SWING_BREAKEVEN_STOP, "high"
            msg = (f"Stop breakeven accionado @ ${current_price:.2f}. "
                   f"Chegou a +{mfe_r:.1f}R e devolveu — sai a zero, "
                   f"não a -1R. É o objectivo da regra.")
        else:
            trig, sev = SWING_TRAIL_STOP, "high"
            msg = (f"Trailing stop accionado @ ${current_price:.2f} "
                   f"(stop ${new_stop:.2f}). Fecha com +{r_multiple:.1f}R "
                   f"de um pico de +{mfe_r:.1f}R.")
        return _decide("EXIT", trig, sev, 1.0, msg)

    # ── 2. Target hit (once) ─────────────────────────────────────────
    if not state.partial_taken and current_price >= plan.target:
        # Locking in R means the remainder must never become a loser.
        if new_stop < plan.entry:
            new_stop = plan.entry
            stop_basis = "breakeven"
            stop_moved = True
            reasons.append(f"target atingido → stop sobe para breakeven ({plan.entry:.2f})")
        if PARTIAL_FRACTION >= 1.0:
            # Plan §6 default: order B is a full-size limit. Target hit =
            # trade closed, nothing left to manage.
            return _decide(
                "EXIT", SWING_TARGET_PARTIAL, "med", 1.0,
                (f"Alvo ${plan.target:.2f} atingido @ ${current_price:.2f} "
                 f"(+{r_multiple:.1f}R). Ordem B preenchida — posição fechada, "
                 f"ordem C cancelada automaticamente (OCO)."),
            )
        return _decide(
            "PARTIAL", SWING_TARGET_PARTIAL, "med", PARTIAL_FRACTION,
            (f"Alvo ${plan.target:.2f} atingido @ ${current_price:.2f} "
             f"(+{r_multiple:.1f}R). Realiza {PARTIAL_FRACTION*100:.0f}% e "
             f"sobe o stop para ${new_stop:.2f}; o resto corre."),
        )

    # ── 3. Time stop ─────────────────────────────────────────────────
    # Deliberately AFTER the target check: a position that reaches its
    # target on day 15 should take the partial, not be flushed by the clock.
    if days_held >= plan.time_stop_days:
        return _decide(
            "EXIT", SWING_TIME_STOP, "med", 1.0,
            (f"Time stop: {days_held}d >= {plan.time_stop_days}d sem resolver "
             f"({r_multiple:+.1f}R). A tese era de {plan.time_stop_days} dias — "
             f"capital parado perde opcionalidade."),
        )

    # ── 4. Stop moved ────────────────────────────────────────────────
    if stop_moved:
        return _decide(
            "RAISE_STOP", SWING_RAISE_STOP, "low", 0.0,
            (f"Sobe o stop de ${prev_stop:.2f} para ${new_stop:.2f} "
             f"({stop_basis}). Actual {r_multiple:+.1f}R, pico +{mfe_r:.1f}R."),
        )

    # ── 5. Nothing to do ─────────────────────────────────────────────
    return _decide(
        "HOLD", SWING_HOLD, "low", 0.0,
        (f"{r_multiple:+.1f}R · stop ${new_stop:.2f} ({stop_basis}) · "
         f"alvo ${plan.target:.2f} · dia {days_held}/{plan.time_stop_days}"),
    )


def format_exit_alert(ticker: str, decision: ExitDecision, *, days_held: int = 0) -> str:
    """Telegram message for an actionable decision."""
    icon = {
        SWING_STOP:           "🛑",
        SWING_BREAKEVEN_STOP: "🟡",
        SWING_TRAIL_STOP:     "💰",
        SWING_TARGET_PARTIAL: "🎯",
        SWING_TIME_STOP:      "⏱️",
        SWING_RAISE_STOP:     "⬆️",
    }.get(decision.trigger, "•")

    lines = [
        f"{icon} *{decision.trigger.replace('SWING_', '')} — {ticker}*  [Dia {days_held}]",
        "",
        decision.message,
        "",
        f"`R actual: {decision.r_multiple:+.2f}R  |  pico: {decision.mfe_r:+.2f}R`",
        f"`stop:     ${decision.new_stop:.2f} ({decision.stop_basis})`",
    ]
    if decision.sell_fraction > 0:
        pct = decision.sell_fraction * 100
        lines.append(
            f"\n💡 *Acção:* vende {pct:.0f}% da posição restante em T212."
            if decision.sell_fraction < 1.0
            else "\n💡 *Acção:* fecha a posição em T212."
        )
    elif decision.stop_moved:
        lines.append(f"\n💡 *Acção:* actualiza a ordem de stop para ${decision.new_stop:.2f}.")
    return "\n".join(lines)
