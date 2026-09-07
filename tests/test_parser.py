import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from algo.providers.dhan.parser import parse_columnar_candles, parse_error_payload

FIXTURES = Path(__file__).parent / "fixtures" / "dhan"
UTC = ZoneInfo("UTC")


def test_parse_intraday_columnar_payload():
    payload = json.loads((FIXTURES / "intraday_candles.json").read_text())
    candles = parse_columnar_candles(payload, instrument_id="NSE:INDEX:NIFTY", timeframe="5m")
    assert len(candles) == 3
    assert candles[0].open == 22000.0
    assert candles[0].high >= max(candles[0].open, candles[0].close)
    assert candles[0].timestamp.tzinfo is not None
    assert candles[0].timestamp == datetime.fromtimestamp(1735703100, tz=UTC)


def test_parse_error_unsubscribed():
    payload = json.loads((FIXTURES / "error_unsubscribed.json").read_text())
    message, code, retryable = parse_error_payload(payload, 401)
    assert code == "DH-902"
    assert "not subscribed" in message.lower() or "DH-902" in code
    assert retryable is False
