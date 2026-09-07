from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from algo.domain.models import Candle

UTC = ZoneInfo("UTC")


def parse_columnar_candles(
    payload: dict,
    *,
    instrument_id: str,
    timeframe: str,
    source: str = "dhan",
) -> list[Candle]:
    opens = payload.get("open") or []
    highs = payload.get("high") or []
    lows = payload.get("low") or []
    closes = payload.get("close") or []
    volumes = payload.get("volume") or []
    timestamps = payload.get("timestamp") or []
    oi = payload.get("open_interest") or payload.get("oi") or []
    n = len(timestamps)
    if not all(len(col) == n for col in (opens, highs, lows, closes, volumes)):
        raise ValueError("Dhan candle arrays have mismatched lengths")
    candles: list[Candle] = []
    for i in range(n):
        candles.append(
            Candle(
                instrument_id=instrument_id,
                timestamp=_epoch_to_utc(timestamps[i]),
                timeframe=timeframe,
                open=float(opens[i]),
                high=float(highs[i]),
                low=float(lows[i]),
                close=float(closes[i]),
                volume=int(volumes[i] or 0),
                open_interest=_optional_int(oi[i]) if i < len(oi) else None,
                source=source,
            )
        )
    return candles


def _epoch_to_utc(value: int | float | str) -> datetime:
    return datetime.fromtimestamp(int(value), tz=UTC)


def _optional_int(value: object) -> int | None:
    if value is None or value == "":
        return None
    return int(value)


def parse_error_payload(payload: dict | list | None, status_code: int) -> tuple[str, str | None, bool]:
    retryable = status_code in {429, 500, 502, 503, 504}
    if not isinstance(payload, dict):
        return f"HTTP {status_code}", str(status_code), retryable
    # Marketfeed often returns {"data":{"806":"Data APIs not Subscribed"},"status":"failed"}
    nested = payload.get("data")
    if isinstance(nested, dict) and nested and all(isinstance(k, str) for k in nested.keys()):
        first_key = next(iter(nested))
        if first_key.isdigit() or first_key.startswith("DH-"):
            return str(nested[first_key]), str(first_key), False
    code = str(payload.get("errorCode") or payload.get("errorType") or payload.get("code") or status_code)
    remarks = payload.get("remarks") or payload.get("errorMessage") or payload.get("message")
    if isinstance(remarks, dict):
        remarks = remarks.get("error_message") or remarks.get("message") or str(remarks)
    message = str(remarks or payload.get("status") or f"HTTP {status_code}")
    if code in {"DH-904", "805", "429"} or status_code == 429:
        retryable = True
    if code in {"800", "DH-908", "DH-909"}:
        retryable = True
    return message, code, retryable
