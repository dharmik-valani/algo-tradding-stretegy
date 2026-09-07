from __future__ import annotations

from datetime import date, datetime
from enum import Enum

from pydantic import BaseModel, Field


class InstrumentType(str, Enum):
    INDEX = "INDEX"
    EQUITY = "EQUITY"
    FUTURE = "FUTURE"
    OPTION = "OPTION"


class OptionType(str, Enum):
    CE = "CE"
    PE = "PE"


def canonical_instrument_id(
    *,
    exchange: str,
    instrument_type: InstrumentType,
    symbol: str,
    expiry: date | None = None,
    strike: float | None = None,
    option_type: OptionType | None = None,
) -> str:
    parts = [exchange.upper(), instrument_type.value, symbol.upper()]
    if instrument_type is InstrumentType.FUTURE:
        if expiry is None:
            raise ValueError("Futures require expiry")
        parts.append(expiry.isoformat())
    if instrument_type is InstrumentType.OPTION:
        if expiry is None or strike is None or option_type is None:
            raise ValueError("Options require expiry, strike, and option_type")
        strike_txt = str(int(strike)) if float(strike).is_integer() else str(strike)
        parts.extend([expiry.isoformat(), strike_txt, option_type.value])
    return ":".join(parts)


class Instrument(BaseModel):
    id: str
    exchange: str
    segment: str
    symbol: str
    trading_symbol: str
    instrument_type: InstrumentType
    underlying: str | None = None
    expiry: date | None = None
    strike: float | None = None
    option_type: OptionType | None = None
    lot_size: int | None = None
    tick_size: float | None = None
    isin: str | None = None
    active: bool = True


class ProviderMapping(BaseModel):
    instrument_id: str
    provider: str
    provider_instrument_id: str
    exchange_segment: str
    provider_symbol: str | None = None
    provider_instrument: str | None = None


class Candle(BaseModel):
    instrument_id: str
    timestamp: datetime
    timeframe: str
    open: float
    high: float
    low: float
    close: float
    volume: int = 0
    open_interest: int | None = None
    source: str = "dhan"


class DownloadChunk(BaseModel):
    start: date
    end: date


class QualityReport(BaseModel):
    instrument_id: str
    timeframe: str
    start: date
    end: date
    expected: int
    actual: int
    missing: int
    duplicates: int
    invalid: int
    first_timestamp: datetime | None = None
    last_timestamp: datetime | None = None
    provider: str = "dhan"
    status: str = "UNKNOWN"
    notes: list[str] = Field(default_factory=list)
