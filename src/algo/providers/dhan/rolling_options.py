"""Dhan /charts/rollingoption helpers for index OPTIDX history."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Literal

from algo.providers.dhan.client import DhanClient

OptSide = Literal["CALL", "PUT"]


def fetch_rolling_option(
    client: DhanClient,
    *,
    security_id: int | str = 13,
    interval: str = "5",
    strike: str = "ATM",
    option_type: OptSide = "PUT",
    expiry_flag: str = "WEEK",
    expiry_code: int = 1,
    from_date: date | str,
    to_date: date | str,
    exchange_segment: str = "NSE_FNO",
    instrument: str = "OPTIDX",
) -> dict[str, list[Any]]:
    """Return columnar series for one moneyness/side (open/high/low/close/…).

    Dhan packs CALL under data.ce and PUT under data.pe. expiryCode 0 is rejected;
    use 1 for near weekly.
    """
    body = {
        "exchangeSegment": exchange_segment,
        "interval": str(interval),
        "securityId": int(security_id),
        "instrument": instrument,
        "expiryFlag": expiry_flag,
        "expiryCode": int(expiry_code),
        "strike": strike,
        "drvOptionType": option_type,
        "requiredData": ["open", "high", "low", "close", "volume", "oi", "iv", "strike", "spot"],
        "fromDate": str(from_date)[:10],
        "toDate": str(to_date)[:10],
    }
    payload = client.post_json("/charts/rollingoption", body)
    data = payload.get("data", payload) if isinstance(payload, dict) else {}
    side_key = "pe" if option_type == "PUT" else "ce"
    series = (data or {}).get(side_key) or {}
    if not isinstance(series, dict):
        return {}
    return series


def moneyness_label(offset: int) -> str:
    if offset == 0:
        return "ATM"
    return f"ATM{offset:+d}"


def chunk_date_ranges(start: date, end: date, *, max_days: int = 28) -> list[tuple[date, date]]:
    """Inclusive start, exclusive end chunks (Dhan toDate is non-inclusive)."""
    out: list[tuple[date, date]] = []
    cur = start
    while cur < end:
        nxt = min(cur + timedelta(days=max_days), end)
        out.append((cur, nxt))
        cur = nxt
    return out


def series_to_ts_map(series: dict[str, list[Any]]) -> dict[int, dict[str, float]]:
    """Map epoch-second → {close, volume, iv, strike, spot}."""
    ts_list = series.get("timestamp") or []
    out: dict[int, dict[str, float]] = {}
    for i, raw_ts in enumerate(ts_list):
        try:
            ts = int(raw_ts)
        except (TypeError, ValueError):
            continue
        out[ts] = {
            "close": float((series.get("close") or [0])[i] or 0),
            "volume": float((series.get("volume") or [0])[i] or 0),
            "iv": float((series.get("iv") or [0])[i] or 0) / 100.0
            if (series.get("iv") or [None])[i] is not None
            and float((series.get("iv") or [0])[i] or 0) > 1.5
            else float((series.get("iv") or [0])[i] or 0),
            "strike": float((series.get("strike") or [0])[i] or 0),
            "spot": float((series.get("spot") or [0])[i] or 0),
        }
    return out


def align_bar_ts(ts: datetime) -> int:
    """Floor to epoch seconds (UTC) for join with Dhan rolling timestamps."""
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return int(ts.timestamp())
