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
3. Set env vars in the Dashboard: `DHAN_CLIENT_ID`, `DHAN_ACCESS_TOKEN`, `DATABASE_URL` (Supabase), `CRON_SECRET`, plus email vars below for digests.
4. Open the service URL → configure strategies once → Start live (or let cron wake).
5. Free tier sleeps after ~15 min idle. **Do not rely on GitHub Actions alone** — its `schedule` often skips or runs hours late.

### Keep Render awake automatically (required)

GitHub cron is best-effort. Add a free **UptimeRobot** (or cron-job.org) HTTP monitor:

1. Create monitor → type **HTTPS**
2. URL (every **5 minutes**):
   `https://algo-paper-desk.onrender.com/api/cron/heartbeat?cron_secret=YOUR_CRON_SECRET&send_email=true`
3. That single ping will:
   - keep the free instance warm
   - auto-**wake** live paper Mon–Fri 08:00–15:45 IST
   - auto-**sleep** after 15:45 IST
   - send **pre-market / post-market Brevo emails once per day** (even if GitHub is late)

Use the same `CRON_SECRET` value as on Render.

### Cron (GitHub Actions — backup)

Workflow: `.github/workflows/render-paper-cron.yml`

| When (IST, Mon–Fri) | Action |
|---------------------|--------|
| ~every 5 min, 08:00–15:50 | `GET /api/cron/heartbeat` (wake + once-daily email) |
| ~08:17 / 08:37 / 08:47 / 08:57 / 09:07 | extra `wake` pings |
| ~08:55 | `report_open` email |
| ~15:40 | `report_close` email |
| ~15:47 | `sleep` |

Repo secrets (GitHub Actions):

- `RENDER_APP_URL` = `https://algo-paper-desk.onrender.com`
- `CRON_SECRET` = same as Render env `CRON_SECRET`

Render env for email digests (to `dharmikvalani57@gmail.com`):

- `REPORT_EMAIL_TO` = `dharmikvalani57@gmail.com`
- `EMAIL_USER` / `SMTP_FROM` = `hello@mail.weupsell.com` (verified Brevo sender)
- `BREVO_API_KEY` = Brevo transactional API key (preferred; uses HTTPS, ignores `EMAIL_HOST`)
- Optional SMTP fallback: `SMTP_USER` + `SMTP_PASSWORD` + `EMAIL_HOST` / `SMTP_HOST`

If Brevo says sent but Gmail is empty, check **Spam / Promotions**, and confirm `mail.weupsell.com` has SPF/DKIM in Brevo.

Manual test (Actions → this workflow → Run workflow):

- `heartbeat` — wake + digest if due
- `report_open` / `report_close` — force email
- `wake` / `sleep` / `health` — session only

Also grant Render’s GitHub app access to this **private** repo, or deploys will fail to clone.

**Database:** use Supabase Postgres (`DATABASE_URL`) so reports survive sleep. Do not run live on laptop + Render at the same time.
