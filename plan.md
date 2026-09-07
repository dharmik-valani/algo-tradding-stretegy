> Working plan: [ROADMAP.md](ROADMAP.md). Architecture: [docs/architecture.md](docs/architecture.md). Dhan research: [docs/dhan-historical-data.md](docs/dhan-historical-data.md).
>
> This file is the detailed Phase-1 spec. Phase 0 code lives in `src/algo/` and is limited to historical data.

# Build Historical Market Data Infrastructure Using DhanHQ

## Objective

I am building a modular algorithmic trading platform for Indian markets.

The FIRST phase is ONLY:

> Build a reliable historical market-data ingestion and storage system using DhanHQ.

Do NOT build the trading strategy, paper trading, live trading, or frontend dashboard yet.

The goal is to create a high-quality historical database that will later be used for:

```text
Historical Data
      ↓
Backtesting
      ↓
Strategy Validation
      ↓
Paper Trading
      ↓
Live Trading
```

The historical-data system must also be provider-independent so that later I can replace:

```text
DhanHQ
```

with:

```text
Upstox
Zerodha
Angel One
Fyers
CSV
Parquet
another data provider
```

without changing the database or backtesting engine.

---

# 1. First: Research DhanHQ Current API

Before implementing anything, inspect the CURRENT DhanHQ API documentation.

Do not rely on old examples or assumptions.

Verify:

* Authentication
* Instrument master
* Historical candle API
* Intraday historical API
* Daily historical API
* Futures data
* Options data
* Open Interest
* Expired contracts
* Option chain
* Rate limits
* Date-range limitations
* Pagination
* Response format
* Timestamp format
* Instrument identifiers
* Exchange/segment identifiers

Document the findings in:

```text
docs/dhan-historical-data.md
```

Important:

> If the Dhan API has limitations, do not work around them by inventing unsupported API behavior. Clearly document the limitation and design the downloader around the actual API.

---

# 2. Scope of Initial Historical Dataset

Initially support these instruments:

## Indices

```text
NIFTY
BANKNIFTY
FINNIFTY
```

## Futures

```text
NIFTY Futures
BANKNIFTY Futures
FINNIFTY Futures
```

## Options

Initially support:

```text
NIFTY CE
NIFTY PE
BANKNIFTY CE
BANKNIFTY PE
FINNIFTY CE
FINNIFTY PE
```

with:

```text
expiry
strike
option_type
underlying
```

Do not download the entire NSE universe initially.

The architecture must support it later.

---

# 3. Timeframes

Support the following canonical timeframes:

```text
1m
5m
15m
30m
1h
1D
```

The provider may return different formats.

Normalize them into our canonical timeframe values.

Example:

```text
Dhan 5-minute
        ↓
Canonical 5m
```

---

# 4. Historical Data Types

Design the system to support:

### OHLCV

```text
timestamp
open
high
low
close
volume
```

### Open Interest

For F&O:

```text
open_interest
```

Do not assume OI exists for equities/indices.

Allow nullable OI.

---

# 5. Canonical Instrument Model

Create our OWN instrument identity.

Do not use Dhan's security ID as the primary identity.

Example:

```python
Instrument:
    id
    exchange
    segment
    symbol
    trading_symbol
    instrument_type
    underlying
    expiry
    strike
    option_type
    lot_size
    tick_size
    isin
    active
```

For an option:

```text
exchange = NSE
segment = FNO
underlying = NIFTY
expiry = 2026-09-24
strike = 25000
option_type = CE
```

Dhan-specific IDs should be stored separately:

```text
instrument_provider_mapping
```

Example:

```text
instrument_id
provider
provider_instrument_id
provider_symbol
```

This is mandatory because another provider will have different instrument IDs.

---

# 6. Canonical Candle Model

Create:

```python
Candle:
    instrument_id
    timestamp
    timeframe
    open
    high
    low
    close
    volume
    open_interest
```

Rules:

* timestamp must be timezone-aware
* use Asia/Kolkata for market interpretation
* store timestamps consistently
* do not mix local and UTC timestamps
* document the chosen database timestamp convention

Prefer storing timestamps in UTC internally while retaining correct Indian-market conversion.

---

# 7. Database

Use:

```text
PostgreSQL
```

Prefer:

```text
TimescaleDB
```

if it does not introduce unnecessary complexity.

Create:

```text
instruments
instrument_provider_mapping

candles

data_download_jobs
data_quality_reports
```

Future tables can be added later.

---

# 8. Candle Table

Design the candle table for large-scale time-series data.

Required fields:

```text
id
instrument_id
timestamp
timeframe
open
high
low
close
volume
open_interest
created_at
```

Create an appropriate unique constraint so the same candle cannot be inserted twice.

The logical unique key should be:

```text
instrument_id
+
timeframe
+
timestamp
```

Create indexes optimized for:

```text
instrument_id
timeframe
timestamp
```

---

# 9. DhanHQ Adapter

Create:

```text
DhanHistoricalDataProvider
```

It should implement a generic interface:

```python
class HistoricalDataProvider(ABC):

    def get_historical_candles(
        self,
        instrument,
        timeframe,
        start,
        end
    ):
        pass
```

The core application must NOT directly depend on DhanHQ.

Architecture:

```text
Application
    ↓
HistoricalDataProvider
    ↓
DhanHistoricalDataProvider
    ↓
DhanHQ API
```

Later:

```text
HistoricalDataProvider
        ↓
 ┌──────┼────────┐
 │      │        │
Dhan  Upstox  Zerodha
```

---

# 10. Instrument Master

Before downloading candles, build an instrument-master ingestion process.

The system must know:

```text
instrument
provider ID
exchange
segment
expiry
strike
option type
lot size
tick size
```

For derivatives, do NOT manually construct contract IDs.

Use the provider's official instrument metadata.

Create:

```text
InstrumentMasterService
```

Responsibilities:

```text
Download provider instrument master
Parse
Normalize
Validate
Store
Map to canonical instruments
```

---

# 11. Historical Downloader

Create a robust downloader.

Example CLI:

```bash
python -m app.jobs.download_historical \
  --symbol NIFTY \
  --timeframe 5m \
  --start 2022-01-01 \
  --end 2026-09-01
```

It must support:

```text
--symbol
--instrument-id
--timeframe
--start
--end
```

For options/futures also support:

```text
--expiry
--strike
--option-type
```

---

# 12. Automatic Date Chunking

Do NOT assume Dhan allows an unlimited date range in one API request.

Create a date-chunking system based on the actual Dhan API limitations.

Example:

```text
2022-01-01
      ↓
Chunk 1
      ↓
Chunk 2
      ↓
Chunk 3
      ↓
2026-09-01
```

The chunk size must be configurable.

Example:

```yaml
historical_data:
  chunk_size_days: 90
```

If Dhan requires a smaller window for a specific timeframe, automatically adapt to the documented limit.

---

# 13. Resume Capability

The downloader must survive:

* Internet failure
* API failure
* computer restart
* process crash
* rate limit
* partial download

If I start downloading:

```text
2022 → 2026
```

and it stops in 2024, restarting the command should continue from where it stopped.

Do not download duplicate data.

---

# 14. Retry Logic

Implement:

```text
retry
exponential backoff
maximum retry count
```

Handle:

```text
429
500
502
503
504
network timeout
connection reset
```

Do not endlessly retry.

---

# 15. Rate Limiting

Respect DhanHQ's current documented API rate limits.

Create:

```text
RateLimiter
```

Do not hardcode arbitrary high request rates.

Configuration:

```yaml
dhan:
  rate_limit:
    requests_per_second: configurable
```

Use the actual documented limits as the default.

---

# 16. Raw Data Storage

Keep the original provider response before normalization when practical.

Example:

```text
data/raw/dhan/
    candles/
    instruments/
```

Structure:

```text
data/raw/dhan/candles/NIFTY/5m/2026-01-01.json
```

or use Parquet where more appropriate.

Raw data is useful for:

* debugging
* auditing
* provider migration
* investigating bad data

---

# 17. Normalization Pipeline

Use:

```text
Dhan Response
      ↓
Raw Storage
      ↓
Parser
      ↓
Normalizer
      ↓
Validator
      ↓
Deduplicator
      ↓
PostgreSQL
```

Never insert raw API objects directly into the database.

---

# 18. Data Validation

Every downloaded batch must be validated.

Check:

### OHLC

```text
open > 0
high > 0
low > 0
close > 0

high >= max(open, close)
low <= min(open, close)
```

### Volume

```text
volume >= 0
```

### OI

```text
open_interest >= 0
```

where applicable.

### Timestamp

Check:

```text
timezone
ordering
duplicates
future timestamps
```

---

# 19. Duplicate Detection

The system must detect:

```text
same instrument
same timeframe
same timestamp
```

and never create duplicate candles.

Use database-level protection, not only application-level checking.

---

# 20. Missing Candle Detection

Build a data-quality service.

For each instrument/timeframe/date range, calculate:

```text
expected candles
actual candles
missing candles
duplicate candles
invalid candles
```

Example:

```text
NIFTY 5m
2026-01-01 → 2026-01-31

Expected: 1,755
Actual:    1,755
Missing:   0
Duplicate: 0
Invalid:   0

Status: PASS
```

Do NOT simply assume every weekday has the same number of candles.

Account for:

* weekends
* Indian market holidays
* special trading sessions
* exchange holidays
* contract expiry
* instrument lifecycle

````

---

# 21. Market Calendar

Do not implement candle completeness using Monday-Friday alone.

Create:

```text
MarketCalendar
````

It should eventually support:

```text
NSE
BSE
MCX
```

Initially implement NSE.

Account for Indian market holidays.

Keep the calendar replaceable/updatable.

---

# 22. Futures

Design futures handling properly.

A futures contract must contain:

```text
underlying
expiry
contract
```

Do not merge all futures into one continuous series at the database level.

Store the actual contracts separately.

Example:

```text
NIFTY
2026-09-24
2026-10-29
2026-11-26
```

Each is a distinct instrument.

Later the backtesting engine can create:

```text
continuous futures
```

as a derived dataset.

---

# 23. Options

Options must be uniquely identified by:

```text
underlying
expiry
strike
option_type
```

Example:

```text
NIFTY
2026-09-24
25000
CE
```

Do not identify options only by trading symbol.

Store actual contracts.

---

# 24. Expired Contracts

The architecture must support expired derivatives.

This is important because historical F&O backtesting requires expired contracts.

Research and confirm how Dhan currently exposes:

```text
expired futures
expired options
historical expired contracts
```

If Dhan provides a dedicated API, create a separate provider method:

```python
get_expired_derivative_data(...)
```

Do not mix expired-contract retrieval logic into the normal live instrument workflow.

---

# 25. Data Provider Abstraction

Create:

```python
class HistoricalDataProvider:
    get_instruments()
    get_historical_candles()
    get_expired_contracts()
```

Dhan implementation:

```python
class DhanHistoricalDataProvider(HistoricalDataProvider):
    ...
```

Future implementations:

```text
UpstoxHistoricalDataProvider
ZerodhaHistoricalDataProvider
CSVHistoricalDataProvider
ParquetHistoricalDataProvider
```

---

# 26. Data Source Metadata

Every dataset should record:

```text
provider
provider_version/API version
download_timestamp
source
instrument
timeframe
start
end
```

This is important for reproducible backtests.

---

# 27. Dataset Versioning

Create a concept of:

```text
Dataset
Dataset Version
```

Example:

```text
NIFTY 5m
Dataset Version: 1
Provider: Dhan
Downloaded: 2026-09-06
Range: 2022-01-01 → 2026-09-01
```

If the data is later corrected or re-downloaded, create a new version or record the update.

Do not silently overwrite historical datasets without tracking it.

---

# 28. Data API

Create internal API endpoints:

```text
GET /historical/instruments
GET /historical/candles
GET /historical/status
GET /historical/jobs
GET /historical/data-quality
```

Example:

```text
GET /historical/candles?
instrument=NIFTY&
timeframe=5m&
start=2026-01-01&
end=2026-02-01
```

---

# 29. CLI

Create:

```bash
algo instruments sync
algo data download
algo data validate
algo data status
algo data repair
```

Example:

```bash
algo data download \
  --instrument NIFTY \
  --timeframe 5m \
  --start 2022-01-01 \
  --end 2026-09-01
```

---

# 30. Data Status

The CLI should show:

```text
Instrument
Timeframe
First candle
Last candle
Total candles
Missing candles
Duplicates
Data quality
Provider
```

Example:

```text
NIFTY
5m

Provider: Dhan
First: 2022-01-03
Last: 2026-09-01

Candles: 350,421
Missing: 0
Duplicates: 0
Invalid: 0

Status: HEALTHY
```

---

# 31. Performance

Historical datasets may become very large.

Optimize for:

```text
bulk inserts
batch processing
database indexes
Parquet
compressed raw files
```

Do not insert one row at a time if avoidable.

Use bulk insert/copy mechanisms.

---

# 32. Future Scale

The architecture should eventually support:

```text
Thousands of instruments
Millions/billions of candles
Multiple timeframes
Multiple providers
Multiple exchanges
```

But do not over-engineer the MVP.

Build clean abstractions and efficient database access.

---

# 33. Timezone Rules

Indian market data must be handled correctly.

Use:

```text
Asia/Kolkata
```

for market session logic.

Internally prefer:

```text
UTC
```

for database timestamps if appropriate.

Never use naive datetime objects.

All timestamps must be timezone-aware.

---

# 34. Market Sessions

Initially support NSE equity/F&O session logic.

Do not hardcode:

```text
09:15
15:30
```

throughout the application.

Create:

```text
MarketSessionService
```

so session rules can be changed later.

---

# 35. Configuration

Use:

```text
.env
```

for credentials.

Example:

```env
DHAN_CLIENT_ID=
DHAN_ACCESS_TOKEN=
```

Never commit credentials.

Create:

```text
.env.example
```

with placeholders.

---

# 36. Security

Dhan credentials must ONLY exist server-side.

Never expose:

```text
DHAN_ACCESS_TOKEN
CLIENT_SECRET
```

to frontend.

Do not log secrets.

Mask sensitive information in logs.

---

# 37. Testing

Create unit tests for:

```text
Dhan response parser
Normalizer
Instrument mapping
Timestamp conversion
Candle validation
Deduplication
Date chunking
Rate limiting
Retry logic
```

Create integration tests for:

```text
Dhan API → parser → database
```

Use mocked Dhan responses for automated tests.

Tests must not require real API credentials.

---

# 38. Mock Data

Create fixtures:

```text
tests/fixtures/dhan/
```

containing representative:

```text
equity/index candle response
futures response
options response
OI response
instrument master response
error response
```

Use them for testing.

---

# 39. Do NOT Build Yet

Do NOT implement:

```text
Strategy Engine
Backtesting Engine
Paper Trading
Live Trading
Order Execution
Risk Engine
Trading Dashboard
AI Strategy Generator
Machine Learning
```

These are later phases.

The ONLY goal right now is:

> **Reliable historical market-data infrastructure.**

---

# 40. First Milestone

The first successful milestone is:

```text
DhanHQ
   ↓
Instrument Master
   ↓
NIFTY
   ↓
5-minute historical data
   ↓
Normalize
   ↓
Validate
   ↓
PostgreSQL/TimescaleDB
   ↓
Data Quality Report
```

It must be possible to run:

```bash
algo data download \
  --instrument NIFTY \
  --timeframe 5m \
  --start 2025-01-01 \
  --end 2026-01-01
```

and get:

```text
Download complete
Rows inserted: XXXXX
Duplicates: 0
Invalid: 0
Missing: X
Data quality: PASS
```

---

# 41. Second Milestone

After NIFTY works, test:

```text
BANKNIFTY 5m
FINNIFTY 5m
NIFTY Futures
BANKNIFTY Futures
NIFTY Options
```

Only after all of these work should we expand the dataset.

---

# 42. Important Design Requirement

The future backtesting engine should be able to request data like:

```python
data_provider.get_candles(
    instrument_id="...",
    timeframe="5m",
    start=start_date,
    end=end_date
)
```

without knowing that the original data came from Dhan.

The backtesting engine should simply see:

```text
Canonical Candle
```

---

# 43. Final Architecture

The final historical-data subsystem should look like:

```text
                    DHANHQ
                       │
                       ▼
             Dhan API Adapter
                       │
                       ▼
              Raw Data Storage
                       │
                       ▼
                Normalizer
                       │
                       ▼
                 Validator
                       │
                       ▼
                Deduplicator
                       │
                       ▼
              PostgreSQL/Timescale
                       │
              ┌────────┴─────────┐
              │                  │
              ▼                  ▼
        Data Quality        Historical API
              │                  │
              └────────┬─────────┘
                       ▼
                Future Backtest
```

Later:

```text
                    DATA LAYER

        ┌──────────┬──────────┬──────────┐
        │          │          │          │
      Dhan       Upstox    Zerodha      CSV
        │          │          │          │
        └──────────┴──────────┴──────────┘
                       │
                 Normalization
                       │
                 Canonical Data
                       │
              PostgreSQL/Timescale
                       │
                  Backtesting
```

---

# 44. Implementation Rules

Before coding:

1. Inspect the current repository.
2. Inspect the current DhanHQ documentation.
3. Create an architecture document.
4. Create the database schema.
5. Create provider interfaces.
6. Implement Dhan adapter.
7. Implement instrument master.
8. Implement historical downloader.
9. Implement normalization.
10. Implement validation.
11. Implement database storage.
12. Implement tests.
13. Run a real NIFTY 5m download.
14. Verify the stored data manually.
15. Only then proceed to additional instruments.

After each phase:

```text
Run tests
Fix errors
Update documentation
Verify data
```

Do not move forward with broken data.

---

# FINAL SUCCESS CRITERIA

This phase is complete only when:

* Dhan authentication works
* Instrument master works
* NIFTY 5m historical download works
* Data is normalized
* Data is stored efficiently
* Duplicate protection works
* Missing-data detection works
* Retry/resume works
* Date chunking works
* Timestamps are correct
* Data-quality reports work
* The Dhan implementation is isolated behind an interface
* No future backtesting code depends directly on Dhan

The result must be a **clean historical-data foundation**, not a trading bot.
