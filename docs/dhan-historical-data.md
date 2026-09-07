# DhanHQ historical data (current as of 2026-09-06)

Researched from [DhanHQ v2 docs](https://dhanhq.co/docs/v2/historical-data/), [annexure](https://dhanhq.co/docs/v2/annexure/), [instruments](https://dhanhq.co/docs/v2/instruments/), [expired options](https://dhanhq.co/docs/v2/expired-options-data/), and [authentication](https://dhanhq.co/docs/v2/authentication/). Do not assume older SDK examples are still correct.

## Authentication

- Header: `access-token` (JWT). Also send `client-id` (`DHAN_CLIENT_ID`) on data routes.
- Individual tokens from [web.dhan.co](https://web.dhan.co) last **24 hours**.
- `/v2/profile` is the cheapest connectivity check. `dataPlan` / `dataValidity` show Data API subscription.
- **DH-902** / Data error **806**: Data APIs not subscribed.
- Static IP whitelist is required for **order** APIs, not for historical download.

## Instrument master

Public CSVs (updated ~08:30 IST):

- Compact: `https://images.dhan.co/api-data/api-scrip-master.csv`
- Detailed: `https://images.dhan.co/api-data/api-scrip-master-detailed.csv`

Compact columns we use:

```text
SEM_EXM_EXCH_ID, SEM_SEGMENT, SEM_SMST_SECURITY_ID, SEM_INSTRUMENT_NAME,
SEM_TRADING_SYMBOL, SEM_CUSTOM_SYMBOL, SM_SYMBOL_NAME, SEM_LOT_UNITS,
SEM_TICK_SIZE, SEM_EXPIRY_DATE, SEM_STRIKE_PRICE, SEM_OPTION_TYPE, SEM_EXPIRY_FLAG
```

Segment map:

| CSV `SEM_SEGMENT` | API `exchangeSegment` |
| --- | --- |
| I | IDX_I |
| E | NSE_EQ / BSE_EQ |
| D | NSE_FNO / BSE_FNO |
| C | NSE_CURRENCY / BSE_CURRENCY |
| M | MCX_COMM |

**`securityId` is not globally unique.** Compact CSV on 2026-09-06: NIFTY INDEX is `13` on `NSE`/`I`, and ABB EQUITY is also `13` on `NSE`/`E`. Always send `securityId` + `exchangeSegment` + `instrument`.

Known index underlyings (confirm via master, do not hardcode as the only source):

| Symbol | securityId | exchangeSegment | instrument |
| --- | --- | --- | --- |
| NIFTY | 13 | IDX_I | INDEX |
| BANKNIFTY | 25 | IDX_I | INDEX |
| FINNIFTY | 27 | IDX_I | INDEX |

## Historical candles

| Kind | Method | URL |
| --- | --- | --- |
| Daily | POST | `https://api.dhan.co/v2/charts/historical` |
| Intraday | POST | `https://api.dhan.co/v2/charts/intraday` |

Daily body:

```json
{
  "securityId": "13",
  "exchangeSegment": "IDX_I",
  "instrument": "INDEX",
  "expiryCode": 0,
  "oi": false,
  "fromDate": "2025-01-01",
  "toDate": "2026-01-01"
}
```

`toDate` is **non-inclusive** on daily.

Intraday body:

```json
{
  "securityId": "13",
  "exchangeSegment": "IDX_I",
  "instrument": "INDEX",
  "interval": "5",
  "oi": true,
  "fromDate": "2025-01-01",
  "toDate": "2025-03-31"
}
```

Documented constraints:

- Intraday intervals: `1`, `5`, `15`, `25`, `60` only. **No 30m.**
- **90 days** max per intraday request.
- Intraday available for about **last 5 years**, active instruments.
- Daily available back to inception.
- `oi: true` for F&O open interest; indices typically return zeros — store as nullable.

Response is **columnar arrays**, not a list of candle objects:

```json
{
  "open": [...],
  "high": [...],
  "low": [...],
  "close": [...],
  "volume": [...],
  "timestamp": [1326220200],
  "open_interest": [0]
}
```

`timestamp` is epoch seconds. We store UTC and display IST.

## Expired options (later, not Phase 0)

`POST https://api.dhan.co/v2/charts/rollingoption`

- Rolling ATM / ATM±N, not an arbitrary historical strike by security ID.
- Index options near expiry: up to ATM±10; others ATM±3.
- Max ~**30 days** per call, ~5 years history.
- `expiryFlag`: `WEEK` or `MONTH`. `drvOptionType`: `CALL` / `PUT`.
- This is a different product than “download expired contract by trading symbol”. Design a separate provider method when we reach F&O history.

## Rate limits (documented)

Support page: Data APIs **5 requests/second**. Releases/marketing: Data API daily cap on the order of **100,000** requests; per-minute/hour caps removed. Default the limiter to 5/sec and 100000/day. If Dhan returns `DH-904` / `805`, back off.

## Errors to handle

| Code | Meaning |
| --- | --- |
| DH-901 / 808–810 | Bad or expired token |
| DH-902 / 806 | Data API not subscribed |
| DH-904 / 805 | Rate limit |
| DH-907 | Bad params or no data |
| 800 / DH-908 | Server error — retry |
| 812 | Invalid date format |
| 813 | Invalid securityId |

HTTP 429 / 500 / 502 / 503 / 504 and network timeouts are retried with exponential backoff. Do not invent extra query parameters to bypass the 90-day window.

## Limitations we will not work around

1. No native 30-minute candles — resample later.
2. Intraday 90-day cap — chunk locally.
3. Expired options are ATM-relative rolling data, not a full expired contract tape.
4. Instrument CSV IDs collide across segments — never key only on securityId.
5. Access tokens expire daily unless using a longer OAuth/TOTP flow.
