Swing Trade Assistant — Jira Ticket Set
Product: A personal daily swing-trade assistant. A backend job runs every trading day after the close, pulls market data through a free API, applies a fixed rulebook (1% risk, ≥2:1 reward, no-earnings, no-leverage/penny filter), and emails a ranked list of candidate trades with exact bracket-order levels (entry / stop / target / share count). A simple React dashboard shows the latest candidates, watchlist, and trade journal.

Hard scope guardrail (applies to EVERY ticket): This app is ANALYSIS + EMAIL ONLY. It must never connect to a brokerage, place, modify, or cancel any order, or hold brokerage credentials. All order execution is done manually by the user in Fidelity. Any code that reaches toward broker execution is out of scope and must be rejected in review.

Stack: Backend = Python 3.11+ / FastAPI + APScheduler (or cron). Frontend = React (Vite) + TypeScript. Database = PostgreSQL. Data = free-tier stock API behind a pluggable DataProvider interface (default: yfinance; alternates: Finnhub, Alpha Vantage). Email = SMTP or a free-tier transactional email API (e.g. SendGrid free tier).

EPIC SWING-1 — Swing Trade Assistant (MVP)
Goal: Ship an end-to-end daily pipeline: fetch data → screen with rulebook → persist candidates → email report → view in dashboard.

Definition of Done (Epic):

A scheduled job runs each US trading day and emails a candidate report.

The React dashboard displays the latest report, current watchlist, and a manual trade journal.

No brokerage integration exists anywhere in the codebase.

STORY SWING-2 — Project scaffolding & configuration
Type: Story | Priority: Highest | Epic: SWING-1

Description:
Set up the monorepo skeleton, environment config, and secrets handling for backend, frontend, and database.

Acceptance Criteria:

Repo layout: /backend (FastAPI), /frontend (React+Vite+TS), /db (migrations), /docker-compose.yml.

docker-compose.yml spins up Postgres, backend, and frontend locally with one command.

.env.example documents all config keys: DATA_PROVIDER, DATA_API_KEY, SMTP_* / EMAIL_API_KEY, RECIPIENT_EMAIL, DATABASE_URL, RISK_PER_TRADE_PCT, ACCOUNT_SIZE, MIN_REWARD_RISK.

Secrets loaded from environment only — never hardcoded, never committed.

README with local-run instructions.

Technical Notes:

Python: FastAPI, SQLAlchemy, Alembic, APScheduler, pydantic-settings, httpx, yfinance.

Frontend: Vite + React + TypeScript + a lightweight fetch client.

STORY SWING-3 — Database schema & migrations
Type: Story | Priority: Highest | Epic: SWING-1

Description:
Define the PostgreSQL schema and Alembic migrations for watchlist, daily quotes, generated candidates, run logs, and the manual trade journal.

Acceptance Criteria — Tables:

watchlist — id, symbol (unique), active (bool), notes, created_at.

daily_quote — id, symbol, trade_date, open, high, low, close, prev_close, change_pct, volume, avg_volume, year_high, created_at. Unique on (symbol, trade_date).

earnings_date — id, symbol, next_earnings_date, fetched_at.

candidate — id, run_id (FK), symbol, rank, setup_type (breakout|pullback), entry, stop, target, shares, risk_dollars, reward_risk_ratio, rationale (text), created_at.

pipeline_run — id, run_date, status (success|partial|failed), market_trend (uptrend|mixed|downtrend), spy_change_pct, qqq_change_pct, num_candidates, error_msg, started_at, finished_at.

trade_journal — id, symbol, entry_price, stop_price, target_price, shares, status (open|closed|cancelled), opened_at, closed_at, exit_price, pnl_dollars, notes. Filled in manually by the user — the app never writes trades automatically.

Acceptance Criteria — General:

Alembic migration creates all tables; alembic upgrade head works from clean DB.

Seed migration inserts default watchlist: JPM, AAPL, MSFT, GOOGL, NVDA (explicitly NOT PLTR).

STORY SWING-4 — Pluggable market-data provider
Type: Story | Priority: Highest | Epic: SWING-1

Description:
Create a DataProvider abstraction so the data source can be swapped without touching business logic. Ship a default free implementation.

Acceptance Criteria:

Abstract interface DataProvider with methods: get_quote(symbol) -> Quote, get_ohlcv(symbol, days) -> list[Bar], get_most_active(limit) -> list[Quote], get_next_earnings(symbol) -> date | None.

Default implementation YFinanceProvider (free, no key required).

Provider selected via DATA_PROVIDER env var; factory pattern.

Graceful handling of rate limits and missing symbols (log + skip, never crash the run).

Unit tests with mocked HTTP responses (no live network in CI).

Technical Notes:

Normalize all provider outputs into internal Quote / Bar dataclasses so downstream code is provider-agnostic.

STORY SWING-5 — Rulebook screening engine (core logic)
Type: Story | Priority: Highest | Epic: SWING-1

Description:
Implement the deterministic screening engine that turns raw market data into ranked, sized candidate trades using the user's rulebook. This is the heart of the app.

Acceptance Criteria — Filters (reject if any fail):

No earnings within holding window: exclude any symbol whose next_earnings_date is within the next 10 calendar days.

No leveraged / inverse ETFs: exclude symbols matching a maintained denylist/pattern (e.g. TQQQ, SQQQ, SOXL, SOXS, TZA, TSLL, and any 2x/3x/"Bull"/"Bear" leveraged product).

No thin/penny names: exclude price < $10 or average daily volume < 1,000,000 shares.

Liquidity: only large-cap / liquid names pass.

Explicit permanent exclusion: PLTR is always excluded, everywhere, no exceptions.

Acceptance Criteria — Market gate:

Compute market trend from SPY & QQQ change vs prior close: uptrend if both green and QQQ > +0.5%; downtrend if QQQ < -1%; else mixed.

If downtrend, engine returns zero candidates and the report states "No-trade day — market weak." (Cash is a valid position.)

Acceptance Criteria — Level & size math (per surviving candidate):

Determine setup_type: breakout (entry = today's high + small buffer) or pullback (entry = just below current price).

stop placed below support / below entry; target set so reward_risk_ratio >= MIN_REWARD_RISK (default 2.0).

risk_per_share = entry - stop.

shares = floor((ACCOUNT_SIZE * RISK_PER_TRADE_PCT) / risk_per_share) — round DOWN. Default ACCOUNT_SIZE=3000, RISK_PER_TRADE_PCT=0.01 → max $30 risk.

risk_dollars = shares * risk_per_share must be <= ACCOUNT_SIZE * RISK_PER_TRADE_PCT.

Reject candidate if shares < 1 or reward_risk_ratio < MIN_REWARD_RISK.

Rank surviving candidates by relative strength (change_pct vs SPY) and output top N (default 3).

Acceptance Criteria — Testing:

Pure functions, fully unit-tested with fixed inputs → deterministic outputs.

Test cases: earnings-exclusion, leverage-exclusion, PLTR-exclusion, downtrend → zero candidates, share-rounding, reward:risk rejection.

Technical Notes:

Engine must be pure/deterministic given input data (no side effects) so it's testable and auditable.

STORY SWING-6 — Daily pipeline orchestration & scheduler
Type: Story | Priority: High | Epic: SWING-1

Description:
Wire the steps into one scheduled job: fetch → screen → persist → email, with a pipeline_run audit record.

Acceptance Criteria:

Job sequence: load active watchlist + most-active → fetch quotes/OHLCV/earnings → run engine → persist candidate rows → send email → write pipeline_run (status, trend, counts, timings).

Scheduled once per US trading day at a configurable time (default 4:30 PM ET, after close). Skips weekends and US market holidays.

Idempotent: re-running for the same run_date replaces that day's candidates, doesn't duplicate.

Any provider failure degrades gracefully → status=partial, email still sends with whatever succeeded.

Manual trigger endpoint POST /api/runs/trigger for on-demand runs (matches user's on-demand habit).

STORY SWING-7 — Email report
Type: Story | Priority: High | Epic: SWING-1

Description:
Send a clean daily email summarizing market trend and ranked candidates with exact bracket levels.

Acceptance Criteria:

Email includes: date, market trend (uptrend/mixed/downtrend) with SPY & QQQ %, and a table of candidates: symbol, setup type, entry, stop, target, shares, $ risk, reward:risk, one-line rationale.

If zero candidates: email clearly says "No-trade day" and why.

Plain-text + simple HTML version; renders on mobile.

Footer disclaimer: "Analysis only. Not financial advice. Place orders manually; verify levels before trading."

Configurable recipient via RECIPIENT_EMAIL.

Failure to send is logged and reflected in pipeline_run, but never crashes the pipeline.

STORY SWING-8 — Backend API for frontend
Type: Story | Priority: High | Epic: SWING-1

Description:
Expose REST endpoints the React app consumes.

Acceptance Criteria — Endpoints:

GET /api/candidates/latest — most recent run's ranked candidates + run metadata.

GET /api/runs?limit=30 — recent pipeline runs (history/status).

GET /api/watchlist / POST /api/watchlist / DELETE /api/watchlist/{symbol} — manage watchlist (server rejects PLTR on add).

GET /api/journal / POST /api/journal / PATCH /api/journal/{id} — manual trade journal CRUD.

POST /api/runs/trigger — on-demand pipeline run.

OpenAPI docs auto-generated; CORS configured for the frontend origin.

STORY SWING-9 — React frontend (simple dashboard)
Type: Story | Priority: Medium | Epic: SWING-1

Description:
A clean, simple dashboard — no clutter. Single page with a few cards.

Acceptance Criteria:

Header: market trend badge (green/amber/red) + SPY & QQQ % for the latest run + last-run timestamp.

Today's Candidates card: table of ranked candidates (symbol, setup, entry, stop, target, shares, $risk, R:R, rationale). Empty state shows "No-trade day."

Watchlist card: list + add/remove symbols (blocks PLTR with a friendly message).

Trade Journal card: table of manual trades with add/edit; shows open vs closed and running P&L.

"Run now" button → calls trigger endpoint, shows loading + refreshes.

Responsive, minimal styling, readable on phone. No login required for v1 (single-user, local/self-hosted).

Technical Notes:

Keep it deliberately simple: React + fetch + a component library or plain CSS. No heavy state management needed.

STORY SWING-10 — Tests, docs & deployment
Type: Story | Priority: Medium | Epic: SWING-1

Description:
Ensure reliability and easy self-hosting.

Acceptance Criteria:

Unit tests for provider, engine (all rulebook cases), and API happy paths; CI runs them with no live network.

docker-compose up runs the full stack locally.

README covers: env setup, running migrations, triggering a run, configuring email, swapping data provider.

Deployment note for a cheap host (single VM / container) with the scheduler enabled.

Security check documented: no brokerage credentials, no order-execution code anywhere.

Out of scope (do NOT build)
Any brokerage/broker-API integration or order placement, modification, or cancellation.

Real-money automation of any kind.

Multi-user accounts / auth (v1 is single-user self-hosted).

Options, crypto, futures (equities only for v1).

Suggested build order
SWING-2 → SWING-3 → SWING-4 → SWING-5 → SWING-6 → SWING-7 → SWING-8 → SWING-9 → SWING-10