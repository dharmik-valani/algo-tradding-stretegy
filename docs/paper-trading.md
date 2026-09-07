# Paper trading vs Dhan Sandbox vs MCP

## Short answer

| Thing | What it is | Good for paper strategies? |
| --- | --- | --- |
| **Dhan Sandbox** (`sandbox.dhan.co`) | Closed API playground — same request shapes as live, no exchange fills | API wiring only |
| **Dhan Live MCP** (`mcp.dhan.co`) | Live account tools (portfolio, quotes, **real orders**) | Quotes yes; orders = real money |
| **Our paper engine** (`algo paper …` + UI) | Virtual cash + simulated fills on local/live prices | **Yes — use this** |

Dhan Support is explicit: Sandbox is **not** paper trading with virtual money and live market data.  
See: [Can I use DhanHQ Sandbox for paper trading?](https://dhan.co/support/platforms/dhanhq-api/can-i-use-dhanhq-sandbox-for-paper-trading-with-virtual-money-and-live-market-data/)

Sandbox docs: base URL `https://sandbox.dhan.co/v2` for integration tests only.  
See: [DhanHQ Sandbox](https://docs.dhanhq.co/docs/v2/) / DevPortal [developer.dhanhq.co/sandbox](https://developer.dhanhq.co/sandbox).

## What you should do with your DevPortal sandbox

1. Keep sandbox Client ID + Access Token for **order-API dry runs** later (payload shape checks).
2. Do **not** expect it to mark-to-market a strategy with live NIFTY like a paper account.
3. For strategy paper tests, use **our paper engine**.

## Modes in this project

### 1. Replay (works tonight, no market hours)

```bash
algo paper run --strategy ema_cross --mode replay --instrument NIFTY --timeframe 5m
```

Walks stored historical candles and simulates fills. Good for checking signal logic before the open.

### 2. Live paper (tomorrow during market hours)

```bash
algo paper ui
# or
algo paper run --strategy ema_cross --mode live --instrument NIFTY
```

Polls / streams live prices and simulates fills into a **virtual** wallet.  
With Data API + a valid token, Paper Desk prefers Dhan **WebSocket live market feed**
([docs](https://dhanhq.co/docs/v2/live-market-feed/)), with REST `/marketfeed/ltp` fallback.  
Tokens last ~24h — use **Settings → Dhan Data API** to paste/renew (`GET /v2/RenewToken`).  
Does **not** place Dhan orders.

## Credentials

```env
# Live / Data API (historical download + live LTP for paper)
DHAN_CLIENT_ID=
DHAN_ACCESS_TOKEN=

# Optional: sandbox only for API shape tests (not paper PnL)
DHAN_SANDBOX_CLIENT_ID=
DHAN_SANDBOX_ACCESS_TOKEN=
DHAN_MODE=live   # live | sandbox  — paper live quotes always use live Data API
```

## MCP

Official Dhan MCP is already in `.cursor/mcp.json`:

```json
"dhan": { "url": "https://mcp.dhan.co/mcp" }
```

- Connect once: Cursor → Settings → Tools & MCP → **dhan** → Connect (OAuth).
- Use for: holdings, funds, search, live quotes questions.
- Do **not** ask MCP to place orders until you intentionally go live.
- There is **no separate sandbox MCP** from Dhan today.

## Tomorrow morning checklist

1. `algo doctor` — Data plan active, token fresh (24h tokens expire).
2. `algo paper ui --watch` — open the dashboard (auto-restarts if the process dies; resumes open trades).
3. Enable **Strategy 1** (`ema_cross`) on NIFTY.
4. Watch virtual fills / PnL — no real money moves.
5. Add Strategy 2 / 3 from the UI when ready.
