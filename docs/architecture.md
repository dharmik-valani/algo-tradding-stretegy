# Architecture — historical data

```text
                 DhanHQ v2
                     │
                     ▼
            DhanHistoricalDataProvider
                     │
                     ▼
              data/raw/dhan/...          (optional audit copy)
                     │
                     ▼
         parse → normalize → validate
                     │
                     ▼
           SQLite / Postgres candles
                     │
          ┌──────────┴──────────┐
          ▼                     ▼
   algo data inspect      pandas / Parquet
          │
          ▼
    later: backtest (canonical candles only)
```

The application never calls Dhan URLs outside `src/algo/providers/dhan/`.

## Canonical identity

Dhan `securityId` is **not** unique by itself (NIFTY index and some equities share numeric IDs). Our primary key is a stable string:

```text
NSE:INDEX:NIFTY
NSE:FUTIDX:NIFTY:2026-09-24
NSE:OPTIDX:NIFTY:2026-09-24:25000:CE
```

Provider IDs live in `instrument_provider_mapping` (`provider`, `provider_instrument_id`, `exchange_segment`).

## Timestamps

- Store UTC, timezone-aware.
- Interpret market sessions in `Asia/Kolkata`.
- Dhan returns epoch seconds; we convert with `datetime.fromtimestamp(ts, tz=UTC)`.
- First NIFTY inspect should show ~09:15 IST for the first regular bar. If it shows 03:45 or 14:45, the epoch convention needs a documented fix — do not silently shift.

## Timeframes

| Canonical | Dhan |
| --- | --- |
| 1m | intraday interval `1` |
| 5m | `5` |
| 15m | `15` |
| 25m | `25` (native only) |
| 1h | `60` |
| 1D | `/charts/historical` |

There is **no** native 30m. Resample from 5m in a later derived dataset.

## Storage

Default: `sqlite:///./data/algo.db` for local analysis.

Same SQLAlchemy models work with Postgres/Timescale later (`DATABASE_URL`). Unique key:

```text
(instrument_id, timeframe, timestamp)
```

Raw JSON: `data/raw/dhan/candles/<instrument>/<timeframe>/<from>_<to>.json`

Parquet: `data/parquet/<instrument>/<timeframe>.parquet`

## Intraday limits (Dhan, current docs)

- Max **90 days** per `/charts/intraday` call
- About **5 years** of intraday history for active instruments
- Daily history back to inception
- Data APIs: **5 requests/second**, on the order of **100,000/day**
- `toDate` on daily candles is **non-inclusive**

Expired options use a **separate** endpoint (`/charts/rollingoption`): ATM± rolling strikes, max ~30 days per call. Do not mix that into the live instrument-master download.

## Later providers

`HistoricalDataProvider` is the only interface the downloader and (later) backtester use. Add `UpstoxHistoricalDataProvider` without changing `candles`.
