from pathlib import Path

from algo.domain.models import InstrumentType
from algo.providers.dhan.instruments import filter_universe, parse_instrument_master_csv

CSV = Path(__file__).parent / "fixtures" / "dhan" / "instrument_master_sample.csv"


def test_index_and_equity_can_share_security_id():
    items = parse_instrument_master_csv(CSV.read_text())
    nifty = next(i for i, _ in items if i.id == "NSE:INDEX:NIFTY")
    abb = next(i for i, _ in items if i.symbol == "ABB")
    assert nifty.instrument_type is InstrumentType.INDEX
    mappings = {i.id: m for i, m in items}
    assert mappings[nifty.id].provider_instrument_id == mappings[abb.id].provider_instrument_id == "13"
    assert mappings[nifty.id].exchange_segment == "IDX_I"
    assert mappings[abb.id].exchange_segment == "NSE_EQ"


def test_universe_keeps_exact_index_symbols():
    items = parse_instrument_master_csv(CSV.read_text())
    filtered = filter_universe(items, index_symbols=["NIFTY", "BANKNIFTY", "FINNIFTY"])
    ids = {i.id for i, _ in filtered}
    assert ids == {"NSE:INDEX:NIFTY", "NSE:INDEX:BANKNIFTY", "NSE:INDEX:FINNIFTY"}


def test_option_canonical_id():
    items = parse_instrument_master_csv(CSV.read_text())
    option = next(i for i, _ in items if i.instrument_type is InstrumentType.OPTION)
    assert option.id == "NSE:OPTION:NIFTY:2026-09-24:25000:CE"
