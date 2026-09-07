import pytest

from algo.domain.timeframes import Timeframe, parse_timeframe


def test_parse_aliases():
    assert parse_timeframe("5m") is Timeframe.M5
    assert parse_timeframe("1D") is Timeframe.D1
    assert parse_timeframe("60") is Timeframe.H1


def test_reject_native_30m():
    with pytest.raises(ValueError, match="30m"):
        parse_timeframe("30m")
