from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pandas as pd

from algo.config import get_settings
from algo.domain.timeframes import Timeframe, parse_timeframe
from algo.storage.db import session_scope
from algo.storage.repositories import get_instrument_by_symbol, load_candles as load_candle_rows

IST = ZoneInfo("Asia/Kolkata")


def load_candles(
    instrument: str,
    timeframe: str | Timeframe,
    start: str | date | datetime | None = None,
    end: str | date | datetime | None = None,
    *,
    tz: str = "Asia/Kolkata",
) -> pd.DataFrame:
    """Load stored canonical candles as a pandas DataFrame.

    `instrument` is a symbol like NIFTY (index) or a full id like NSE:INDEX:NIFTY.
    """
    tf = timeframe if isinstance(timeframe, Timeframe) else parse_timeframe(str(timeframe))
    settings = get_settings()
    start_dt = _as_datetime(start, end=False)
    end_dt = _as_datetime(end, end=True)
    with session_scope(settings) as session:
        instrument_id = instrument
        if ":" not in instrument:
            row = get_instrument_by_symbol(session, instrument)
            if row is None:
                raise ValueError(f"Unknown instrument {instrument}. Sync instruments first.")
            instrument_id = row.id
        candles = load_candle_rows(session, instrument_id, tf.value, start=start_dt, end=end_dt)
    if not candles:
        return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume", "open_interest"])
    frame = pd.DataFrame([c.model_dump() for c in candles])
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True).dt.tz_convert(tz)
    frame = frame.set_index("timestamp").sort_index()
    return frame[["open", "high", "low", "close", "volume", "open_interest"]]


def export_parquet(
    instrument: str,
    timeframe: str | Timeframe,
    start: str | date | None = None,
    end: str | date | None = None,
) -> str:
    settings = get_settings()
    tf = timeframe if isinstance(timeframe, Timeframe) else parse_timeframe(str(timeframe))
    df = load_candles(instrument, tf, start=start, end=end)
    instrument_id = instrument.replace(":", "_")
    if ":" not in instrument:
        with session_scope(settings) as session:
            row = get_instrument_by_symbol(session, instrument)
            if row is not None:
                instrument_id = row.id.replace(":", "_")
    out_dir = settings.parquet_dir / instrument_id
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{tf.value}.parquet"
    df.to_parquet(path)
    return str(path)


def _as_datetime(value: str | date | datetime | None, *, end: bool) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        ts = datetime.combine(value, datetime.max.time() if end else datetime.min.time())
        return ts.replace(tzinfo=IST)
    parsed = date.fromisoformat(str(value)[:10])
    ts = datetime.combine(parsed, datetime.max.time() if end else datetime.min.time())
    return ts.replace(tzinfo=IST)
