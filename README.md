# Algo Trading Platform

Historical data → paper desk → (later) live.

DhanHQ **live MCP** is configured ([docs/dhan-mcp.md](docs/dhan-mcp.md)).  
**Paper trading is ours** — Dhan Sandbox is not virtual-money paper trading ([docs/paper-trading.md](docs/paper-trading.md)).

```text
DhanHQ data  →  local DB  →  paper desk (virtual fills)  →  later live orders
```

## Quick start

```bash
cd AI-TRADING
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env   # DHAN_CLIENT_ID + DHAN_ACCESS_TOKEN (live Data API)

algo doctor
algo instruments sync
algo data download --instrument NIFTY --timeframe 5m --start 2025-01-01 --end 2026-01-01

# Paper Desk UI (multi-strategy)
algo paper ui
# → http://127.0.0.1:8787
```

Or CLI replay tonight:

```bash
algo paper run --strategy ema_cross --mode replay --instrument NIFTY --timeframe 5m
```

Tomorrow during market hours:

```bash
algo paper run --strategy ema_cross --mode live --instrument NIFTY
# or use the UI → Mode: Live paper → Start
```

## Easy working plan

See [ROADMAP.md](ROADMAP.md). Long Phase-0 data spec: [plan.md](plan.md).

## What Dhan Sandbox is for

[DevPortal Sandbox](https://developer.dhanhq.co/sandbox) (`sandbox.dhan.co`) tests **API shapes** without exchange fills.  
It is **not** paper trading with live marks. Official answer: [Dhan Support](https://dhan.co/support/platforms/dhanhq-api/can-i-use-dhanhq-sandbox-for-paper-trading-with-virtual-money-and-live-market-data/).

Optional sandbox creds in `.env` (`DHAN_SANDBOX_*`) are for later order dry-runs only.

## Dhan MCP

`"dhan": { "url": "https://mcp.dhan.co/mcp" }` in `.cursor/mcp.json`.  
Connect via Settings → Tools & MCP → **dhan** → Connect (OAuth).  
Use for holdings / quotes. Do not place live orders via MCP until Phase 4.

## Layout

```text
src/algo/
  providers/   Dhan historical adapter
  paper/       virtual broker + strategies + Paper Desk UI
  analysis/    pandas helpers
docs/          paper-trading.md, dhan-mcp.md, architecture
```

## Deploy on Render (free Hobby)

1. Push this repo to GitHub (auto-deploy on `main`).
2. Create a **Web Service** from the repo (or apply `render.yaml` Blueprint).
3. Set env vars in the Dashboard: `DHAN_CLIENT_ID`, `DHAN_ACCESS_TOKEN` (never commit `.env`).
4. Open the service URL → configure strategies once → Start live.
5. Free tier sleeps after ~15 min idle. Use a free external cron (e.g. cron-job.org):
   - Every 10–12 min (market hours): `GET https://YOUR-APP.onrender.com/api/health`
   - ~08:55 IST: `POST https://YOUR-APP.onrender.com/api/session/wake`

**Database on free Render:** keep `DATABASE_URL=sqlite:///./data/algo.db` for testing. Expect resets when the instance sleeps. Switch to free Supabase Postgres later if you need durable trade history.
