from datetime import datetime, timezone

from algo.paper.broker import PaperBroker
from algo.paper.models import Side


def test_buy_then_sell_realizes_pnl():
    broker = PaperBroker(starting_cash=100_000, fee_bps=0, slippage_bps=0)
    broker.bind("ema_cross", "NSE:INDEX:NIFTY", "NIFTY")
    ts = datetime(2026, 1, 2, 4, 0, tzinfo=timezone.utc)
    buy = broker.submit_market(
        strategy_id="ema_cross",
        instrument_id="NSE:INDEX:NIFTY",
        symbol="NIFTY",
        side=Side.BUY,
        quantity=2,
        last_price=100.0,
        ts=ts,
    )
    assert buy.status.value == "FILLED"
    assert broker.position.quantity == 2
    assert broker.cash == 100_000 - 200
    sell = broker.submit_market(
        strategy_id="ema_cross",
        instrument_id="NSE:INDEX:NIFTY",
        symbol="NIFTY",
        side=Side.SELL,
        quantity=2,
        last_price=110.0,
        ts=ts,
    )
    assert sell.status.value == "FILLED"
    assert broker.position.quantity == 0
    assert broker.realized_pnl == 20.0
    assert broker.cash == 100_000 + 20


def test_rejects_insufficient_cash():
    broker = PaperBroker(starting_cash=50, fee_bps=0, slippage_bps=0)
    broker.bind("ema_cross", "NSE:INDEX:NIFTY", "NIFTY")
    ts = datetime(2026, 1, 2, 4, 0, tzinfo=timezone.utc)
    order = broker.submit_market(
        strategy_id="ema_cross",
        instrument_id="NSE:INDEX:NIFTY",
        symbol="NIFTY",
        side=Side.BUY,
        quantity=1,
        last_price=100.0,
        ts=ts,
    )
    assert order.status.value == "REJECTED"
