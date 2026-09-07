# DhanHQ MCP (official)

Official docs: [DhanHQ MCP](https://docs.dhanhq.co/mcp/)

This is Dhan's **hosted** Model Context Protocol server. It talks to your **live** Dhan account through OAuth. It is not a second copy of our historical downloader.

```text
You  →  Cursor  →  https://mcp.dhan.co/mcp  →  Dhan  →  NSE / BSE / MCX
```

Our local pipeline stays the backtest source of truth:

```text
algo data download  →  data/algo.db / Parquet  →  load_candles()
```

Use MCP for live portfolio, quotes, and (later) orders. Use `algo data …` for reproducible historical series.

## Connect in Cursor

Config is already in:

- workspace: `.cursor/mcp.json` (`dhan`)
- this project: `AI-TRADING/.cursor/mcp.json`

Then:

1. Cursor **Settings → Tools & MCP**
2. Find **dhan** and click **Connect**
3. Sign in with your Dhan account in the browser
4. Start a **new chat** and verify with: `Check my holdings with Dhan MCP`

No `DHAN_ACCESS_TOKEN` is stored in `mcp.json`. Auth is OAuth on Dhan's server.

Prerequisites from Dhan:

- Active Dhan trading account
- Optional Data API plan for live quotes and option chains
- Node.js 18+ if the client transport needs it

## Tools (current Dhan catalog)

| Tool | Use |
| --- | --- |
| `portfolio_agent_tool` | Funds, holdings, positions, today's trades |
| `search_agent_tool` | Name/ticker → Dhan security ID |
| `market_data_agent_tool` | Live prices, depth, option chain |
| `historical_data_agent_tool` | Live API OHLCV (not our local DB) |
| `orderbook_agent_tool` | Open / pending orders |
| `tradebook_agent_tool` | Executed trades |
| `margin_agent_tool` | Pre-trade margin |
| `alerts_agent_tool` | Price / indicator alerts |
| `trading_agent_tool` | **Places, modifies, cancels real orders** |

Example prompts from Dhan:

- "What are my available funds?"
- "Show me my holdings"
- "What's the current price of RELIANCE?"
- "What is the security ID for HDFCBANK on NSE?"

## Safety

`trading_agent_tool` hits the **live** account. Do not use it until Phase 4 (live trading) and you have reviewed quantity, product type, and exchange.

Do **not** let MCP overwrite or replace:

- `instruments` / `candles` in SQLite
- Parquet exports
- data-quality reports

If you pull candles via MCP for a quick look, that is fine. Persist backtests only from `algo data download`.

## Sandbox vs MCP

| Surface | URL | Paper strategies? |
| --- | --- | --- |
| Live MCP | `https://mcp.dhan.co/mcp` | Quotes/portfolio yes; orders = **real money** |
| DevPortal Sandbox | `https://sandbox.dhan.co/v2` | API dry-run only — **not** paper PnL |
| Our Paper Desk | `algo paper ui` | **Yes** — virtual fills |

There is no separate “sandbox MCP” from Dhan. Connect the official live MCP once, and keep order tools unused until Phase 4.

Details: [paper-trading.md](paper-trading.md).

## vs our later canonical MCP

Phase 5 in `ROADMAP.md` still plans a **read-only** MCP on `data/algo.db` (`list_instruments`, `get_candles`, `data_status`). That is a different server. Dhan MCP is the broker live surface.
