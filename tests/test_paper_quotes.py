from algo.paper.dhan_equity_ids import _parse_nse_equity_ids
from algo.paper.quotes import _extract_ltp, _extract_ohlc_batch


def test_extract_ltp_nested():
    payload = {"data": {"IDX_I": {"13": {"last_price": 22450.5}}}}
    assert _extract_ltp(payload, "IDX_I", "13") == 22450.5


def test_extract_ltp_equity():
    payload = {"data": {"NSE_EQ": {"2885": {"last_price": 2901.1}}}}
    assert _extract_ltp(payload, "NSE_EQ", "2885") == 2901.1


def test_extract_ohlc_batch():
    payload = {
        "data": {
            "NSE_EQ": {
                "2885": {
                    "last_price": 2901.1,
                    "ohlc": {"open": 2880.0, "high": 2910.0, "low": 2875.0, "close": 2870.0},
                }
            }
        }
    }
    out = _extract_ohlc_batch(payload, "NSE_EQ", {"2885": "RELIANCE"})
    assert out["RELIANCE"]["close"] == 2901.1
    assert out["RELIANCE"]["prev_close"] == 2870.0
    assert out["RELIANCE"]["open"] == 2880.0
    assert out["RELIANCE"]["source"] == "dhan"


def test_parse_nse_equity_ids_eq_series_only():
    csv_text = (
        "SEM_EXM_EXCH_ID,SEM_SEGMENT,SEM_SMST_SECURITY_ID,SEM_INSTRUMENT_NAME,"
        "SEM_TRADING_SYMBOL,SEM_SERIES\n"
        "NSE,E,2885,EQUITY,RELIANCE,EQ\n"
        "NSE,E,1000,EQUITY,656MH32,SG\n"
        "BSE,E,500325,EQUITY,RELIANCE,A\n"
    )
    mapping = _parse_nse_equity_ids(csv_text)
    assert mapping == {"RELIANCE": "2885"}
