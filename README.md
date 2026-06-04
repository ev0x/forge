# Tradezella (self-hosted)

A self-hosted, single-user trading journal for futures traders running multiple prop-firm accounts. Imports raw fills from Sierra Chart's Trade Activity Log (TSV) and NinjaTrader's Executions grid (CSV), matches them into round-trip trades, and computes per-account economics, drawdown lifecycle, payout planning, and analytics.

The sidebar still brands the UI as **Forge.local** — same product.

## What it does

- **Multi-account aggregation** — top-right account picker filters every page; pin one account or combine many.
- **Auto-classification** — accounts auto-named and typed from external IDs (Apex `APEX-…`, Lucid Flex `LFE…`, Tradeify `tradeify-TDFY…`, etc.).
- **Drawdown lifecycle** — walks each account's trade + payout timeline under its configured drawdown mode (static / EOD trailing / intraday trailing), auto-flips `active → blown` / `active → funded`. Broker balance overrides when set.
- **Real-money economics** — Costs page tracks evaluation subscriptions (one-off or recurring monthly), activations, resets, etc., then shows payouts received − total spent.
- **Payout planning** — ETA-to-funded and ETA-to-next-payout per account, respecting min-trading-day and payout-spacing rules.
- **Prop-firm preset library** — Apex (Intraday + EOD), TopStep, TakeProfit Trader, Lucid, Tradeify Growth, Tradovate. Editable in the UI.
- **Self-hosted, local-only** — no auth, no telemetry, all data lives in your local Postgres.

## Quick start

Prerequisites: Docker Desktop running.

```bash
git clone <this-repo>
cd tradezella
cp .env.example .env          # set passwords + ports
docker compose up -d --build
```

Then open http://localhost:5180 (or whatever `WEB_PORT` you set in `.env`).

## Importing data

- **Sierra Chart** — `Trade > Trade Activity Log > Export Trade Activity Log` → drop the `.txt` onto the **Upload** page. Detected by header sniffing.
- **NinjaTrader** — `Control Center > Performance > Executions` grid → right-click → Export → CSV → upload the same way.

Accounts are created on first sight of an external ID; apply a prop-firm preset from the **Prop Firms** page (or set a default in **Plan > Edit goals** so new imports get classified automatically).

## Project layout

```
backend/         FastAPI + SQLAlchemy + psycopg
frontend/        Vite + React 18 + TypeScript + Tailwind + recharts
docker-compose.yml   db (postgres) + api (uvicorn) + web (nginx-served static)
.env.example     copy to .env and edit
CLAUDE.md        deep dive: architecture, data pipeline, conventions
```

The api bind-mounts `./backend/app` into the container so source edits are live; uvicorn runs without `--reload`, so backend changes need `docker compose restart api`. The frontend is built into the image — frontend edits need `docker compose up -d --build web`.

## Architecture

See [`CLAUDE.md`](./CLAUDE.md) for the full tour: the parser → executions → matching → trades → lifecycle pipeline, the trade-matching algorithm, account-classification regexes, schema-migration ritual, and the gotchas.

## Status & disclaimers

- **Personal tool, not a product.** No tests, no auth, CORS is wide open. Run on localhost, not on the public internet.
- **Prop-firm seed values are best-estimates.** The rules in `backend/app/prop_firms.py` (profit targets, max drawdowns, contract limits, monthly fees) are scraped from each firm's public pricing widget and the industry-standard tier ratios. Verify against your live dashboard before relying on any forecast for real-money decisions.
- **No migration system.** Schema changes are idempotent `ALTER TABLE … ADD COLUMN IF NOT EXISTS …` statements in `backend/app/migrations.py`, run on every startup.

## License

[MIT](./LICENSE).
