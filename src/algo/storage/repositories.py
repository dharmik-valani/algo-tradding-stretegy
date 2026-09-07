from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import Select, func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from algo.domain.models import Candle, Instrument, InstrumentType, OptionType, ProviderMapping, QualityReport
from algo.storage.models import (
    CandleRow,
    DataQualityReportRow,
    DownloadJobRow,
    InstrumentProviderMappingRow,
    InstrumentRow,
)

UTC = ZoneInfo("UTC")


def upsert_instruments(session: Session, items: list[tuple[Instrument, ProviderMapping]]) -> int:
    count = 0
    for instrument, mapping in items:
        row = session.get(InstrumentRow, instrument.id)
        payload = _instrument_payload(instrument)
        if row is None:
            session.add(InstrumentRow(**payload))
        else:
            for key, value in payload.items():
                setattr(row, key, value)
        existing = session.scalar(
            select(InstrumentProviderMappingRow).where(
                InstrumentProviderMappingRow.instrument_id == mapping.instrument_id,
                InstrumentProviderMappingRow.provider == mapping.provider,
            )
        )
        if existing is None:
            session.add(
                InstrumentProviderMappingRow(
                    instrument_id=mapping.instrument_id,
                    provider=mapping.provider,
                    provider_instrument_id=mapping.provider_instrument_id,
                    exchange_segment=mapping.exchange_segment,
                    provider_symbol=mapping.provider_symbol,
                    provider_instrument=mapping.provider_instrument,
                )
            )
        else:
            existing.provider_instrument_id = mapping.provider_instrument_id
            existing.exchange_segment = mapping.exchange_segment
            existing.provider_symbol = mapping.provider_symbol
            existing.provider_instrument = mapping.provider_instrument
        count += 1
    return count


def get_instrument_by_symbol(
    session: Session,
    symbol: str,
    *,
    expiry: date | None = None,
    strike: float | None = None,
    option_type: str | None = None,
) -> InstrumentRow | None:
    stmt: Select[tuple[InstrumentRow]] = select(InstrumentRow).where(InstrumentRow.symbol == symbol.upper())
    if option_type:
        stmt = stmt.where(
            InstrumentRow.instrument_type == InstrumentType.OPTION.value,
            InstrumentRow.expiry == expiry,
            InstrumentRow.option_type == option_type.upper(),
        )
        if strike is not None:
            stmt = stmt.where(InstrumentRow.strike == strike)
    elif expiry:
        stmt = stmt.where(
            InstrumentRow.instrument_type == InstrumentType.FUTURE.value,
            InstrumentRow.expiry == expiry,
        )
    else:
        stmt = stmt.where(InstrumentRow.instrument_type == InstrumentType.INDEX.value)
    return session.scalar(stmt)


def get_mapping(session: Session, instrument_id: str, provider: str) -> InstrumentProviderMappingRow | None:
    return session.scalar(
        select(InstrumentProviderMappingRow).where(
            InstrumentProviderMappingRow.instrument_id == instrument_id,
            InstrumentProviderMappingRow.provider == provider,
        )
    )


def insert_candles(session: Session, candles: list[Candle]) -> int:
    if not candles:
        return 0
    rows = [
        {
            "instrument_id": c.instrument_id,
            "timestamp": c.timestamp.astimezone(UTC),
            "timeframe": c.timeframe,
            "open": c.open,
            "high": c.high,
            "low": c.low,
            "close": c.close,
            "volume": c.volume,
            "open_interest": c.open_interest,
        }
        for c in candles
    ]
    bind = session.get_bind()
    dialect = bind.dialect.name if bind is not None else "sqlite"
    if dialect == "sqlite":
        stmt = sqlite_insert(CandleRow).values(rows).on_conflict_do_nothing(
            index_elements=["instrument_id", "timeframe", "timestamp"]
        )
        result = session.execute(stmt)
        return result.rowcount or 0
    try:
        from sqlalchemy.dialects.postgresql import insert as pg_insert
    except ImportError:  # pragma: no cover
        pg_insert = sqlite_insert
    stmt = pg_insert(CandleRow).values(rows).on_conflict_do_nothing(
        index_elements=["instrument_id", "timeframe", "timestamp"]
    )
    result = session.execute(stmt)
    return result.rowcount or 0


def load_candles(
    session: Session,
    instrument_id: str,
    timeframe: str,
    start: datetime | None = None,
    end: datetime | None = None,
) -> list[Candle]:
    stmt = select(CandleRow).where(
        CandleRow.instrument_id == instrument_id,
        CandleRow.timeframe == timeframe,
    )
    if start is not None:
        stmt = stmt.where(CandleRow.timestamp >= start)
    if end is not None:
        stmt = stmt.where(CandleRow.timestamp <= end)
    stmt = stmt.order_by(CandleRow.timestamp)
    rows = session.scalars(stmt).all()
    return [
        Candle(
            instrument_id=row.instrument_id,
            timestamp=row.timestamp,
            timeframe=row.timeframe,
            open=row.open,
            high=row.high,
            low=row.low,
            close=row.close,
            volume=int(row.volume or 0),
            open_interest=int(row.open_interest) if row.open_interest is not None else None,
        )
        for row in rows
    ]


def candle_stats(session: Session, instrument_id: str, timeframe: str) -> dict:
    stmt = select(
        func.count(CandleRow.id),
        func.min(CandleRow.timestamp),
        func.max(CandleRow.timestamp),
    ).where(CandleRow.instrument_id == instrument_id, CandleRow.timeframe == timeframe)
    count, first, last = session.execute(stmt).one()
    return {"count": int(count or 0), "first": first, "last": last}


def save_quality(session: Session, report: QualityReport) -> None:
    session.add(
        DataQualityReportRow(
            instrument_id=report.instrument_id,
            timeframe=report.timeframe,
            range_start=report.start,
            range_end=report.end,
            expected=report.expected,
            actual=report.actual,
            missing=report.missing,
            duplicates=report.duplicates,
            invalid=report.invalid,
            status=report.status,
            notes="\n".join(report.notes) if report.notes else None,
        )
    )


def create_job(
    session: Session,
    *,
    instrument_id: str,
    timeframe: str,
    provider: str,
    start: date,
    end: date,
) -> DownloadJobRow:
    job = DownloadJobRow(
        instrument_id=instrument_id,
        timeframe=timeframe,
        provider=provider,
        range_start=start,
        range_end=end,
        status="running",
    )
    session.add(job)
    session.flush()
    return job


def mapping_to_domain(row: InstrumentProviderMappingRow) -> ProviderMapping:
    return ProviderMapping(
        instrument_id=row.instrument_id,
        provider=row.provider,
        provider_instrument_id=row.provider_instrument_id,
        exchange_segment=row.exchange_segment,
        provider_symbol=row.provider_symbol,
        provider_instrument=row.provider_instrument,
    )


def instrument_to_domain(row: InstrumentRow) -> Instrument:
    return Instrument(
        id=row.id,
        exchange=row.exchange,
        segment=row.segment,
        symbol=row.symbol,
        trading_symbol=row.trading_symbol,
        instrument_type=InstrumentType(row.instrument_type),
        underlying=row.underlying,
        expiry=row.expiry,
        strike=row.strike,
        option_type=OptionType(row.option_type) if row.option_type else None,
        lot_size=row.lot_size,
        tick_size=row.tick_size,
        isin=row.isin,
        active=row.active,
    )


def _instrument_payload(instrument: Instrument) -> dict:
    return {
        "id": instrument.id,
        "exchange": instrument.exchange,
        "segment": instrument.segment,
        "symbol": instrument.symbol,
        "trading_symbol": instrument.trading_symbol,
        "instrument_type": instrument.instrument_type.value,
        "underlying": instrument.underlying,
        "expiry": instrument.expiry,
        "strike": instrument.strike,
        "option_type": instrument.option_type.value if instrument.option_type else None,
        "lot_size": instrument.lot_size,
        "tick_size": instrument.tick_size,
        "isin": instrument.isin,
        "active": instrument.active,
    }
