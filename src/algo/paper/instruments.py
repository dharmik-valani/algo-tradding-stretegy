from __future__ import annotations

"""Instrument menus for Paper Desk (indices / stocks / option underlyings)."""

INDICES = [
    {"symbol": "NIFTY", "name": "Nifty 50"},
    {"symbol": "BANKNIFTY", "name": "Bank Nifty"},
    {"symbol": "FINNIFTY", "name": "Fin Nifty"},
    {"symbol": "MIDCPNIFTY", "name": "Midcap Nifty"},
    {"symbol": "SENSEX", "name": "Sensex"},
]

STOCKS = [
    {"symbol": "RELIANCE", "name": "Reliance"},
    {"symbol": "HDFCBANK", "name": "HDFC Bank"},
    {"symbol": "ICICIBANK", "name": "ICICI Bank"},
    {"symbol": "INFY", "name": "Infosys"},
    {"symbol": "TCS", "name": "TCS"},
    {"symbol": "SBIN", "name": "SBI"},
    {"symbol": "ITC", "name": "ITC"},
    {"symbol": "BHARTIARTL", "name": "Bharti Airtel"},
    {"symbol": "LT", "name": "L&T"},
    {"symbol": "AXISBANK", "name": "Axis Bank"},
]

OPTION_UNDERLYINGS = [
    {"symbol": "NIFTY", "name": "Nifty options", "strike_step": 50},
    {"symbol": "BANKNIFTY", "name": "Bank Nifty options", "strike_step": 100},
    {"symbol": "FINNIFTY", "name": "Fin Nifty options", "strike_step": 50},
]


def instrument_catalog() -> dict:
    return {
        "indices": INDICES,
        "stocks": STOCKS,
        "option_underlyings": OPTION_UNDERLYINGS,
        "timeframes": ["1m", "5m", "15m"],
    }
