from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from algo.paper.equity_orb import Nifty500GainerOrbStrategy, Nifty500LoserOrbStrategy
from algo.paper.journal import list_trades, open_trade, close_trade, start_journal_session, report_summary
from algo.paper.models import Bar, SignalAction
from algo.paper.registry import list_strategies

IST = ZoneInfo("Asia/Kolkata")


def test_equity_orb_registered():
    ids = {s["id"] for s in list_strategies()}
    assert "nifty500_gainer_orb" in ids
    assert "nifty500_loser_orb" in ids


def test_gainer_selects_open_eq_low():
    strat = Nifty500GainerOrbStrategy(params={"top_n": 2, "open_eq_tol_pct": 0.05})
    snaps = [
        {"symbol": "AAA", "open": 100, "high": 110, "low": 100, "close": 108, "prev_close": 100},  # +8%
        {"symbol": "BBB", "open": 100, "high": 105, "low": 99, "close": 104, "prev_close": 100},  # open≠low
        {"symbol": "CCC", "open": 50, "high": 60, "low": 50, "close": 59, "prev_close": 50},  # +18%
        {"symbol": "DDD", "open": 200, "high": 201, "low": 200, "close": 200.5, "prev_close": 200},  # +0.25%
    ]
    picked = strat.select_symbols(snaps)
    assert picked == ["CCC", "AAA"]


def test_loser_selects_open_eq_high():
    strat = Nifty500LoserOrbStrategy(params={"top_n": 2, "open_eq_tol_pct": 0.05})
    snaps = [
        {"symbol": "AAA", "open": 100, "high": 100, "low": 90, "close": 92, "prev_close": 100},  # -8%
        {"symbol": "BBB", "open": 100, "high": 101, "low": 95, "close": 96, "prev_close": 100},  # open≠high
        {"symbol": "CCC", "open": 50, "high": 50, "low": 40, "close": 41, "prev_close": 50},  # -18%
    ]
    picked = strat.select_symbols(snaps)
    assert picked == ["CCC", "AAA"]


def test_gainer_breakout_sl_target():
    strat = Nifty500GainerOrbStrategy(
        params={
            "session_open": "09:15",
            "range_minutes": 5,
            "buffer_pct": 0.5,
            "risk_reward": 3,
            "entry_end": "15:00",
        }
    )
    strat.select_symbols(
        [{"symbol": "RELIANCE", "open": 100, "high": 102, "low": 100, "close": 101, "prev_close": 99}]
    )
    day = datetime(2026, 9, 8, 9, 15, tzinfo=IST)
    # build range 09:15
    b0 = Bar(timestamp=day, open=100, high=102, low=100, close=101, volume=1)
    hist: list[Bar] = []
    sig = strat.on_symbol_bar("RELIANCE", b0, hist)
    hist.append(b0)
    assert sig.action is SignalAction.HOLD
    # after range: break high
    b1 = Bar(timestamp=day + timedelta(minutes=6), open=102, high=103.5, low=102, close=103, volume=1)
    sig = strat.on_symbol_bar("RELIANCE", b1, hist)
    assert sig.action is SignalAction.BUY
    assert sig.meta["stop"] == 100 * (1 - 0.005)
    risk = 103 - sig.meta["stop"]
    assert abs(sig.meta["target"] - (103 + 3 * risk)) < 1e-6


def test_loser_breakout_short():
    strat = Nifty500LoserOrbStrategy(
        params={
            "session_open": "09:15",
            "range_minutes": 5,
            "buffer_pct": 0.5,
            "risk_reward": 3,
            "entry_end": "15:00",
        }
    )
    strat.select_symbols(
        [{"symbol": "TATASTEEL", "open": 100, "high": 100, "low": 97, "close": 98, "prev_close": 105}]
    )
    day = datetime(2026, 9, 8, 9, 15, tzinfo=IST)
    b0 = Bar(timestamp=day, open=100, high=100, low=97, close=98, volume=1)
    hist: list[Bar] = []
    strat.on_symbol_bar("TATASTEEL", b0, hist)
    hist.append(b0)
    b1 = Bar(timestamp=day + timedelta(minutes=6), open=97, high=97, low=95, close=96, volume=1)
    sig = strat.on_symbol_bar("TATASTEEL", b1, hist)
    assert sig.action is SignalAction.SELL
    assert abs(sig.meta["stop"] - 100 * 1.005) < 1e-6


def test_journal_persists_trade(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'paper_test.db'}")
    from algo.config import get_settings
    from algo.storage.db import reset_engine

    reset_engine()
    get_settings.cache_clear()  # type: ignore[attr-defined]
    sid = start_journal_session(mode="test")
    from datetime import timezone

    ts = datetime(2026, 9, 8, 4, 0, tzinfo=timezone.utc)
    tid = open_trade(
        session_id=sid,
        strategy_id="nifty500_gainer_orb",
        instance_id="x",
        symbol="RELIANCE",
        side="LONG",
        quantity=1,
        entry_price=100,
        stop_price=99,
        target_price=103,
        entry_at=ts,
        reason="test entry",
    )
    close_trade(
        trade_id=tid,
        session_id=sid,
        strategy_id="nifty500_gainer_orb",
        instance_id="x",
        symbol="RELIANCE",
        side="LONG",
        quantity=1,
        exit_price=103,
        exit_at=ts,
        realized_pnl=3.0,
        reason="target",
    )
    trades = list_trades(strategy_id="nifty500_gainer_orb")
    assert len(trades) >= 1
    assert trades[0]["status"] == "closed"
    assert trades[0]["realized_pnl"] == 3.0
    summary = report_summary()
    assert summary["closed"] >= 1


def test_cash_orb_registered_and_autosizes_buy():
    from datetime import timezone

    from algo.paper.basket import BasketRunner
    from algo.paper.equity_orb import Nifty500GainerOrbCashStrategy
    from algo.paper.models import Signal, SignalAction

    ids = {s["id"] for s in list_strategies()}
    assert "nifty500_gainer_orb_cash" in ids
    assert "nifty500_loser_orb_cash" in ids
    assert Nifty500GainerOrbCashStrategy.auto_size_cash is True

    strat = Nifty500GainerOrbCashStrategy(params={"qty_per_symbol": 10})
    strat.selected = ["HAL"]
    strat._legs["HAL"] = {
        "range_high": 4800,
        "range_low": 4700,
        "range_done": True,
        "in_trade": True,
        "entry": 4900,
        "stop": 4690,
        "target": 5500,
        "trades_today": 1,
        "side": "long",
    }
    runner = BasketRunner(
        instance_id="test:cash",
        strategy=strat,
        starting_cash=20_000.0,
        quantity=10,
    )
    runner.selected = ["HAL"]
    from algo.paper.broker import PaperBroker

    broker = PaperBroker(starting_cash=20_000.0)
    broker.bind(strat.id, "NSE:EQ:HAL", "HAL")
    runner.brokers["HAL"] = broker
    ts = datetime(2026, 9, 8, 10, 0, tzinfo=IST)
    signal = Signal(
        action=SignalAction.BUY,
        reason="break",
        meta={"structure": "long_orb", "fill_price": 4900.0, "stop": 4690, "target": 5500},
    )
    runner._apply("HAL", broker, signal, 4900.0, ts)
    # 20000 / 4900 ≈ 4 shares, not 10
    assert broker.position.quantity == 4
    assert strat._legs["HAL"]["in_trade"] is True


def test_cash_orb_rolls_back_when_cannot_afford_one():
    from algo.paper.basket import BasketRunner
    from algo.paper.broker import PaperBroker
    from algo.paper.equity_orb import Nifty500GainerOrbCashStrategy
    from algo.paper.models import Signal, SignalAction

    strat = Nifty500GainerOrbCashStrategy(params={"qty_per_symbol": 10})
    strat.selected = ["HAL"]
    strat._legs["HAL"] = {
        "range_high": 4800,
        "range_low": 4700,
        "range_done": True,
        "in_trade": True,
        "entry": 4900,
        "stop": 4690,
        "target": 5500,
        "trades_today": 1,
        "side": "long",
    }
    runner = BasketRunner(instance_id="test:cash2", strategy=strat, starting_cash=1000.0, quantity=10)
    runner.selected = ["HAL"]
    broker = PaperBroker(starting_cash=1000.0)
    broker.bind(strat.id, "NSE:EQ:HAL", "HAL")
    runner.brokers["HAL"] = broker
    ts = datetime(2026, 9, 8, 10, 0, tzinfo=IST)
    signal = Signal(
        action=SignalAction.BUY,
        reason="break",
        meta={"structure": "long_orb", "fill_price": 4900.0, "stop": 4690, "target": 5500},
    )
    runner._apply("HAL", broker, signal, 4900.0, ts)
    assert broker.position.quantity == 0
    assert strat._legs["HAL"]["in_trade"] is False
    assert strat._legs["HAL"]["trades_today"] == 0
