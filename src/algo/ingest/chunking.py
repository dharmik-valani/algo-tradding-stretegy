from __future__ import annotations

from datetime import date, timedelta

from algo.domain.models import DownloadChunk
from algo.domain.timeframes import Timeframe


def date_chunks(
    start: date,
    end: date,
    timeframe: Timeframe,
    *,
    intraday_days: int = 90,
    daily_days: int = 365,
) -> list[DownloadChunk]:
    """Split [start, end) into provider-safe windows.

    Dhan intraday: max 90 days per call.
    Dhan daily: toDate is non-inclusive; we still chunk to keep payloads small.
    """
    if end <= start:
        return []
    size = timedelta(days=intraday_days if timeframe.is_intraday else daily_days)
    chunks: list[DownloadChunk] = []
    cursor = start
    while cursor < end:
        nxt = min(cursor + size, end)
        chunks.append(DownloadChunk(start=cursor, end=nxt))
        cursor = nxt
    return chunks
