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

### Honest scoring (default = `swing` since 2026-07-27; `watchlist` via `DIPRADAR_SCORING_MODE=watchlist`)

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

## 🧾 Research ledger — every hypothesis, every verdict

One table, in the order things were run. Each row links to its own postmortem
below. **Nothing in this ledger is deployed.** No threshold was moved after a
result was seen, in either direction.

| # | Hypothesis | Instrument | Sample | Verdict | Headline |
|---|---|---|---|---|---|
| F.5 | ML alpha ranking | 241 quality names | 2021–2026 | ❌ **REVERT** | top-decile 6m excess −1.09pp vs +5pp needed |
| P1 | Trend pullback (`trend_pullback`) | 240 names | 666 trades | ❌ **NO EDGE** | −0.009R; 0.4pp under its own breakeven |
| P1B | Dip in uptrend (`dip_in_uptrend`) | 3 universes | **0 trades** | ❌ **FAIL** | signal scarcity, not measured losses |
| H1 | Panic mean reversion (`panic_reversion`) | 229 names | 1,509 trades | ⚠️ **NO-EDGE** | +0.028R — real but capacity-bound |
| H2 | Gap continuation (`gap_continuation`) | 395 names | 180 trades | ❌ **FAIL** | −0.069R; founding observation refuted |
| C1 | Connors RSI(2) < 5 (`rsi2`) | SPY | 80 trades | ⚠️ **MARGINAL** | best per trade, 80 < 100-trade floor |
| **C2** | Connors cum-RSI < 35 (`rsi2`) | **SPY** | **146 trades** | ✅ **PASS** | **+0.436%/trade, PF 2.095, t=+3.20** |
| **C3** | Connors cum-RSI < 50 (`rsi2`) | **SPY** | **207 trades** | ✅ **PASS** | **+0.336%/trade, PF 1.836, t=+2.98** |
| A1 | C2 replication on a 12-ETF basket | 12 ETFs | 1,750 trades | ✅ **G1/G2/G3 PASS** | 12/12 positive, +0.330%/trade pooled |
| H3 | Post-earnings drift (PEAD) | 395 names | — | 🕓 **QUEUED** | data precondition now met (EDGAR, 97.1%) |
| H4 | Regime trend-following | SPY + QQQ | — | 🕓 **QUEUED** | own gate: drawdown, not R-expectancy |

### Reading the two PASSes honestly

C2 and C3 are the project's first pre-registered passes, and they are trustable
in a way nothing before them was: SPY is a single continuously-listed ETF, so
there is **no survivorship bias, no signal clustering, no capacity problem, and a
2bps cost assumption that is actually defensible**. On 18.6 years of genuinely
out-of-sample data (2008–2026 — the book was published on 1993–2007) the gate is
cleared on every bar.

Three caveats belong in the same breath as the verdict:

1. **The edge is small.** +0.336% to +0.436% per trade after costs. That is real,
   but it is not a large number, and it will not survive careless execution.
2. **It has been decaying since publication.** Per-trade edge roughly halved
   post-book: C1 +1.050% → +0.528%, C2 +0.814% → +0.436%, C3 +0.660% → +0.336%,
   with win rates down 6–8pp. The direction of that trend is unfavourable.
3. **It does not beat buy-and-hold on return.** 2.4–4.1% CAGR against SPY's
   11.24%, because it holds SPY only 5.6–16.5% of the time. It beats buy-and-hold
   only on a **drawdown-adjusted** basis — 14.0–14.9% max drawdown vs SPY's
   51.9%, so MAR is modestly better (C3 0.28 vs 0.22). This is a cash-deployment
   rule, not a replacement for owning the index, and it is only interesting if
   the idle 84–94% of the time earns something.

Full detail: [C-series postmortem](#-c-series-postmortem--c2-and-c3-pass-the-projects-first).

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

## 🔬 Phase 1B postmortem — dip in an uptrend (FAIL, zero trades on three universes)

**P1B — buy the quiet pullback inside a long-term uptrend**, registered in
`Phase1B_Reversal_Strategy_Spec.md` (tickets P1B-1 … P1B-7). Rules: close > SMA50 >
SMA200, average volume ≥ 200k, a ≥ 5% pop over SMA20 in the recent past, RVOL > 1.5
on the signal bar, and a structure offering ≥ 2R to the prior swing high. The
strategy **produced zero trades on every universe it was given**, so the verdict is
FAIL by the trade-count gate (≥ 300) rather than by measured losses. Output:
`data/validation_cache/phase1b_validation_2026-07-29.json`.

A zero-trade result is worthless without knowing whether it is scarcity or a bug, so
each run logged a full **gate census** — every evaluable bar attributed to the first
gate it failed (the gate order is cheapest-first, so this is well-defined):

| Universe | How it was built | Tickers | Bars | Reached structure gate | Trades |
|---|---|---|---|---|---|
| CORE quality (original) | today's quality screen, projected back | 240 | 293,361 | **17** | **0** |
| $2B+ screen | 5,549-name Nasdaq/NYSE listing pool, $2B+ | 407 | — | — | **0** |
| $300M–$2B small/mid | same pool, small/mid band, price floor $5 | 96 | 111,237 | **25** | **0** |

On the original universe the binding constraint is unambiguous: **RVOL > 1.5 alone
rejects 86.4% of bars**, and only **17 bars in 5.5 years** survived exclusions +
volume + RVOL + the core signal to even reach the structure gate. Even deleting the
structure gate entirely caps the strategy at 17 candidates — **30× below the 300-trade
minimum**. The verdict therefore does *not* depend on the `MAX_TARGET_ATR = 4.0`
choice, which is exactly the question a reader should ask of a zero-trade result.

The small/mid universe was built specifically to test the obvious objection — *"quiet
pullbacks don't happen in mega-caps, try smaller names"* — and it did shift the
funnel (`avg volume ≥ 200k` becomes the top rejector at 29.8%, RVOL stops dominating)
without producing a single trade. **The conjunction is the problem, not any one
threshold**: a bar must simultaneously be quiet enough to be a pause and busy enough
to clear RVOL 1.5. Those two requirements are close to mutually exclusive, and no
universe fixes that.

Three things were deliberately *not* done, all of them offered by the data and all
of them refused: thresholds were not loosened after seeing the funnel (three separate
opportunities), the structure gate was not deleted "just to see", and no realism pass
was run — there is nothing to apply costs to. One real bug was found and fixed along
the way: a universe-path bug that was hiding ~1,000 tickers from production. The one
finding carried forward was that **GAB and BDJ are closed-end funds, not operating
companies**, and slipped the exclusion gate because they aren't leveraged/inverse
ETFs; that was reported rather than patched mid-run, and became the pre-registered
closed-end-fund blacklist now in `strategies/filters.py`.

⚠️ All P1B runs are labelled **SURVIVOR / CURRENT-FUNDAMENTALS / UPPER-BOUND-ONLY**.
The no-look-ahead check returned **INCONCLUSIVE** for this strategy — with zero closed
trades there is nothing to compare. That gap is why H1's harness added
`assert trades_compared >= 2`, so a look-ahead check can no longer pass vacuously.

Code: `strategies/dip_in_uptrend.py`, `scripts/run_phase1b_validation.py`.

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

Code: `strategies/panic_reversion.py` (entry), `core/h1_backtest.py` (limit-fill simulator + H1 exit ladder), `tests/test_panic_reversion.py` (54 tests), `scripts/run_h1_validation.py` (the ladder). Phase 1 and Phase 1B entries are untouched. The closed-end-fund blacklist the Phase 1B postmortem asked for now lives in `strategies/filters.py` and is applied by every **research** strategy. ⚠️ The live scanner (`core/swing_setups.py`) does not share it — production is protected only by whatever `core_universe.json` currently holds.

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

## 🔬 C-series postmortem — C2 and C3 PASS (the project's first)

**Connors & Alvarez RSI-2 on SPY**, pre-registered in `connors_rsi2_spec.md`. Buy short-term oversold inside a long-term uptrend: SPY > SMA200, then C1 = RSI(2) < 5 exiting above SMA5, C2 = 2-day cumulative RSI(2) < 35 and C3 = < 50, both exiting when RSI(2) > 65. No stop (the book's design), 25-day safety valve, 2bps/side. L0 reproduced the book's published win rates (C1 83.7% vs 83.6%), which validated the harness and nothing else — 1993-2007 is the authors' own development window. The verdict lives in **L2: POST-BOOK (2008-2026), close-fill, costs on**, and it is the first genuinely trustable verdict this project has produced, because SPY is a single continuously-listed ETF: no survivorship bias, no clustering or capacity problem, and a 2bps cost assumption that is actually defensible. **C2 passes every bar of the gate (146 trades, 74.7% win, +0.436%/trade after costs, PF 2.095, t = +3.20). C3 passes every bar (207 trades, 73.0%, +0.336%/trade, PF 1.836, t = +2.98). C1 is MARGINAL on one bar only — 80 POST-BOOK trades against a registered floor of 100 — while being the strongest per trade (+0.528%, PF 2.294).** The edge decayed after publication but did not disappear: C1 went +1.050% -> +0.528% per trade, C2 +0.814% -> +0.436%, C3 +0.660% -> +0.336%, roughly a halving, with win rates down 6-8pp. It is not execution-fragile — at next-open fills for both entry AND exit, C2 and C3 actually *improve* (PF 2.180 and 1.961). The 25-day valve never fired once in 4,672 trades across every run, so the exit rules resolve on their own.

**The thing the gate does not measure, stated plainly:** over 2008-2026 these strategies compound at **2.4-4.1% CAGR against SPY's 11.24%**, because they hold SPY only 5.6-16.5% of the time. The per-trade edge is real and statistically supported; it does not beat buy-and-hold on capital. What it does beat is *risk*: max drawdown 14.0-14.9% (daily mark-to-market) against SPY's 51.9%, so MAR is modestly better (C3 0.28 vs 0.22). This is a cash-deployment strategy, not a replacement for owning the index — and it is only interesting if the idle 84-94% of the time is earning something.

| Run | Window | Fill | Costs | C1 | C2 | C3 |
|---|---|---|---|---|---|---|
| L0 | BOOK 1993-2007 | close | off | 49t, 83.7%, +1.050% | 118t, 83.0%, +0.814% | 164t, 81.1%, +0.660% |
| L1 | POST-BOOK | close | off | 80t, 77.5%, +0.569% | 146t, 74.7%, +0.476% | 207t, 73.4%, +0.376% |
| **L2** | **POST-BOOK** | **close** | **2bps** | **80t, 77.5%, +0.528%, PF 2.294** | **146t, 74.7%, +0.436%, PF 2.095** | **207t, 73.0%, +0.336%, PF 1.836** |
| L3 | POST-BOOK | next open | 2bps | 80t, 73.8%, +0.450%, PF 2.004 | 146t, 72.6%, +0.462%, PF 2.180 | 207t, 69.1%, +0.385%, PF 1.961 |
| L4 | FULL 1993-2026 | close | 2bps | 129t, 79.1%, +0.711% | 264t, 78.4%, +0.587% | 371t, 76.3%, +0.461% |

**Verdicts (L2): C1 MARGINAL** (trade-count floor), **C2 PASS**, **C3 PASS**. Per the spec MARGINAL means "not tradable; do not tune; record and close" — C1 is not rescued by noting it is the best per trade.

**Risk lives in the tail, as it must with no stop.** The worst POST-BOOK trade was 2011-07-27, −10.2% closed with a −15.6% max adverse excursion; COVID cost C3 −7.4% on one trade with a −14.4% MAE. Every stress window was survived (2008-09 was the *best* period, +13.0% for C2), but a strategy with no stop that goes 15% underwater on a single position is one where position sizing is the entire risk control. The 10%-allocation overlay the spec asks for returns 6.5-7.2% over the window at ~1.2% drawdown.

**Honest note on one number.** The gate compares strategy drawdown to buy-and-hold drawdown. As written, `equity_curve_stats` marks the strategy only at trade exits while buy-and-hold is marked daily — an apples-to-oranges comparison that flatters the strategy. Marked daily the strategy's drawdown is 14.0-14.9%, not 11.8-12.2%. The verdict is unchanged either way (both are far under 51.9%), and the registered figure is reported above, but the stricter number is the one to believe.

Code: `strategies/rsi2.py`, `core/c_series_backtest.py`, `tests/test_rsi2.py` (52 tests), `scripts/run_c_series.py`, `scripts/fetch_spy_extended.py`. Universe files untouched — the C-series trades SPY only.

**The C-series registration is now spent.** Per `connors_rsi2_spec.md`, C1/C2/C3 were
the one registration; any further Connors strategy needs a **new pre-registered
addendum naming ONE strategy and its gate** before a line of code is written. That
addendum was written — see below.

---

## 🔬 Addendum 1 postmortem — C2 replicates across 12 ETFs (G1/G2/G3 all pass)

**The question:** is the C2 edge a property of short-term mean reversion in liquid
index products generally, or a SPY-specific fluke? Registered in
`c2_etf_basket_addendum.md` as a **replication, not a new strategy** — the C2 rules
are frozen exactly as validated, and the only thing allowed to differ per instrument
is the cost model (2bps/side for QQQ/DIA/IWM, 5bps for the nine sector SPDRs). The
basket, the gates and the tiebreak were all named before a single instrument was run.

**The pipeline was proven before it was trusted.** SPY is excluded from the basket
but replays through the identical new runner as a control, compared **trade-by-trade
against the stored trades of the published C2 verdict** — 146/146, **zero mismatches
across 11 fields per trade**. Look-ahead replay passed on all 13 instruments, none
vacuously (183–264 trades compared each).

**The result: C2 replicates, and not narrowly.** All **12 of 12 instruments are
positive after costs** over 2008–2026; pooled, **1,750 trades, 72.7% win,
+0.3295%/trade, PF 1.590**, and the sign holds in both halves of a 70/30 date split
(train +0.3264%, test +0.3362%). It is not execution-fragile — at next-open fills the
pooled result *improves* to +0.3625%/trade, PF 1.655. The 25-day valve fired **zero
times** across all 24 instrument-runs, exactly as on SPY. **G1 SUCCESS, G2 PASS,
G3 PASS.**

| | Trades | Win% | Avg%/trade | PF | t (naive) | Positive |
|---|---|---|---|---|---|---|
| Pooled, close fill | 1,750 | 72.7% | **+0.3295** | 1.590 | +6.44 | **12/12** |
| Pooled, next open | 1,749 | 70.7% | +0.3625 | 1.655 | +6.97 | 11/12 |
| Broad (QQQ/DIA/IWM) | 452 | — | +0.3876 | 1.741 | +4.16 | 3/3 |
| Sectors (9 SPDRs) | 1,298 | — | +0.3092 | 1.542 | +5.08 | 9/9 |

**Four things temper that, all measured rather than asserted:**

**1. The pooled t-stat is inflated, and here is by how much.** The addendum required
this to be weighed honestly rather than counted as twelve confirmations. Naive pooled
t = **+6.44**; clustering trades by entry day (same-day sector signals are one market
event) gives **t = +3.81**; clustering by calendar month gives **+3.83**. The edge
survives the correction comfortably — but the honest figure is ~+3.8, not +6.4. Mean
pairwise signal-date Jaccard is **0.109** (QQQ↔XLK highest at 0.483, XLE↔XLU lowest
at 0.023), and the largest single-day cluster was **10 of 12 instruments** on
2010-01-22.

**2. Twelve are positive; only seven are individually significant.** Own t ≥ 2.0:
QQQ, DIA, XLI, XLK, XLP, XLV, XLY. The other five — IWM (1.48), XLE (0.91), XLB
(0.71), XLF (0.71), XLU (0.47) — are positive but not distinguishable from noise on
their own. G1 asked for sign, not significance, and passes as written; the
distinction belongs next to it.

**3. The basket beats buy-and-hold on MAR — but loses to SPY-only C2.** G2's
registered bar is P-capped MAR ≥ SPY buy-and-hold MAR, and it clears: **0.2321 vs
0.2167**. Against the addendum's *other* named benchmark, the registered SPY-only C2
implementation, it does not: **0.2321 vs 0.2396**, daily-marked and sizing-invariant.
So the basket buys **6.5× the activity** (51.5 trades/yr vs 7.9) at a slightly
*worse* risk-adjusted return than the single-instrument reference. G2 passes; the
thing G2 was written to detect — "activity without quality" — is visible anyway.

**4. The capacity problem that killed H1 is present, and the registered tiebreak
makes it worse.** Under the 3-position cap, **45.3% of signals are refused**, and the
trades actually taken average **+0.2247% against +0.4559% for the ones refused** —
the cap systematically declines the better signals. Unlike H1 this stays positive, so
it is a haircut rather than a kill. But the registered priority rule (most oversold
first) is the *cause*: against 25 random-take seeds it sits at the **16th percentile**
on taken expectancy (random mean +0.2383%) and the 32nd on MAR. Picking the most
oversold name first is worse than picking at random. Per the addendum the rule stands
for this run and is reported, not changed.

| Sim | Sizing | CAGR | MaxDD | MAR | Trades/yr | Refused |
|---|---|---|---|---|---|---|
| P-uncapped | equal-open | +5.36% | 44.29% | 0.121 | 94.2 | 0% |
| **P-capped (registered)** | **1/3 slots** | **+3.73%** | **16.08%** | **0.232** | **51.5** | **45.3%** |
| P-capped (diagnostic) | equal-open | +6.65% | 19.35% | 0.344 | 51.5 | 45.3% |
| SPY-only C2 (reference) | — | +3.37% | 14.06% | 0.240 | 7.9 | — |
| SPY buy-and-hold | 100% | +11.24% | 51.87% | 0.217 | — | — |

**Risk still lives in the tail, and 2020 is where it shows.** Pooled crisis windows:
2008-09 was the *best* period (+0.759%/trade, PF 2.434), 2022 mildly negative
(−0.194%), and **COVID was carnage — 12 trades at −5.58%/trade, PF 0.038**, including
**XLF −32.8% closed with a −40.8% maximum adverse excursion**. A no-stop strategy that
goes 41% underwater on one position is one where sizing is the entire risk control,
which is exactly why the 1/3-slot ruling matters. The cap did its job: the P-capped
portfolio lost only −4.5% over Feb–Apr 2020. Its worst drawdown day was not COVID at
all but **2011-08-08 (−16.08%)**, when the debt-ceiling selloff hit several correlated
sectors at once. 5 of 19 years are negative (2008, 2011, 2018, 2020, 2022).

**Verdict: the SPY C2 result was not a fluke.** The same rules, unchanged, produce a
positive after-cost edge on twelve other liquid index products, out of sample, with
the sign holding in both halves. What replication does *not* buy is a better
business: the basket still compounds at ~3.7% against SPY's 11.2%, still sits in cash
half the time, and is still slightly worse risk-adjusted than just running C2 on SPY.
Per the addendum, **SPY-only remains the reference implementation.** Nothing is
deployed; no threshold was moved.

Code: `core/basket_backtest.py` (runner + portfolio sim + gates),
`scripts/fetch_etf_basket.py`, `scripts/run_etf_basket.py`, `tests/test_etf_basket.py`
(42 tests). Data: `data/validation_cache/etf_basket.parquet`. Universe files untouched.

---

## 🕓 Still queued — H3 and H4

Both are registered in `edge_search_protocol.md` with their gates already written.
Neither has been implemented. Listing them here so the queue is visible and so the
gates cannot be quietly rewritten later.

### H3 — Post-Earnings Announcement Drift (PEAD)

**Status: viable, not started.** H3 was registered as *conditional on data* — it needs
historical earnings dates for most of the 407-name universe, and the protocol says
report `DATA-UNAVAILABLE` and close it unimplemented if they can't be had. **That
precondition is now satisfied.** H2's by-product, `data/validation_cache/earnings_dates.parquet`,
covers **395/407 tickers (97.1%) from EDGAR 8-K item 2.02** — free, filed same-day,
genuinely point-in-time. So H3 runs when authorised.

Registered rules (unchanged, not to be edited): earnings reported and the next
regular session closes ≥ +5% vs the pre-announcement close, on RVOL ≥ 2.0, closing in
the top third of its range; buy at the open **2 trading days after** the reaction day;
close > SMA200 and regime gate ON; stop at entry − 1.5 × ATR(14), break-even ratchet
at +1R, time exit at 15 bars. Gate: the default gate with the **trade-count floor
lowered to 150** (events are rarer — registered in advance, not after seeing a count).

⚠️ H3 inherits the same survivorship problem as H1/H2: the 407 names are today's
screen projected backward, so any H3 result is an **upper bound**, capped at
MARGINAL by the protocol. Note also that H2 already measured how large this event
population is — **500 of 680 qualifying bars (73.5%) were earnings reactions** — so
sample size is not expected to be the binding constraint.

### H4 — Regime trend-following on index ETFs

**Status: not started.** SPY and QQQ only, 50/50, evaluated on the last trading day of
each week and acted on at the next open. IN when close > SMA200 and SMA200 is rising
vs 20 days ago; OUT to cash when close < SMA200 for **5 consecutive** sessions (the
anti-whipsaw constant, registered and final). No stops, no targets — it is a switch,
not a trade.

H4 is the only hypothesis with a **different gate by design**, because its claim is
not excess return but drawdown avoidance: max drawdown ≤ ⅔ of buy-and-hold's, AND
CAGR ≥ (buy-and-hold CAGR − 2pp), AND MAR strictly greater than buy-and-hold's —
all three, over the full window and in both splits. It is also the only hypothesis
that can reach an unqualified **PASS** on present data, since index ETFs have no
survivorship issue. Even so, 5.5 years contains essentially one bear cycle (2022), so
a pass must be labelled **ONE-CYCLE-SAMPLE**.

Note the overlap worth watching: H4 and the C-series are both "own SPY only when it's
healthy" rules on different timescales, and the C-series already spends 84–94% of its
time in cash. If H4 passes, the honest question is whether the two compose or merely
restate each other.

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
├── strategies/                      # Pre-registered entry hypotheses (research)
│   ├── trend_pullback.py           # P1  — NO EDGE
│   ├── dip_in_uptrend.py           # P1B — FAIL (zero trades)
│   ├── panic_reversion.py          # H1  — NO-EDGE
│   ├── gap_continuation.py         # H2  — FAIL
│   ├── rsi2.py                     # C1/C2/C3 — MARGINAL / PASS / PASS
│   └── filters.py                  # Shared exclusions, CEF blacklist, EarningsCalendar
├── core/
│   ├── swing_setups.py             # Swing setup engine (DEFAULT scorer)
│   ├── swing_exits.py              # Exit state machine (stop/breakeven/trail/time)
│   ├── swing_backtest.py           # Replay engine + pre-registered gate
│   ├── protocol_gate.py            # THE shared H1-H3 gate + train/test split
│   ├── h1_backtest.py              # Limit-fill simulator (H1)
│   ├── h2_backtest.py              # Market-entry simulator + SMA10 trail (H2)
│   ├── c_series_backtest.py        # %-per-trade simulator, no stop (C-series)
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
│   ├── run_swing_validation.py     # P1 ladder
│   ├── run_phase1b_validation.py   # P1B ladder + zero-trade census
│   ├── run_h1_validation.py        # H1 ladder
│   ├── run_h2_validation.py        # H2 ladder
│   ├── run_c_series.py             # C-series ladder L0-L4
│   ├── fetch_earnings_dates.py     # EDGAR 8-K 2.02 -> PIT earnings calendar
│   └── fetch_spy_extended.py       # SPY 1993-2026 daily cache (C-series)
├── core_universe.json               # Live universe (+ .bak / .bak2 / .p1b_final)
└── tests/                           # pytest suite (377 tests across all suites)
```

**Pre-registration workflow.** Every hypothesis above was written into a spec
document — `Phase1B_Reversal_Strategy_Spec.md`, `edge_search_protocol.md`,
`connors_rsi2_spec.md` — with its **gate thresholds fixed before the first run**, and
each spec forbids editing itself once results exist. That is why the ledger contains
five failures: the pass mark could not be moved to meet the result. See
[HOW_IT_WORKS.md](HOW_IT_WORKS.md) for the plain-language version.

Module docstrings carry the technical detail per file.
