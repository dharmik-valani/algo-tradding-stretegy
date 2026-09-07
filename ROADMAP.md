# Easy working plan

`plan.md` is the detailed Phase-1 spec. This file is the order of work so you can actually look at historical data without building a trading bot.

```text
Phase 0   Local historical data
Phase 1   More instruments
Phase 2   Backtesting (still light — paper can replay history first)
Phase 3   Paper trading   ← Paper Desk is here
Phase 4   Live trading
Phase 5   Our own read-only data MCP
```

Dhan's **live** MCP is wired (`https://mcp.dhan.co/mcp`). See [docs/dhan-mcp.md](docs/dhan-mcp.md).

Dhan **Sandbox ≠ paper trading**. See [docs/paper-trading.md](docs/paper-trading.md).

Do not start live Dhan orders until paper PnL and fills look sane.

---

## Phase 0 — Historical data you can analyze

**Goal:** one instrument, one timeframe, stored locally, easy to query.

| Step | Command / check |
| --- | --- |
| 1. Setup | `pip install -e ".[dev]"` and fill `.env` |
| 2. Health | `algo doctor` — token, data plan, database |
| 3. Instrument master | `algo instruments sync` — NIFTY / BANKNIFTY / FINNIFTY mapped |
| 4. Download | `algo data download --instrument NIFTY --timeframe 5m --start 2025-01-01 --end 2026-01-01` |
| 5. Inspect | `algo data inspect` — first/last IST timestamp, row count |
| 6. Quality | `algo data validate` — missing / invalid / duplicates |
| 7. Analyze | `load_candles(...)` or Parquet export |

Success looks like:

```text
NIFTY  5m
Provider: dhan
First (IST): 2025-01-01 09:15
Last  (IST): 2025-12-31 15:25
Candles: tens of thousands
Duplicates: 0
Invalid: 0
Status: HEALTHY or MISSING with an explained holiday/session gap
```

What this phase includes:

- Canonical `Instrument` and `Candle` (provider-independent)
- DhanHQ adapter behind `HistoricalDataProvider`
- Date chunking (90-day intraday limit)
- Retry, rate limit, resume
- SQLite + Parquet
- NSE calendar for expected-bar checks

What this phase does **not** include:

- Strategy code, backtest engine, paper/live orders
- Dashboard, FastAPI, MCP
- Entire NSE universe
- Native 30m download (Dhan has 1/5/15/25/60, not 30)

---

## Phase 1 — Widen the dataset

Only after NIFTY 5m is trusted:

1. BANKNIFTY 5m, FINNIFTY 5m
2. Same indices on `1D` then `1m` / `15m` / `1h`
3. Current NIFTY / BANKNIFTY / FINNIFTY futures (one expiry each)
4. A small options slice (one expiry, a few strikes)
5. Expired-options path via Dhan `/charts/rollingoption` (ATM± style, not full strike history)

Still no strategy code.

---

## Phase 2 — Backtesting

The engine asks the **canonical** store, never Dhan:

```python
data_provider.get_candles(instrument_id="NSE:INDEX:NIFTY", timeframe="5m", start=..., end=...)
```

Add:

- Signal / position / fills models
- Costs, slippage, lot size
- Continuous futures as a **derived** series (raw contracts stay separate)

---

## Phase 3 — Paper trading

Implemented as **our** paper engine + Paper Desk UI:

```bash
algo paper ui
algo paper run --strategy ema_cross --mode replay
algo paper run --strategy ema_cross --mode live
```

- Same strategy plugins for replay and live-paper
- Virtual cash / fills — never hits Dhan `/orders`
- Live mode polls Data API LTP during market hours
- Add strategy 1, then 2, then 3 from the UI

Dhan Sandbox remains optional for API payload dry-runs only.

---

## Phase 4 — Live trading

Order adapter, risk limits, kill switch. Historical code still does not import Dhan types.

---

## Phase 5 — Our canonical-store MCP

DhanHQ live MCP is already connected. This later phase is a **different** server on top of local history:

- `list_instruments`
- `get_candles`
- `data_status`
- later: `run_backtest`

It should read stored data. It should not become a second Dhan downloader.

---

## How to analyze data today

```python
from algo.analysis import load_candles

df = load_candles("NIFTY", "5m")
df["ema20"] = df["close"].ewm(span=20).mean()
```

Or:

```bash
algo data export --instrument NIFTY --timeframe 5m
# → data/parquet/NSE_INDEX_NIFTY/5m.parquet
```

Open `notebooks/01_inspect_nifty.ipynb`.

---

## After each download

1. Run tests (`pytest`)
2. `algo data inspect` and spot-check IST session times
3. `algo data validate`
4. Only then download the next instrument
