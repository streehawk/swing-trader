# 📡 DipRadar — Honest Quality-Compounder Screener

[![Python](https://img.shields.io/badge/Python-3.12%2B-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://www.python.org/)
[![Railway](https://img.shields.io/badge/Deploy-Railway-000000?style=for-the-badge&logo=railway&logoColor=white)](https://railway.app/)
[![Telegram](https://img.shields.io/badge/Telegram-Bot-26A5E4?style=for-the-badge&logo=telegram&logoColor=white)](https://core.telegram.org/bots)

> **Swing setup engine.** Finds established uptrends pausing into a defined trigger and emits a COMPLETE trade plan — entry (buy stop), stop (1.5×ATR), target (2.5×ATR), 15-day time stop, risk-based size. Execution is always manual by the user in T212.
>
> **Phase 1 pivot (2026-07-27):** the default scorer changed from `watchlist` to `swing`. The watchlist answered a multi-month accumulation question ("which quality businesses are cheap?") via a binding `drawdown ≥ 25%` filter — structurally the opposite posture to swing trading, which trades *with* trend. The watchlist remains available at `DIPRADAR_SCORING_MODE=watchlist` but nothing selects it automatically. See [history](#-history-why-watchlist-and-not-ranking) for the prior pivot.

---

## 📋 What ships today

| Component | Mode | Role |
|---|---|---|
| **Swing setup engine** | ✅ **Default** (`DIPRADAR_SCORING_MODE=swing`) | Uptrend + PULLBACK/BREAKOUT trigger → entry/stop/target/time stop + risk-based size. Earnings inside the window = hard reject. |
| **Watchlist scorer** | ⚠️ Long-term book (`DIPRADAR_SCORING_MODE=watchlist`) | Quality + DD ≥ 25% + catalyst ≤ 30d. Previous default; kept for the long-term thesis. |
| **CORE radar** | ✅ Functional, rule-based | 230 curated quality compounders; provides the signals the watchlist filters |
| **DIP scanner** | ✅ Functional | Detects abrupt drops vs sector; ML inert (`DIPRADAR_USE_ML_SCORE=0` default) |
| **Sector rotation** | ✅ Functional | 24 ETFs ranked monthly, tax-aware via IRS Jovem 7% gate |
| **Momentum scanner** | ✅ Functional (rule-based) | Breakout detector (return_20d ≥ 12%, RSI 52-78, beats sector) |
| **Momentum trainer** | ❌ **RETIRED** | Anti-predictive (CV IC = −0.0042). Stub raises clear retirement error. |
| **Conviction radar** | ✅ Functional | ATR trailing-stop monitor for CORE/MOMENTUM/DIP holdings |
| **Market regime detector** | ✅ Functional | 0-100 score → FROTHY/NEUTRAL/PANIC + cash buffer target |
| **Composite z-score** | ⚠️ Legacy (fallback) | Heuristic z (`DIPRADAR_SCORING_MODE=composite`) |
| **Dislocation z-score** | ⚠️ Experimental | Rule-based 7-signal composite that **failed** F.5 gate (`DIPRADAR_SCORING_MODE=dislocation`) |

**One env switch controls every scoring path**: `/scan_now`, `/analisar`, watchdog cron, `/cash`. Never alert with one logic and respond with another.

---

## 🤖 Commands

### Honest scoring (default = watchlist)

| Command | What it does |
|---|---|
| `/scan_now` | Swing setups now: entry/stop/target/size per name, R:R-sorted. `--full` scans the broad universe. |
| `/analisar <TICKER>` | Full analysis + watchlist criteria check (✅/❌ per criterion). |
| `/regime` | Market regime score + cash-buffer guidance (FROTHY/NEUTRAL/PANIC). |
| `/cash_status` | Compact regime + portfolio + concrete action recommendation. |
| `/cash <eur>` | Allocation proposal. In watchlist mode = equal-weight across qualifiers, concentration cap 30%, regime cash buffer first. |
| `/deployed TICKER AMOUNT [reason]` | Log a real deploy after T212 execution (compares to last `/cash` proposal). |
| `/deploy_history [N]` | Recent `/deployed` history. |
| `/deploy_track [N]` | User picks vs system top picks, live forward returns. |

### Portfolio

| Command | What it does |
|---|---|
| `/sync_portfolio` | Snapshot of current holdings grouped by bucket. |
| `/sync_portfolio TICK:shares:price:BUCKET[:DATE]` | Bulk import positions. |
| `/sync_portfolio --update-avg TICK:...` | Overwrite avg price + shares. |
| `/carteira` | Real-time portfolio snapshot. |
| `/buy TICK PRICE SHARES` / `/sell TICK PRICE [SHARES]` | Manual position updates. |

Buckets: `SWING` (plan-managed trades) · `CORE` (hold-for-life) · `ROTATION` (sector ETFs) · `MOMENTUM` (breakout holdings) · `DIP` (active recovery)

### Sectors and rotation

| Command | What it does |
|---|---|
| `/sectors` | Rotation engine ranking real-time. |
| `/sectors TICKER` | ETF detail. |
| `/sectors holdings` | Your ETFs status. |
| `/admin_calibrate_rotation [years]` | Calibrate rotation alpha (linear / poly / buckets / lightgbm). |

### Conviction (exit signals)

| Command | What it does |
|---|---|
| `/conviction list` | Active positions with trailing stops. |
| `/conviction plan TICKER ENTRY STOP TARGET [DIAS]` | Attach a swing plan (marks the position `SWING`, enables mechanical exits). |
| `/conviction TICKER` | Deep dive. |
| `/conviction close TICKER PRICE [reason]` | Close a position. |
| `/conviction transition TICKER TYPE` | Change bucket (DIP→MOMENTUM→CORE). |

### Admin / introspection

| Command | What it does |
|---|---|
| `/admin_check_config` | Models / scorer / cron state. |
| `/admin_core_debug TICKER` | Debug Core score for a ticker. |
| `/admin_pipeline_status` | Last cron firings + retrains. |
| `/health` | System dashboard. |
| `/backtest` | Backtest summary. |
| `python scripts/run_swing_validation.py` | Re-run the Phase 4 binding gate (CLI, ~5 min for 240 tickers). |
| `/admin_disk` | Disk usage by artifact. |
| `/admin_retrain` | Retrain DIP model (ML inert in watchlist mode but command preserved). |
| `/admin_momentum_retrain` | **RAISES** retirement error (Momentum ML retired). |

---

## 💰 `/cash` in swing mode

```
/cash 1050
```

1. Checks the **weekly circuit breaker** first (plan rule 9): down 6% this week → stop trading, review. Setups are not even shown, so the rule cannot be talked out of.
2. Checks the **market regime**: SPY below SMA50/SMA200 → no new longs today.
3. Sizes each setup so a stop-out costs exactly 1% of the pool: `shares = floor(pool × 1% / (entry − stop))`. **Rounded down** — rounding up would breach the 1% rule on every trade.
4. Applies the caps in order — max 2 open positions → portfolio heat → cash on hand. A cap cuts the *weakest* plans, never an arbitrary slice.
5. Emits each trade as a complete **OTOCO bracket** (order A entry stop-limit, B take-profit, C stop-loss) ready to place.
6. Persists `watchdog_state.json` + `last_cash_proposal.json` (with entry/stop/target, so the nightly sync can reattach the plan and a later fill can be judged against it).

**Why not equal weight:** €500 in a name stopped at −3% risks €15; the same €500 in a name stopped at −8% risks €40. Equal weight is incoherent once stop distances differ. Sizing off the stop is what makes a losing streak survivable and results comparable in R.

No setups today → suggestion is "hold cash". Swing does not force trades: without uptrend + trigger + R:R, the correct position is no position.

*(`DIPRADAR_SCORING_MODE=watchlist` still runs the previous equal-weight allocation across watchlist qualifiers with a 30% concentration cap.)*

---

## 🚨 Auto-alerts (watchdog cron)

| Cron | Time (Lisbon) | What |
|---|---|---|
| DIP scanner hourly | 15h, 16h, 18h, 19h, 20h Mon-Fri | DIP alerts (intra-session) |
| US EOD scan | 21h15 Mon-Fri | DIP + thesis degradation + recovery + weekly DIP scan |
| Momentum scanner | 17h50 + 21h30 Mon-Fri | Rule-based breakout alerts |
| Conviction daily | 22h05 Mon-Fri | CORE/MOMENTUM/DIP trailing-stop monitor |
| **Swing watchdog** | **22h10 Mon-Fri** | **Push when a NEW setup appears — carries the full plan (entry/stop/target) for the NEXT session. Re-pushes when a known setup's entry drifts ≥ 2%.** |
| Heartbeat | 09h00 Mon-Fri | Bot alive check |
| Daily alert reset | 06h00 daily | Reset alerted_today.json |
| Backup | 23h30 daily | data/ backup |

The **watchlist watchdog** is the answer to "auto-alert when something becomes a good investment":
- Runs daily at 22h10 Lisbon — **after the US close (21h00)**, so entries are derived from a completed daily bar. At the old 18h00 slot the bar was still forming and the pushed levels could still move before the bell.
- Scans the CORE universe through the watchlist criteria
- If a ticker NOT in `deployed_tickers` and NOT in `notified_tickers` qualifies → push notification with the full criteria breakdown + headlines
- "Good investment" = passes the watchlist filter, nothing more, nothing less. No alpha promise.

---

## 🏛️ Architecture (post-F.5 reality)

### Scoring dispatcher

`core/scoring_dispatch.py` — `ScoringSession` reads `DIPRADAR_SCORING_MODE` ONCE per scan. Three modes:

```python
DIPRADAR_SCORING_MODE=swing         # DEFAULT — swing setup engine
DIPRADAR_SCORING_MODE=watchlist     # long-term honest filter (previous default)
DIPRADAR_SCORING_MODE=composite     # legacy heuristic z-score
DIPRADAR_SCORING_MODE=dislocation   # experimental, failed F.5 gate
```

### Swing setup engine (v2 — Trading Plan §4)

`core/swing_setups.py` — the seven-gate entry checklist, evaluated in order. Any failure = no trade. Full spec: **[`Phase1_Entry_Filter_Spec.md`](Phase1_Entry_Filter_Spec.md)**.

| # | Gate | Rule |
|---|---|---|
| 1 | **Market regime** | SPY > its own SMA50 **and** SMA200. A hostile market overrides any setup. Fails **closed**. |
| 2 | Universe | price ≥ $10, avg volume ≥ **1M shares**, not in the exclusion list (`PLTR`). Fails **closed** on unknown volume. |
| 3 | Earnings | no report inside the holding window (covers the plan's "not within 10 calendar days" floor). |
| 4 | Uptrend | close > SMA50 > SMA200, SMA50 rising vs 20 bars ago. |
| 5 | **Relative strength** | stock's 63-bar return > SPY's. Buy leaders, not laggards. |
| 6 | **Valid pause** | pullback 1–3×ATR off the 20d high, on **declining volume**, never closing below SMA50. |
| 7 | Structure ≥ 2R | entry above the pause high, stop below the pause low (or entry − 1 ATR), target at 2R — and that 2R move must be plausible (≤ 4×ATR). |

**The plan:**
```
entry  = pause high × 1.001     (buy-stop trigger)
limit  = entry × 1.002          (slippage cap)
stop   = min(pause low × 0.999, entry − 1 × ATR)   ( = 1R )
target = entry + 2 × risk
time   = 15 trading days
```

**BREAKOUT setups were removed in v2**, per the plan's own journal lesson: *"a breakout chased the day after a strong run is prone to shakeout… wait for the pullback entry instead of chasing the high."*

Bold rows are new in v2 and exist because v1 lacked them and failed validation — see [the verdict](#-phase-4-verdict-the-v2-filters-help-but-the-edge-is-still-zero).

**Cost profile:** gates 1-4 read only OHLCV bars — no yfinance `info`, no fundamentals, no SEC, and **no LLM**. The earnings/sector lookups run only on names that already triggered (typically 0-8 of several hundred). The LLM drop-attribution classifier is not invoked at all in swing mode: it exists to explain why something *fell*, and swing buys strength. This path is materially cheaper per scan than watchlist mode.

**Validated and FAILED — see [Phase 4 verdict](#-phase-4-verdict-swing-entries-have-no-measured-edge) below.** Treat output as a candidate list requiring your judgement, not as an edge claim.

### Swing exit engine

`core/swing_exits.py` — a pure state machine shared by **both** monitors (`core/conviction_radar.py` for user-held positions, `portfolio/position_monitor.py` for bot-created ones), so the two paths cannot disagree about where a stop is.

Everything is expressed in **R**, where 1R = `entry − initial stop`. R is the only unit in which a 3%-stop trade and an 8%-stop trade are comparable, and it is what Phase 4's gate is measured in.

| Priority | Trigger | Rule |
|---|---|---|
| 1 | `SWING_STOP` / `SWING_BREAKEVEN_STOP` / `SWING_TRAIL_STOP` | price ≤ effective stop → close. The three names are distinct so a −1R loss is never reported as a trail exit. |
| 2 | `SWING_TARGET_PARTIAL` | price ≥ target, once → sell 50%, stop lifted to at least breakeven |
| 3 | `SWING_TIME_STOP` | `days_held ≥ time stop` → close remainder |
| 4 | `SWING_RAISE_STOP` | stop ratcheted this tick → move the resting order in T212 |

**The stop ratchets and never loosens:** initial (plan stop) → **breakeven** at MFE ≥ 1.0R.

v2 matches the plan's **OTOCO bracket** (§6): order A entry, order B full-size take-profit at 2R, order C stop — B and C paired OCO. That bracket can express a break-even ratchet (modify C) but *not* a continuous trail or a half-size scale-out, so the ATR trail is **off** (`SWING_TRAIL_ENABLED=1` re-enables) and the target closes the **whole** position. The Phase 4 diagnostic found the v1 partial made no measurable difference, so nothing is lost by matching the model you can actually place.

The ratchet is driven by **MFE** (max favourable excursion), not current price. Using current price would let a position that ran to +1.8R and pulled back to +0.4R silently keep its original stop — the giving-back case breakeven exists to prevent. A missing ATR skips the trail rather than guessing, so a data failure can never *loosen* a stop.

**Trading days vs calendar days:** the plan's time stop is in bars (15); both position records measure age in calendar days. `trading_days_to_calendar()` converts (15 → 21), otherwise the time stop would fire ~30% early.

**Mechanical exits bypass `never_sell`.** A stop you can opt out of is not a stop, and R-multiple accounting is meaningless if stops are optional.

**Getting a plan onto a position:**
- automatic — `/cash` writes entry/stop/target into `last_cash_proposal.json`; when you fill the order and the nightly auto-sync runs, the plan is reattached and the position is created as `SWING` (age-capped at `SWING_PLAN_RECOVERY_DAYS`, default 10)
- manual — `/conviction plan TICKER ENTRY STOP TARGET [DIAS]`

### Phase 3 retuned defaults

These were calibrated for multi-month holds and would have managed a 15-day plan on a 120-day leash. All remain env-overridable — set them back if you run the long-term book.

| Constant | Was | Now | Env var |
|---|---|---|---|
| Conviction ATR trail multiple | 2.5 | **1.5** | `CONVICTION_ATR_MULTIPLIER` |
| Conviction ATR stop floor | 8% | **4%** | `CONVICTION_ATR_MIN_STOP_PCT` |
| Conviction trail fallback | 10% | **7%** | `CONVICTION_TRAIL_FALLBACK_PCT` |
| DIP time decay | 120d | **21d** | `CONVICTION_DIP_TIME_DECAY_DAYS` |
| Position stop-loss | 12% | **8%** | `POSITION_STOP_LOSS_PCT` |
| Momentum trailing stop (fallback) | 12% | **8%** | `MOMENTUM_TRAILING_STOP_PCT` |
| Momentum ATR multiple | 2.5 (hardcoded) | **1.5** | `MOMENTUM_ATR_MULTIPLIER` |

Swing exit tunables: `SWING_BREAKEVEN_AT_R` (1.0), `SWING_TRAIL_AT_R` (1.5), `SWING_TRAIL_ATR_MULT` (1.5), `SWING_PARTIAL_FRACTION` (0.5).

Backward-compat: `DIPRADAR_USE_DISLOCATION=0/1` still respected for one more release (will be removed).

### Honest watchlist

`core/honest_watchlist.py` — three filter criteria, sector-relative as **display column**:

| Criterion | Threshold | Source |
|---|---|---|
| Quality gate | revenue_growth ≥ −10%, gross_margin ≥ 25%, FCF ≥ 0 (or investment phase) | `core_radar._score_core_dip` |
| Drawdown depth | dd_from_52w_high ≤ −25% | price history |
| Catalyst proximity | days_to_earnings in [−10, +30] | yfinance earnings calendar |

Sector-relative (60d) is computed via `data/sector_relative.py` and **shown** in every entry but **never used to exclude**. Per the design discipline: removing it would re-shut the door on absolute drops (ADBE-style); showing it lets the user visually triage "fell with market" (sector-rel ≈ 0) from "fell alone" (sector-rel deeply negative).

### Tax engine (`core/tax_engine.py`)

- IRS Jovem 75% exemption default (effective CGT 7%)
- Override via `EFFECTIVE_CGT_PCT` env var
- `should_rotate(unrealized_gain, current_score, target_score)`:
  - Override structural: `exit_strength > 70`, `secular_risk > 60`, `current_score < 30`
  - Loss positions → always rotate (loss = tax deduction)
  - Profit → `score gap ≥ tax_cost_pct × TAX_FRICTION_MULTIPLIER (500)`

### Market regime (`core/market_regime.py`)

0-100 score from 5 components (VIX percentile, SPY vs MA200d, quality_dd, sectoral divergence, SPY 3m). Output:
- **FROTHY** (broad market expensive) → hold cash buffer high
- **NEUTRAL** (default) → normal deploy
- **PANIC** (generational buying zone) → deploy aggressively

Surfaced in `/cash` header and used to compute the cash buffer in watchlist allocation.

### Sector rotation (`core/sector_radar.py`)

- 24 ETFs (sector SPDR + thematic)
- Backtest validated: +15pp alpha vs SPY in 4 years with monthly rebalance
- Calibration multi-method: linear → polynomial → buckets → lightgbm (best CV r²)
- US tickers for data, UCITS mapping for deploy

### Conviction radar (`core/conviction_radar.py`)

- ATR trailing stops (12-21% depending on volatility)
- Structural decline + 8-K event triggers
- Daily check 22h05 Mon-Fri
- Auto-sync with `/sync_portfolio`

### Dislocation scorer (experimental, failed)

`core/dislocation_score.py` — 7 sub-scores (estimate revisions, guidance, drop attribution, catalyst, valuation, quality, governance) composed into a z-score. Built per the plan to differentiate "fear-driven dislocation" from value traps. **F.5 backtest verdict**: REVERT — top-decile underperformed SPY by 1.09pp, worst-fold −22.48pp in 2025 (AI-led regime broke mean reversion). Code retained behind env flag for future re-validation if PIT data improves (e.g., subscribing to Tiingo Fundamentals).

---

## ⚙️ Env vars

### Required

| Var | Description |
|---|---|
| `TELEGRAM_TOKEN` | Bot token |
| `TELEGRAM_CHAT_ID` | Chat ID |
| `MONTHLY_BUDGET_EUR` | Monthly inflow (e.g. `1050`) |
| `TZ` | `Europe/Lisbon` |

### Scoring + allocation

| Var | Default | Description |
|---|---|---|
| `DIPRADAR_SCORING_MODE` | `swing` | `swing` (ship) / `watchlist` (long-term) / `composite` (legacy) / `dislocation` (experimental) |
| `DIPRADAR_USE_DISLOCATION` | unset | Legacy flag (=0 → composite, =1 → dislocation); deprecated, prefer `DIPRADAR_SCORING_MODE` |
| `DIPRADAR_USE_ML_SCORE` | `0` | ML scoring inert by default (set `=1` for legacy DIP ML path) |
| `EFFECTIVE_CGT_PCT` | `0.07` | Effective CGT (IRS Jovem 75% = 7%) |
| `SECTOR_CONCENTRATION_CAP` | `0.35` | Sector cap (legacy reference) |

### Swing mode (`DIPRADAR_SCORING_MODE=swing`)

| Var | Default | Description |
|---|---|---|
| `SWING_RISK_PCT` | `0.01` | Risk per trade as fraction of the **swing pool** (plan rule 1) |
| `SWING_MAX_NEW_POSITIONS` | `2` | Max open positions (plan rule 4) |
| `SWING_MAX_HEAT` | `0.02` | Cap on total open risk (= 2 × 1%) |
| `SWING_WEEKLY_CIRCUIT_BREAKER` | `0.06` | Weekly loss that halts trading (plan rule 9) |
| `SWING_REGIME_GATE` | `1` | `0` disables the SPY regime gate (testing only) |
| `SWING_MIN_PRICE` | `10.0` | Minimum share price |
| `SWING_MIN_AVG_VOLUME` | `1000000` | 20d avg volume floor, in **shares** |
| `SWING_EXCLUDED_TICKERS` | `PLTR` | Comma-separated permanent exclusions |
| `SWING_RS_LOOKBACK` | `63` | Bars for the relative-strength comparison vs SPY |
| `SWING_PULLBACK_MIN_ATR` / `_MAX_ATR` | `1.0` / `3.0` | Pause depth band |
| `SWING_VOLUME_DECLINE_RATIO` | `1.0` | Pause volume must be ≤ this × prior average |
| `SWING_TARGET_R_MULT` | `2.0` | Target distance in R |
| `SWING_MIN_RR` | `2.0` | Minimum reward:risk |
| `SWING_MAX_TARGET_ATR` | `4.0` | Reject if the 2R target exceeds this many ATR |
| `SWING_MAX_STOP_PCT` | `0.08` | Reject if 1R exceeds this share of entry |
| `SWING_TIME_STOP_DAYS` | `15` | Hard time stop, in trading days |
| `SWING_EXCLUDE_EARNINGS` | `1` | `0` disables the earnings reject |
| `SWING_EARNINGS_MIN_DAYS` | `10` | Calendar-day floor before a report |

### Risk + execution

| Var | Default | Description |
|---|---|---|
| `POSITION_STOP_LOSS_PCT` | `0.12` | DIP positions stop-loss |
| `MIN_DIP_SCORE` | `50` | Minimum DIP score for alert |
| `INLINE_TICKER_TIMEOUT` | `25` | Per-ticker download timeout |

### APIs

| Var | Source | Description |
|---|---|---|
| `TIINGO_API_KEY` | api.tiingo.com | EOD prices |
| `FMP_API_KEY` | financialmodelingprep.com | Analyst estimates (free tier, used by experimental dislocation mode) |
| `FRED_API_KEY` | fred.stlouisfed.org | Macro: recession probability |

---

## 🚀 Cold start

1. Configure Railway env vars (above).
2. Deploy → bot starts automatically.
3. Telegram: `/admin_check_config` to validate config.
4. First time: `/sync_portfolio TICK:shares:price:BUCKET` to register existing positions.

### Monthly cadence

1. Receive inflow (~€1050).
2. `/cash 1050` → watchlist proposal (equal-weight across qualifiers).
3. Execute manually in T212.
4. `/deployed TICKER AMOUNT reason` for each actual purchase (compares to proposal).
5. Watchdog cron at 22h10 Lisbon notifies if a new name qualifies mid-month.

---

## 🔬 Phase 4 verdict: the v2 filters help, but the edge is still zero

**Decision: REVERT** (v2, 2026-07-28). Output: `data/validation_cache/swing_validation_2026-07-28.json`.

`scripts/run_swing_validation.py` replays the shipping code — `core/swing_setups.evaluate_bars` for entries, `core/swing_exits.evaluate_swing_exit` for the ratchet — bar by bar over 240 tickers, 2020-10 → 2026-06, against a gate registered before the first run. **The gate was not relaxed between v1 and v2.**

### v1 → v2

**Run under ticket P1-7 conditions** — see [P1-7 compliance](#p1-7-compliance) for the acceptance log.

| Metric | v1 | **v2 (P1-7)** | Gate | |
|---|---|---|---|---|
| Trades | 4,193 | **666** | ≥ 200 | ✅ |
| **Expectancy** | −0.017R | **−0.009R** | ≥ +0.25R | ❌ |
| Win rate | 34.5% | **36.2%** | ≥ 40% | ❌ |
| Avg win / loss | +1.48 / −0.80R | **+1.49 / −0.86R** | | |
| t-stat | −0.85 | **−0.17** | ≥ 2.0 | ❌ |
| Max drawdown | 26.3% | **12.5%** | ≤ 15% | ✅ |
| Worst year | 2022, −0.245R | **2025, −0.280R** | ≥ −0.10R | ❌ |

Per P1-7 item 4 this run holds sizing (0.75%), the Phase-3 exits (trail on, 50% partial) and the 15-bar time stop constant — **only the entry filter differs** from the baseline. Drawdown now passes, entirely because the Phase-3 trailing exits cap it; expectancy is unchanged at zero.

*(As-shipped with the plan's OTOCO exits — full exit at target, no trail — the same entries give −0.023R on 741 trades at 30.2% win rate. Exits reshuffle win rate against payoff; neither configuration moves expectancy off zero.)*

The filters did what they were designed to do — they cut 82% of trades, removed the 2022 bloodbath (504 trades → 21; total damage −123.5R → −5.7R), and raised the payoff ratio to 2.2:1. Expectancy did not move off zero.

**Every configuration sits on its own breakeven line:**

| Configuration | breakeven win rate | actual | gap |
|---|---|---|---|
| v1 entry, Phase-3 exits | 35.09% | 34.5% | −0.6pp |
| **v2 entry, Phase-3 exits** | **36.60%** | **36.2%** | **−0.4pp** |
| v2 entry, OTOCO exits | 31.16% | 30.2% | −1.0pp |

Change the entry filter, change the exits, change the target multiple — the win rate moves by exactly the amount that keeps expectancy at zero. **These changes move the system along the risk/reward frontier without lifting it off it**, which is the signature of a signal carrying no directional information.

### What the stop-limit costs

Order A is a Stop-**Limit** (`limit = trigger + 0.2%`). Modelling it correctly — a gap beyond the limit that never returns is *no trade* — removed 35 entries. Those 35 averaged **+0.426R with a 68.6% win rate.**

A stock that gaps hard through your limit and never looks back is in a powerful move: exactly the kind that runs to target. The 0.2% limit protects you from chasing bad fills and simultaneously refuses the strongest continuations. On this sample it cost **−0.020R** — the difference between v2 measured as a stop-market (−0.003R) and as the stop-limit you will actually place (−0.023R).

Reported as an observation, **not** a recommendation to widen it: post-hoc parameter changes on a sample sitting at zero is precisely the "tuning to chase a result" the plan forbids.

### Which gate did the work

Ablation with `SWING_REGIME_GATE=0` (everything else identical):

Ablation under identical P1-7 conditions — `SWING_REGIME_GATE=0`, everything else held constant:

| Configuration | Expectancy | Trades | 2022 | 2026 |
|---|---|---|---|---|
| Regime gate **ON** | **−0.009R** | 666 | 17 trades, −3.4R | +0.133R |
| Regime gate OFF | −0.075R | 911 | 101 trades, −20.2R | −0.171R |

**The market-regime gate is worth +0.066R on its own** — by far the most valuable rule in the set, and the one that neutralised 2022 (101 trades → 17, damage −20.2R → −3.4R). Plan rule 11 earns its place; it is simply not enough alone to reach +0.25R.

By year (v2, P1-7): 2021 −0.063R · 2022 −0.198R · 2023 −0.082R · 2024 **+0.154R** · 2025 **−0.280R** · 2026 **+0.133R**. Note 2025 got *worse* per trade than in v1 (−0.041R → −0.280R): the filters concentrated that year into fewer, worse trades.

**Caveat on item 4:** a like-for-like v1-vs-v2 *entry* comparison is not possible — the v1 entry code was replaced, not kept behind a flag. The regime ablation above is the controlled comparison actually available; the v1 column in the headline table was measured under the older cost model and stop-market fills, so treat it as indicative, not exact.

### Capacity

At the plan's `max 2 open positions`, the portfolio sim took 171 of 741 signals — a **77% rejection rate**. Per-trade expectancy describes a system you cannot fully run; the trades you actually get are a first-come subset, not the best ones.

### P1-7 compliance

Every run now logs the ticket's acceptance criteria into its output JSON:

```
Universe : SURVIVOR (241 tickers, 0 delisted)
           !! Every result is an UPPER BOUND, not a measured edge
Costs    : commission $0.00/side · slippage 10bps/side on STOP fills · tick floor $0.01
Sizing   : risk 0.75%/trade · max 2 concurrent · time stop 15 bars
Verifying no look-ahead... PASS (19 trades compared)
```

| Item | Status |
|---|---|
| **1. Survivorship** | ❌ **Not fixable here** — no delisted dataset available. Ticket's fallback taken: `universe_provenance()` classifies the universe **empirically** (counts tickers whose series ends early), the type is logged, and `evaluate_gate()` downgrades any GO on a survivor universe to `GO_UPPER_BOUND_ONLY`. The bias cannot be silently forgotten. |
| **2. Costs + slippage** | ✅ `CostModel` charges commission and slippage **separately**. Slippage 10bps/side (ticket range 5–15), floored at `max(bps, 1 tick)`, applied to **stop and market fills only** — a take-profit limit fills at your price or not at all, so charging it there would overstate costs. v1 charged nothing on stop exits, which understated them badly: a stop-out now costs **more** than 1R. |
| **3. No look-ahead** | ✅ **Verified at runtime, not asserted.** `confirm_no_lookahead()` replays tickers, appends 40 bars of a fabricated 3× rally, and requires every already-closed trade to be byte-identical. A failure sets the verdict to `INVALID`. |
| **4. Hold else constant** | ✅ 0.75% sizing, Phase-3 exits, 15-bar time stop — all logged in `config.exits`. ⚠️ A true v1-vs-v2 entry A/B is not possible (v1's entry code was replaced); the regime ablation is the controlled comparison available. |

**Still not modelled:** real spread (the cache is OHLCV only, so slippage is a parameter not a measurement) · borrow, halts, limit-up · no PIT earnings calendar, so the backtest takes trades the live engine would reject. All inflate results.

### ⚠️ Survivorship bias is NOT fixed (P1-7 item 1)

`data/validation_cache/prices.parquet` carries 241 tickers and **zero of them delist** — every series runs to 2026-06-26. Its own provenance file admits it:

```json
"spine_construction": "today_list_applied_backward",
"survivorship_inflation": true,
"trap_avoidance": "untested"
```

The universe is today's CORE quality list projected backward. Every company that went bankrupt, was acquired, or simply deteriorated out of the quality screen between 2021 and 2026 is **absent** — and those are disproportionately the losers a pullback system would have bought into.

This is the single largest unquantified error in the result, it points **upward**, and closing it needs a data source with point-in-time index membership including dead tickers (CRSP, Sharadar, Norgate — all paid). Until then, treat **every** number above as an optimistic bound: the true expectancy is below −0.009R by an unknown margin.

Per the ticket's own fallback clause, the bias is now **machine-enforced rather than documented in prose**: the universe type is detected empirically and logged in every run, and the gate cannot issue an unqualified GO on a survivor universe.

### What this means

- The swing engine ships as a **candidate generator with disciplined risk management**, not an alpha source. No edge claim.
- ML ranking on top of these entries is **not** authorised — that step was conditional on this gate, and layering a model over a zero-expectancy signal would repeat F.5. `DIPRADAR_USE_ML_SCORE` stays `0`.
- Per the plan's own guardrail — *"No tuning rules mid-stream to chase a result"* — the thresholds will not be adjusted until the gate passes. v2's rules came from the written spec; the gate stayed fixed. That is the whole point.
- Two honest options remain: a genuinely different entry hypothesis under its own pre-registered gate, or accepting that this universe and this data do not support a mechanical edge, and running the swing pool small and by hand.

---

## 🔬 Edge-search protocol — H1 postmortem (NO-EDGE)

**H1 — panic mean reversion in quality large caps.** Buy a liquid name that is still above its SMA200 but has flushed ≥ 2.5 ATR over ten sessions with ≥ 3 of the last 4 closes down; bid a LIMIT 0.5 ATR under the close; exit on the first close above SMA5, a 2 ATR hard stop, or 7 bars. Registered in `edge_search_protocol.md` with no target and no break-even ratchet — mean reversion takes many small wins, and Phase 1's 2R shape was pre-registered out rather than dropped after seeing a result. Run on the original 229-name quality list (SURVIVOR data), 273,654 bars, 2021-08 → 2026-06. **1,509 trades, 63.5% win rate, expectancy +0.028R after 10bps costs, PF 1.110, t = +1.64, and the sign held in both the train and test halves.** That is the first genuinely non-zero, correctly-signed result this project has produced — and it still fails the pre-registered gate, which asked for ≥ +0.10R and PF ≥ 1.2. Three things explain the shortfall and none of them are fixable by tuning: the whole 2022 bear year printed **−0.116R at t = −2.55**, because a strategy with its regime gate registered OFF buys every step of a sustained downtrend; the signal is heavily clustered (19 names can flush on one day, 37% of trades land on days with more than five signals), so under the live 2-position cap **75% of signals are rejected and the 377 trades you could actually take expectancy out at −0.009R while the 1,132 you were capped out of made +0.040R**; and the universe is survivor data, so the true number sits below the measured one by an unknown margin. Per the protocol, H1 closes as **NO-EDGE** (0 < expectancy < +0.10R). Not FAIL — the effect appears to be real, just too small to pay for its own capacity constraint. Nothing is deployed; nothing was tuned.

| Run | #Trades | Win% | AvgWin R | AvgLoss R | Expectancy | Breakeven Win% | PF | t |
|---|---|---|---|---|---|---|---|---|
| costs OFF | 1509 | 64.6% | +0.46 | −0.69 | **+0.049** | 60.3% | 1.200 | +2.90 |
| costs ON (10bps) | 1509 | 63.5% | +0.44 | −0.69 | **+0.028** | 61.0% | 1.110 | +1.64 |
| stress (15bps) | 1509 | 62.8% | +0.44 | −0.69 | **+0.017** | 61.3% | 1.067 | +1.01 |
| train (≤ 2025-01-05) | 1076 | 63.6% | +0.43 | −0.69 | **+0.023** | 61.5% | 1.093 | +1.18 |
| test (> 2025-01-05) | 433 | 63.3% | +0.47 | −0.70 | **+0.039** | 59.9% | 1.152 | +1.20 |

Code: `strategies/panic_reversion.py` (entry), `core/h1_backtest.py` (limit-fill simulator + H1 exit ladder), `tests/test_panic_reversion.py` (54 tests), `scripts/run_h1_validation.py` (the ladder). Phase 1 and Phase 1B entries are untouched. The closed-end-fund blacklist the Phase 1B postmortem asked for now lives in `strategies/filters.py` and is applied by every strategy.

---

## 🔬 Edge-search protocol — H2 postmortem (FAIL)

**H2 — strong-gap continuation.** The only hypothesis in this project generated from its own data: in Phase 1, the 35 trades that gapped beyond the stop-limit cap — the ones the system refused to chase — won 68.6% at +0.43R. Registered interpretation: a gap violent enough to blow through a chase guard reflects real flow that continues for days. The protocol was explicit that those 35 trades were noticed post-hoc, are a spent sample, and prove nothing; H2 re-tests the idea on a fresh pre-registered configuration. Rules: open gaps ≥ 1 ATR above the prior close AND clears the 20-day high AND closes on RVOL ≥ 2, with yesterday's close > SMA50 > SMA200 and SPY > SMA200; buy the NEXT open at market, one day late, so only closed bars are used; exit on the first close below SMA10, a 1.5 ATR stop, a 15-bar time stop, break-even ratchet at +1R, no target. Universe: the 407-name $2B+ screen, 395 tradeable. **180 trades, 29.4% win rate, expectancy −0.069R after costs, PF 0.838, negative in BOTH splits.** The founding observation is refuted: 29.4% is not 68.6%, and the strategy is already at −0.012R *before* costs, so 35 trades' worth of apparent edge was selection and noise. The shape is right — the SMA10 trail does what it was registered to do, letting winners average +1.21R against −0.60R losers — but the hit rate is nowhere near the 33.2% the payoff needs. Slippage is charged per the protocol's rule 4: 15bps for the 222 names under 1M median volume, 10bps for the 173 above, and at a uniform 15bps the result degrades to −0.100R, which matters because H2 buys the opening auction of a gap day, the worst liquidity moment in a session. Per the protocol, H2 closes as **FAIL** (expectancy ≤ 0). Nothing was tuned; nothing is deployed.

| Run | #Trades | Win% | AvgWin R | AvgLoss R | Expectancy | Breakeven Win% | PF | t |
|---|---|---|---|---|---|---|---|---|
| costs OFF | 180 | 30.6% | +1.22 | −0.56 | **−0.012** | 31.2% | 0.968 | −0.16 |
| costs ON (10/15bps) | 180 | 29.4% | +1.21 | −0.60 | **−0.069** | 33.2% | 0.838 | −0.89 |
| stress (15bps flat) | 180 | 28.3% | +1.17 | −0.60 | **−0.100** | 34.0% | 0.768 | −1.35 |
| train (≤ 2024-12-24) | 139 | 30.9% | +1.11 | −0.57 | **−0.054** | 34.2% | 0.864 | −0.65 |
| test (> 2024-12-24) | 41 | 24.4% | +1.65 | −0.69 | **−0.118** | 29.4% | 0.773 | −0.61 |

**The by-product worth keeping: a real point-in-time earnings calendar.** H2's rules exclude earnings gaps specifically so it does not silently become H3 (post-earnings drift is a different hypothesis with a different counterparty). Every earlier run in this project disabled the earnings gate for want of a calendar. `scripts/fetch_earnings_dates.py` now builds one from **EDGAR 8-K item 2.02** filings — free, filed on the day, genuinely point-in-time — covering **395/407 tickers (97.1%), 9,913 filings**, with 381 names at full quarterly coverage. It earned its keep immediately: of the 680 bars that passed every other H2 gate, **500 (73.5%) were earnings reactions**. Without the calendar H2 would have run ~680 trades, three-quarters of them PEAD, under the wrong hypothesis' name. Names the calendar cannot cover (10 foreign issuers filing 6-K rather than 8-K) are excluded rather than waved through, because gating some tickers and not others biases the sample by ticker.

Code: `strategies/gap_continuation.py` (entry), `core/h2_backtest.py` (market-entry simulator + SMA10 trail), `core/protocol_gate.py` (the shared H1–H3 gate, lifted out so three hypotheses cannot drift into three definitions of "passed"), `tests/test_gap_continuation.py` (53 tests), `scripts/run_h2_validation.py`, `scripts/fetch_earnings_dates.py`.

---

## 📚 History: why watchlist and not ranking?

DipRadar was built around an ML-ranked alpha thesis. Through Q2 2026 we shipped:

- DIP scanner with ML alpha prediction (CV IC = +0.063 — weak)
- Core radar with ML alpha (IC = +0.119 — better but unvalidated at top decile)
- Momentum ML model (CV IC = −0.0042 — **anti-predictive**, kept for retraining)
- Sector rotation with calibrated linear/poly/bucket alpha (r² ≤ 0.007 — noise)
- Composite z-score combining quality + drawdown + valuation
- Dislocation z-score (rule-based 7-sub-score composite)

The **F.5 backtest gate** (binding pre-registered: top-decile 6m return ≥ SPY + 500bps AND hit-rate ≥ 65% AND worst-fold ≥ −500bps, run on PIT-clean spine 2021-2026) produced:

- top-decile_6m_excess = **−1.09pp** (need ≥ +5pp)
- hit_rate = 59.3% (need ≥ 65%)
- worst-fold = **−22.48pp** in 2025Q4 (need ≥ −5pp)

All three criteria failed. The signals that distinguish "fear" from "deterioration" — estimate revisions, news guidance, forward valuation — need PIT historical data we don't have freely. Without them, ranking confused dislocations with value traps. The 2025 AI-led regime broke mean-reversion entirely.

Per the binding rule: **REVERT**. Default scoring switched to watchlist. The honest version filters for quality + drawdown + catalyst and shows sector-relative as a column for the user to judge. No alpha claim, no edge ranking, no pretending to know which name will win. **Útil sem mentir.**

The ML files are retained for one transition release; the Momentum trainer is fully retired (stubbed).

---

## 📁 Layout

```
DipRadar/
├── main.py                          # Entry + APScheduler crons
├── bot_commands.py                  # Telegram dispatcher
├── state.py                         # JSON persistence
├── config.py                        # Central config
├── core/
│   ├── swing_setups.py             # Swing setup engine (DEFAULT scorer)
│   ├── swing_exits.py              # Exit state machine (stop/breakeven/trail/time)
│   ├── swing_backtest.py           # Replay engine + pre-registered gate
│   ├── honest_watchlist.py         # Long-term watchlist (previous default)
│   ├── scoring_dispatch.py          # One env switch, 4 modes
│   ├── dislocation_score.py         # Experimental rule-based scorer (F.5 failed)
│   ├── composite_score.py           # Legacy heuristic z-score
│   ├── core_radar.py                # Quality compounder screening
│   ├── sector_radar.py              # Sector rotation
│   ├── conviction_radar.py          # Exit signals
│   ├── market_regime.py             # FROTHY/NEUTRAL/PANIC
│   └── tax_engine.py                # IRS Jovem rotation gate
├── data/
│   ├── fundamental_signals.py       # SEC EDGAR + Form 4 + 8-K
│   ├── sector_relative.py           # Ticker vs sector ETF return
│   ├── valuation_percentile.py      # P/E vs own 3y range
│   ├── tiingo_client.py             # EOD prices
│   └── validation_cache/            # F.5 backtest cache (committed for reproducibility)
├── ml/                              # Inert ML (DIPRADAR_USE_ML_SCORE=0 default)
├── ml_training/                     # Training infra (retired but kept)
├── momentum_radar/
│   ├── scanner.py                   # Rule-based breakout detector (FUNCTIONAL)
│   ├── trainer.py                   # RETIRED stub (anti-predictive)
│   └── scripts/build_dataset.py     # RETIRED stub
├── scanners/                        # DIP scanner + alert DB
├── portfolio/                       # Position tracking
├── utils/
│   ├── news_headlines.py            # yfinance news with as_of_date PIT filter
│   ├── http_timeout.py              # Monkey-patch yfinance to add timeouts
│   └── ...
├── scripts/                         # Build/calibrate/validate one-shot scripts
└── tests/                           # pytest suite (55 tests post-watchlist ship)
```

Module docstrings carry the technical detail per file.
