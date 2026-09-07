from datetime import datetime, timezone

from algo.domain.models import Candle
from algo.domain.validation import validate_candle


def _candle(**kwargs) -> Candle:
    base = dict(
        instrument_id="NSE:INDEX:NIFTY",
        timestamp=datetime(2026, 1, 2, 3, 45, tzinfo=timezone.utc),
        timeframe="5m",
        open=100.0,
        high=110.0,
        low=90.0,
        close=105.0,
        volume=1,
    )
    base.update(kwargs)
    return Candle(**base)


def test_valid_ohlc():
    assert validate_candle(_candle()) == []


def test_high_below_close():
    errors = validate_candle(_candle(high=101.0, open=100.0, close=105.0, low=90.0))
    assert any("high" in e for e in errors)


def test_naive_timestamp():
    errors = validate_candle(_candle(timestamp=datetime(2026, 1, 2, 3, 45)))
    assert any("naive" in e for e in errors)
