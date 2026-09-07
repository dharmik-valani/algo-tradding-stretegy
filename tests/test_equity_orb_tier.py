from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from algo.paper.equity_orb_tier import (
    Nifty500TopGainerBrkStrategy,
    Nifty500TopLoserBrkStrategy,
    qty_for_price,
    parse_qty_tiers,
)
from algo.paper.models import Bar, SignalAction
from algo.paper.registry import list_strategies

IST = ZoneInfo("Asia/Kolkata")


def test_tier_strategies_registered():
    ids = {s["id"] for s in list_strategies()}
    assert "nifty500_top_gainer_brk" in ids
    assert "nifty500_top_loser_brk" in ids


def test_qty_tiers():
    tiers = parse_qty_tiers("80-200:500,200-500:200,600-900:100")
    assert qty_for_price(150, tiers=tiers) == 500
    assert qty_for_price(350, tiers=tiers) == 200
    assert qty_for_price(750, tiers=tiers) == 100
    assert qty_for_price(1200, tiers=tiers, fallback=50) == 50


def test_gainer_selects_top_by_pct_skips_expensive():
    strat = Nifty500TopGainerBrkStrategy(params={"top_n": 2, "max_price": 1500, "min_price": 80})
    snaps = [
        {"symbol": "CHEAP", "open": 100, "high": 120, "low": 99, "close": 118, "prev_close": 100},  # +18
        {"symbol": "MID", "open": 200, "high": 220, "low": 198, "close": 210, "prev_close": 200},  # +5
        {"symbol": "EXPENSIVE", "open": 2000, "high": 2200, "low": 1990, "close": 2100, "prev_close": 2000},  # +5 but >1500
        {"symbol": "LOW", "open": 100, "high": 101, "low": 99, "close": 100.5, "prev_close": 100},  # +0.5
    ]
    picked = strat.select_symbols(snaps)
    assert picked == ["CHEAP", "MID"]
    assert "EXPENSIVE" not in picked


def test_gainer_wide_range_uses_entry_candle_sl():
    strat = Nifty500TopGainerBrkStrategy(
        params={
            "session_open": "09:15",
            "range_minutes": 5,
            "buffer_pct": 0.2,
            "wide_range_pct": 1.0,
            "risk_reward_1": 2,
            "risk_reward_2": 3,
            "entry_end": "15:00",
        }
    )
    strat.select_symbols(
        [{"symbol": "AAA", "open": 100, "high": 105, "low": 100, "close": 104, "prev_close": 100}]
    )
    day = datetime(2026, 9, 8, 9, 15, tzinfo=IST)
    # Wide range: high 103 low 100 → ~3%
    b0 = Bar(timestamp=day, open=100, high=103, low=100, close=102, volume=1)
    hist: list[Bar] = []
    strat.on_symbol_bar("AAA", b0, hist)
    hist.append(b0)
    # Breakout bar: high/low of entry candle drive SL
    b1 = Bar(timestamp=day + timedelta(minutes=6), open=103, high=104.5, low=102.5, close=104, volume=1)
    sig = strat.on_symbol_bar("AAA", b1, hist)
    assert sig.action is SignalAction.BUY
    assert sig.meta["sl_src"] == "entry_candle_low"
    assert abs(float(sig.meta["stop"]) - 102.5 * (1 - 0.002)) < 0.02
    assert sig.meta["qty"] == 500  # ~104 in 80-200 band
    assert float(sig.meta["target_r2"]) > float(sig.meta["target_r1"]) > float(sig.meta["entry"])
    assert float(sig.meta["target_r1"]) > 106  # roughly 1:2


def test_gainer_narrow_range_uses_range_low_sl():
    strat = Nifty500TopGainerBrkStrategy(
        params={
            "session_open": "09:15",
            "range_minutes": 5,
            "buffer_pct": 0.2,
            "wide_range_pct": 1.0,
            "risk_reward_1": 2,
            "risk_reward_2": 3,
        }
    )
    strat.select_symbols(
        [{"symbol": "BBB", "open": 700, "high": 705, "low": 699, "close": 704, "prev_close": 700}]
    )
    day = datetime(2026, 9, 8, 9, 15, tzinfo=IST)
    # Narrow: 700.5 - 700 = 0.07%
    b0 = Bar(timestamp=day, open=700, high=700.5, low=700, close=700.2, volume=1)
    hist: list[Bar] = []
    strat.on_symbol_bar("BBB", b0, hist)
    hist.append(b0)
    b1 = Bar(timestamp=day + timedelta(minutes=6), open=700.5, high=701.2, low=700.4, close=701, volume=1)
    sig = strat.on_symbol_bar("BBB", b1, hist)
    assert sig.action is SignalAction.BUY
    assert sig.meta["sl_src"] == "range_low"
    assert abs(sig.meta["stop"] - 700 * (1 - 0.002)) < 1e-6
    assert sig.meta["qty"] == 100  # 600-900 band


def test_loser_breakout_and_partial_targets():
    strat = Nifty500TopLoserBrkStrategy(
        params={
            "session_open": "09:15",
            "range_minutes": 5,
            "buffer_pct": 0.2,
            "wide_range_pct": 1.0,
            "risk_reward_1": 2,
            "risk_reward_2": 3,
            "partial_at_r1_pct": 50,
        }
    )
    strat.select_symbols(
        [{"symbol": "CCC", "open": 300, "high": 300, "low": 295, "close": 296, "prev_close": 310}]
    )
    day = datetime(2026, 9, 8, 9, 15, tzinfo=IST)
    b0 = Bar(timestamp=day, open=300, high=300, low=297, close=298, volume=1)
    hist: list[Bar] = []
    strat.on_symbol_bar("CCC", b0, hist)
    hist.append(b0)
    b1 = Bar(timestamp=day + timedelta(minutes=6), open=297, high=297.2, low=295.5, close=296, volume=1)
    sig = strat.on_symbol_bar("CCC", b1, hist)
    assert sig.action is SignalAction.SELL
    assert sig.meta["qty"] == 200
    t1 = float(sig.meta["target_r1"])
    # Hit R1
    b2 = Bar(timestamp=day + timedelta(minutes=10), open=t1, high=t1 + 0.5, low=t1 - 0.2, close=t1, volume=1)
    hist.append(b1)
    sig2 = strat.on_symbol_bar("CCC", b2, hist)
    assert sig2.action is SignalAction.FLAT
    assert abs(float(sig2.meta["close_frac"]) - 0.5) < 1e-9
    assert strat._legs["CCC"]["scaled_r1"] is True
    assert strat._legs["CCC"]["in_trade"] is True
