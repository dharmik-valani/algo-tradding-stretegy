from __future__ import annotations

"""Free Dhan instrument master → NSE equity securityId map.

Master CSV is public (no Data API plan required):
https://images.dhan.co/api-data/api-scrip-master.csv

Live LTP/OHLC still need an active Dhan Data API subscription.
"""

import csv
import io
import time

import httpx

from algo.config import ROOT

MASTER_URL = "https://images.dhan.co/api-data/api-scrip-master.csv"
CACHE_PATH = ROOT / "data" / "dhan_scrip_master_eq.csv"
CACHE_MAX_AGE_SEC = 20 * 3600  # refresh ~daily

# Well-known NSE equities (fallback if master download fails).
KNOWN_NSE_EQ: dict[str, str] = {
    "RELIANCE": "2885",
    "HDFCBANK": "1333",
    "TCS": "11536",
    "INFY": "1594",
    "ICICIBANK": "4963",
    "SBIN": "3045",
    "ITC": "1660",
    "BHARTIARTL": "10604",
    "LT": "11483",
    "AXISBANK": "5900",
    "KOTAKBANK": "1922",
    "HINDUNILVR": "1394",
    "BAJFINANCE": "317",
    "ASIANPAINT": "236",
    "MARUTI": "10999",
    "SUNPHARMA": "3351",
    "TITAN": "3506",
    "WIPRO": "3787",
    "ULTRACEMCO": "11532",
    "NTPC": "11630",
    "POWERGRID": "14977",
    "ONGC": "2475",
    "TATAMOTORS": "3456",
    "TATASTEEL": "3499",
    "JSWSTEEL": "11723",
    "ADANIENT": "25",
    "ADANIPORTS": "15083",
    "TECHM": "13538",
    "HCLTECH": "7229",
    "BAJAJFINSV": "16675",
    "INDUSINDBK": "5258",
    "CIPLA": "694",
    "DRREDDY": "881",
    "DIVISLAB": "10940",
    "EICHERMOT": "910",
    "HEROMOTOCO": "1348",
    "M&M": "2031",
    "GRASIM": "1232",
    "COALINDIA": "20374",
    "BPCL": "526",
    "HINDALCO": "1363",
    "SBILIFE": "21808",
    "HDFCLIFE": "467",
    "BAJAJ-AUTO": "16669",
}

_EQ_MAP: dict[str, str] | None = None
_EQ_LOADED_AT = 0.0


def nse_eq_security_id(symbol: str) -> str | None:
    symbol = symbol.upper().replace(".NS", "")
    mapping = get_nse_eq_map()
    return mapping.get(symbol)


def get_nse_eq_map(*, force_refresh: bool = False) -> dict[str, str]:
    global _EQ_MAP, _EQ_LOADED_AT
    now = time.time()
    if not force_refresh and _EQ_MAP is not None and (now - _EQ_LOADED_AT) < CACHE_MAX_AGE_SEC:
        return _EQ_MAP
    mapping = dict(KNOWN_NSE_EQ)
    try:
        text = _load_master_csv()
        mapping.update(_parse_nse_equity_ids(text))
    except Exception:
        pass
    _EQ_MAP = mapping
    _EQ_LOADED_AT = now
    return mapping


def _load_master_csv() -> str:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    if CACHE_PATH.exists() and (time.time() - CACHE_PATH.stat().st_mtime) < CACHE_MAX_AGE_SEC:
        return CACHE_PATH.read_text(encoding="utf-8", errors="ignore")
    with httpx.Client(timeout=60.0, headers={"User-Agent": "algo-paper-desk/0.1"}) as http:
        response = http.get(MASTER_URL)
        response.raise_for_status()
        text = response.text
    CACHE_PATH.write_text(text, encoding="utf-8")
    return text


def _parse_nse_equity_ids(text: str) -> dict[str, str]:
    """NSE cash EQ series only (skip bonds / SDL / SM)."""
    reader = csv.DictReader(io.StringIO(text))
    out: dict[str, str] = {}
    for row in reader:
        exch = (row.get("SEM_EXM_EXCH_ID") or "").strip().upper()
        seg = (row.get("SEM_SEGMENT") or "").strip().upper()
        inst = (row.get("SEM_INSTRUMENT_NAME") or "").strip().upper()
        series = (row.get("SEM_SERIES") or "").strip().upper()
        if exch != "NSE" or seg != "E" or inst != "EQUITY":
            continue
        if series and series != "EQ":
            continue
        sym = (row.get("SEM_TRADING_SYMBOL") or "").strip().upper()
        sid = (row.get("SEM_SMST_SECURITY_ID") or "").strip()
        if not sym or not sid:
            continue
        out[sym] = sid
    return out
