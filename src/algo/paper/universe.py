from __future__ import annotations

"""Liquid NIFTY 500–style equity universe for paper scanners.

Full official NIFTY 500 is large for public-quote polling; this curated liquid
subset is scanned by default. Override via strategy param ``universe`` (comma symbols).
"""

# Liquid large/midcaps commonly in NIFTY 500 — paper scan default.
NIFTY500_PAPER_UNIVERSE: list[str] = [
    "RELIANCE",
    "HDFCBANK",
    "ICICIBANK",
    "INFY",
    "TCS",
    "SBIN",
    "ITC",
    "BHARTIARTL",
    "LT",
    "AXISBANK",
    "KOTAKBANK",
    "HINDUNILVR",
    "BAJFINANCE",
    "ASIANPAINT",
    "MARUTI",
    "SUNPHARMA",
    "TITAN",
    "WIPRO",
    "ULTRACEMCO",
    "NESTLEIND",
    "POWERGRID",
    "NTPC",
    "ONGC",
    "TATAMOTORS",
    "TATASTEEL",
    "JSWSTEEL",
    "ADANIENT",
    "ADANIPORTS",
    "TECHM",
    "HCLTECH",
    "BAJAJFINSV",
    "INDUSINDBK",
    "CIPLA",
    "DRREDDY",
    "DIVISLAB",
    "APOLLOHOSP",
    "EICHERMOT",
    "HEROMOTOCO",
    "BAJAJ-AUTO",
    "M&M",
    "GRASIM",
    "COALINDIA",
    "BPCL",
    "IOC",
    "HINDALCO",
    "VEDL",
    "SBILIFE",
    "HDFCLIFE",
    "ICICIGI",
    "PIDILITIND",
    "DABUR",
    "BRITANNIA",
    "GODREJCP",
    "HAVELLS",
    "VOLTAS",
    "SIEMENS",
    "ABB",
    "BEL",
    "HAL",
    "IRCTC",
    "ZOMATO",
    "PAYTM",
    "NYKAA",
    "POLICYBZR",
    "DMART",
    "TRENT",
    "PAGEIND",
    "DIXON",
    "POLYCAB",
    "CUMMINSIND",
    "ASHOKLEY",
    "TVSMOTOR",
    "BANKBARODA",
    "PNB",
    "CANBK",
    "FEDERALBNK",
    "IDFCFIRSTB",
    "RECLTD",
    "PFC",
    "IRFC",
    "GAIL",
    "PETRONET",
    "IGL",
    "MGL",
    "DLF",
    "GODREJPROP",
    "OBEROIRLTY",
    "LODHA",
    "AMBUJACEM",
    "SHREECEM",
    "DALBHARAT",
    "TORNTPHARM",
    "LUPIN",
    "AUROPHARMA",
    "BIOCON",
    "LAURUSLABS",
    "MAXHEALTH",
    "FORTIS",
    "NAUKRI",
    "PERSISTENT",
    "COFORGE",
    "LTIM",
    "MPHASIS",
    "OFSS",
]


def resolve_universe(params: dict | None = None) -> list[str]:
    raw = (params or {}).get("universe") or ""
    if isinstance(raw, str) and raw.strip():
        return [s.strip().upper() for s in raw.split(",") if s.strip()]
    mode = str((params or {}).get("universe_mode") or "liquid").strip().lower()
    limit = int((params or {}).get("scan_size") or len(NIFTY500_PAPER_UNIVERSE))
    limit = max(1, limit)
    if mode in {"nse_eq", "nse", "all_nse"}:
        from algo.paper.dhan_equity_ids import list_mainboard_symbols

        return list_mainboard_symbols(include_bse=False)[:limit]
    if mode in {"nse_bse_eq", "nse_bse", "all"}:
        from algo.paper.dhan_equity_ids import list_mainboard_symbols

        return list_mainboard_symbols(include_bse=True)[:limit]
    return list(NIFTY500_PAPER_UNIVERSE[:limit])
