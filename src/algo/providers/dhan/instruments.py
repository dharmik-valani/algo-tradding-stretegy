from __future__ import annotations

from datetime import date, datetime
from io import StringIO

import pandas as pd

from algo.domain.models import Instrument, InstrumentType, OptionType, ProviderMapping, canonical_instrument_id

SEGMENT_TO_API = {
    ("NSE", "I"): "IDX_I",
    ("BSE", "I"): "IDX_I",
    ("NSE", "E"): "NSE_EQ",
    ("BSE", "E"): "BSE_EQ",
    ("NSE", "D"): "NSE_FNO",
    ("BSE", "D"): "BSE_FNO",
    ("NSE", "C"): "NSE_CURRENCY",
    ("BSE", "C"): "BSE_CURRENCY",
    ("MCX", "M"): "MCX_COMM",
}

INSTRUMENT_TYPE_MAP = {
    "INDEX": InstrumentType.INDEX,
    "EQUITY": InstrumentType.EQUITY,
    "FUTIDX": InstrumentType.FUTURE,
    "FUTSTK": InstrumentType.FUTURE,
    "FUTCOM": InstrumentType.FUTURE,
    "FUTCUR": InstrumentType.FUTURE,
    "OPTIDX": InstrumentType.OPTION,
    "OPTSTK": InstrumentType.OPTION,
    "OPTFUT": InstrumentType.OPTION,
    "OPTCUR": InstrumentType.OPTION,
}


def parse_instrument_master_csv(text: str, *, provider: str = "dhan") -> list[tuple[Instrument, ProviderMapping]]:
    df = pd.read_csv(StringIO(text), dtype=str, keep_default_na=False)
    rows: list[tuple[Instrument, ProviderMapping]] = []
    for rec in df.to_dict(orient="records"):
        parsed = _row_to_instrument(rec, provider=provider)
        if parsed is not None:
            rows.append(parsed)
    return rows


def filter_universe(
    items: list[tuple[Instrument, ProviderMapping]],
    *,
    index_symbols: list[str],
) -> list[tuple[Instrument, ProviderMapping]]:
    wanted = {s.upper() for s in index_symbols}
    out: list[tuple[Instrument, ProviderMapping]] = []
    for instrument, mapping in items:
        if instrument.instrument_type is InstrumentType.INDEX and instrument.symbol in wanted:
            out.append((instrument, mapping))
    return out


def _row_to_instrument(row: dict, *, provider: str) -> tuple[Instrument, ProviderMapping] | None:
    exchange = (row.get("SEM_EXM_EXCH_ID") or row.get("EXCH_ID") or "").strip().upper()
    segment = (row.get("SEM_SEGMENT") or row.get("SEGMENT") or "").strip().upper()
    security_id = (row.get("SEM_SMST_SECURITY_ID") or row.get("SECURITY_ID") or "").strip()
    dhan_instrument = (row.get("SEM_INSTRUMENT_NAME") or row.get("INSTRUMENT") or "").strip().upper()
    trading_symbol = (row.get("SEM_TRADING_SYMBOL") or row.get("SYMBOL_NAME") or "").strip()
    display = (row.get("SM_SYMBOL_NAME") or row.get("SEM_CUSTOM_SYMBOL") or trading_symbol).strip()
    if not exchange or not security_id or not dhan_instrument:
        return None
    inst_type = INSTRUMENT_TYPE_MAP.get(dhan_instrument)
    if inst_type is None:
        return None
    expiry = _parse_date(row.get("SEM_EXPIRY_DATE") or row.get("SM_EXPIRY_DATE"))
    strike = _parse_float(row.get("SEM_STRIKE_PRICE") or row.get("STRIKE_PRICE"))
    option_raw = (row.get("SEM_OPTION_TYPE") or row.get("OPTION_TYPE") or "").strip().upper()
    option_type = OptionType(option_raw) if option_raw in {"CE", "PE"} else None
    symbol = _canonical_symbol(display, trading_symbol, inst_type)
    try:
        instrument_id = canonical_instrument_id(
            exchange=exchange,
            instrument_type=inst_type,
            symbol=symbol,
            expiry=expiry if inst_type in {InstrumentType.FUTURE, InstrumentType.OPTION} else None,
            strike=strike if inst_type is InstrumentType.OPTION else None,
            option_type=option_type if inst_type is InstrumentType.OPTION else None,
        )
    except ValueError:
        return None
    api_segment = SEGMENT_TO_API.get((exchange, segment), "")
    instrument = Instrument(
        id=instrument_id,
        exchange=exchange,
        segment=api_segment or segment,
        symbol=symbol,
        trading_symbol=trading_symbol or symbol,
        instrument_type=inst_type,
        underlying=symbol if inst_type is not InstrumentType.EQUITY else None,
        expiry=expiry if inst_type in {InstrumentType.FUTURE, InstrumentType.OPTION} else None,
        strike=strike if inst_type is InstrumentType.OPTION else None,
        option_type=option_type,
        lot_size=_parse_int(row.get("SEM_LOT_UNITS") or row.get("LOT_SIZE")),
        tick_size=_parse_float(row.get("SEM_TICK_SIZE") or row.get("TICK_SIZE")),
        isin=(row.get("ISIN") or "").strip() or None,
        active=True,
    )
    mapping = ProviderMapping(
        instrument_id=instrument_id,
        provider=provider,
        provider_instrument_id=security_id,
        exchange_segment=api_segment,
        provider_symbol=trading_symbol or symbol,
        provider_instrument=dhan_instrument,
    )
    return instrument, mapping


def _canonical_symbol(display: str, trading_symbol: str, inst_type: InstrumentType) -> str:
    if inst_type is InstrumentType.INDEX:
        token = (trading_symbol or display).strip().upper()
        if token in {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX"}:
            return token
        return token.replace(" ", "_")
    return (trading_symbol or display).split("-")[0].strip().upper()


def _parse_date(value: object) -> date | None:
    text = str(value or "").strip()
    if not text or text.startswith("0001-01-01"):
        return None
    try:
        return datetime.fromisoformat(text.replace(" ", "T")[:10]).date()
    except ValueError:
        return None


def _parse_float(value: object) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _parse_int(value: object) -> int | None:
    number = _parse_float(value)
    return int(number) if number is not None else None
