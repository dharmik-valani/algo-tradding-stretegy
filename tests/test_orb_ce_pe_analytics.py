from algo.paper.analytics import summarize_closed_trades
from algo.paper.registry import create_strategy, list_strategies
from algo.paper.broker import PaperBroker
from algo.paper.models import Side
from datetime import datetime, timezone


def test_ce_and_pe_registered():
    ids = {s["id"] for s in list_strategies()}
    assert "nifty_opt_orb_ce" in ids
    assert "nifty_opt_orb_pe" in ids
    ce = create_strategy("nifty_opt_orb_ce")
    pe = create_strategy("nifty_opt_orb_pe")
    assert ce.default_params()["option_type"] == "CE"
    assert pe.default_params()["option_type"] == "PE"
    assert ce.params["option_type"] == "CE"
    assert pe.params["option_type"] == "PE"
    # locked — PE param cannot override Call strategy
    locked = create_strategy("nifty_opt_orb_ce", params={"option_type": "PE"})
    assert locked.params["option_type"] == "CE"


def test_closed_trade_analytics():
    broker = PaperBroker(starting_cash=100_000)
    broker.bind("nifty_opt_orb_ce", "NSE:OPT:NIFTY:CE:ATM", "NIFTY")
    ts = datetime(2026, 9, 7, 4, 0, tzinfo=timezone.utc)
    broker.submit_market(
        strategy_id="nifty_opt_orb_ce",
        instrument_id="x",
        symbol="NIFTY",
        side=Side.BUY,
        quantity=1,
        last_price=100,
        ts=ts,
    )
    broker.submit_market(
        strategy_id="nifty_opt_orb_ce",
        instrument_id="x",
        symbol="NIFTY",
        side=Side.SELL,
        quantity=1,
        last_price=120,
        ts=ts,
    )
    broker.submit_market(
        strategy_id="nifty_opt_orb_ce",
        instrument_id="x",
        symbol="NIFTY",
        side=Side.BUY,
        quantity=1,
        last_price=100,
        ts=ts,
    )
    broker.submit_market(
        strategy_id="nifty_opt_orb_ce",
        instrument_id="x",
        symbol="NIFTY",
        side=Side.SELL,
        quantity=1,
        last_price=90,
        ts=ts,
    )
    stats = summarize_closed_trades(broker.closed_trades)
    assert stats["trades"] == 2
    assert stats["wins"] == 1
    assert stats["losses"] == 1
    assert stats["win_rate"] == 50.0
    assert stats["avg_win"] > 0
    assert stats["avg_loss"] < 0
