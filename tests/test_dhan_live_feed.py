from __future__ import annotations

import struct

from algo.providers.dhan.live_feed import parse_header, parse_quote_payload, parse_ticker_payload


def _header(code: int, length: int, seg: int, sid: int) -> bytes:
    return bytes([code]) + struct.pack("<h", length) + bytes([seg]) + struct.pack("<i", sid)


def test_parse_ticker_packet():
    # NSE_EQ=1, security 1333, ltp 100.5, ltt epoch
    body = _header(2, 16, 1, 1333) + struct.pack("<f", 100.5) + struct.pack("<i", 1700000000)
    pkt = parse_ticker_payload(body)
    assert pkt["segment"] == "NSE_EQ"
    assert pkt["security_id"] == "1333"
    assert abs(pkt["ltp"] - 100.5) < 0.01


def test_parse_quote_packet_ohlc():
    hdr = _header(4, 50, 1, 11536)
    # ltp, last_qty, ltt, atp, volume, sell, buy, open, close, high, low
    payload = (
        struct.pack("<f", 4525.55)
        + struct.pack("<h", 10)
        + struct.pack("<i", 1700000000)
        + struct.pack("<f", 4520.0)
        + struct.pack("<i", 1000)
        + struct.pack("<i", 0)
        + struct.pack("<i", 0)
        + struct.pack("<f", 4521.45)
        + struct.pack("<f", 4507.85)
        + struct.pack("<f", 4530.0)
        + struct.pack("<f", 4500.0)
    )
    pkt = parse_quote_payload(hdr + payload)
    assert pkt["segment"] == "NSE_EQ"
    assert pkt["security_id"] == "11536"
    assert abs(pkt["open"] - 4521.45) < 0.02
    assert abs(pkt["high"] - 4530.0) < 0.02
    assert abs(pkt["low"] - 4500.0) < 0.02


def test_parse_header_index_segment():
    buf = _header(2, 16, 0, 13) + struct.pack("<f", 24000.0) + struct.pack("<i", 1)
    code, length, segment, sid = parse_header(buf)
    assert code == 2
    assert segment == "IDX_I"
    assert sid == "13"
