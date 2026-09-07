from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from algo.domain.calendar import MarketCalendar
from algo.domain.models import Candle, QualityReport
from algo.domain.timeframes import Timeframe
from algo.domain.validation import validate_candle

IST = ZoneInfo("Asia/Kolkata")


def build_quality_report(
    candles: list[Candle],
    *,
    instrument_id: str,
    timeframe: Timeframe,
    start: date,
    end: date,
    calendar: MarketCalendar,
    provider: str = "dhan",
) -> QualityReport:
    ordered = sorted(candles, key=lambda c: c.timestamp)
    invalid = sum(1 for c in ordered if validate_candle(c))
    stamps = [_floor_minute(c.timestamp.astimezone(IST)) for c in ordered]
    duplicates = len(stamps) - len(set(stamps))
    expected = [_floor_minute(ts) for ts in calendar.expected_timestamps(start, end, timeframe)]
    actual_set = set(stamps)
    expected_set = set(expected)
    missing = len(expected_set - actual_set) if expected_set else 0
    first = ordered[0].timestamp if ordered else None
    last = ordered[-1].timestamp if ordered else None

    status = "HEALTHY"
    notes: list[str] = []
    if not ordered:
        status = "EMPTY"
        notes.append("no candles stored")
    elif invalid:
        status = "INVALID"
        notes.append(f"{invalid} invalid candles")
    elif duplicates:
        status = "DUPLICATE"
        notes.append(f"{duplicates} duplicate timestamps")
    elif missing:
        status = "MISSING"
        notes.append(
            f"{missing} expected bars not present (special sessions or provider gaps may explain some)"
        )

    return QualityReport(
        instrument_id=instrument_id,
        timeframe=timeframe.value,
        start=start,
        end=end,
        expected=len(expected_set),
        actual=len(ordered),
        missing=missing,
        duplicates=duplicates,
        invalid=invalid,
        first_timestamp=first,
        last_timestamp=last,
        provider=provider,
        status=status,
        notes=notes,
    )


def _floor_minute(ts: datetime) -> datetime:
    return ts.replace(second=0, microsecond=0)
