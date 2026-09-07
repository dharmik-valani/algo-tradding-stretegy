# MCP

Two different MCP surfaces. Do not mix them.

## 1. DhanHQ live MCP (configured now)

Official hosted server: `https://mcp.dhan.co/mcp`

Setup and safety notes: [docs/dhan-mcp.md](../../../docs/dhan-mcp.md)

Cursor config: [AI-TRADING/.cursor/mcp.json](../../../.cursor/mcp.json)

This talks to the **live Dhan account** (portfolio, quotes, orders). It is not our historical store.

## 2. Canonical store MCP (later)

Read-only tools on local candles, for example:

- `list_instruments`
- `get_candles`
- `data_status`
- later: `run_backtest`

That server must read `data/algo.db` / Parquet. It must not become a second Dhan downloader.
