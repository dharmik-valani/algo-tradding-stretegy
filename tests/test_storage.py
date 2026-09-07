from datetime import datetime, timezone

from algo.config import Settings, get_settings
from algo.domain.models import Candle, Instrument, InstrumentType, ProviderMapping
from algo.storage.db import get_engine, reset_engine, session_scope
from algo.storage.repositories import insert_candles, load_candles, upsert_instruments


def test_duplicate_candles_are_ignored(tmp_path):
    reset_engine()
    get_settings.cache_clear()
    settings = Settings(database_url=f"sqlite:///{tmp_path / 'algo.db'}")
    get_engine(settings)
    try:
        instrument = Instrument(
            id="NSE:INDEX:NIFTY",
            exchange="NSE",
            segment="IDX_I",
            symbol="NIFTY",
            trading_symbol="NIFTY",
            instrument_type=InstrumentType.INDEX,
        )
        mapping = ProviderMapping(
            instrument_id=instrument.id,
            provider="dhan",
            provider_instrument_id="13",
            exchange_segment="IDX_I",
            provider_instrument="INDEX",
        )
        ts = datetime(2026, 1, 27, 3, 45, tzinfo=timezone.utc)
        candle = Candle(
            instrument_id=instrument.id,
            timestamp=ts,
            timeframe="5m",
            open=1,
            high=2,
            low=1,
            close=1.5,
            volume=0,
        )
        with session_scope(settings) as session:
            upsert_instruments(session, [(instrument, mapping)])
            assert insert_candles(session, [candle]) == 1
            assert insert_candles(session, [candle]) == 0
            stored = load_candles(session, instrument.id, "5m")
            assert len(stored) == 1
    finally:
        reset_engine()
        get_settings.cache_clear()
