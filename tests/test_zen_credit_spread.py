from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from algo.paper.models import Bar, SignalAction
from algo.paper.registry import create_strategy, list_strategies
from algo.paper.zen_credit import ZenCreditSpreadOvernightStrategy, _mark_to_close, _percentile_rank

IST = ZoneInfo("Asia/Kolkata")


def _bars(n: int, start: datetime, base: float = 24000.0, path: list[float] | None = None) -> list[Bar]:
    out: list[Bar] = []
    px = base
    for i in range(n):
        if path is not None and i < len(path):
            px = path[i]
        else:
            px = base + (i % 3) * 2
        ts = start + timedelta(minutes=5 * i)
        out.append(
            Bar(
                timestamp=ts,
                open=px,
                high=px + 5,
                low=px - 5,
                close=px,
                volume=100_000 + i * 10,
            )
        )
    return out


def test_zen_registered():
    ids = {s["id"] for s in list_strategies()}
    assert "zen_credit_spread" in ids
    s = create_strategy("zen_credit_spread")
    assert s.default_params()["entry_start"] == "10:15"
    assert s.default_params()["spread_width"] == 400


def test_percentile_rank_extremes():
    assert _percentile_rank([0.1, 0.2, 0.3], 0.3) == 1.0
    assert _percentile_rank([0.1, 0.2, 0.3], 0.05) < 0.5


def test_zen_bearish_call_credit():
    strat = ZenCreditSpreadOvernightStrategy(
        params={
            "alpha_lookback_min": 50,
            "alpha2_lookback_min": 50,
            "entry_start": "10:15",
            "entry_end": "14:15",
            "bar_minutes": 5,
            "spread_width": 400,
            "min_impulse_pts": 25,
        }
    )
    day = datetime(2026, 9, 7, 9, 15, tzinfo=IST)
    # Quiet tape, then sustained selloff inside window
    path = [24500.0] * 40
    path += [24500 - i * 80 for i in range(1, 12)]
    bars = _bars(len(path), day, path=path)
    history: list[Bar] = []
    entry = None
    for bar in bars:
        sig = strat.on_bar(bar, history)
        if sig.action is SignalAction.SELL and sig.meta.get("structure") == "call_credit":
            entry = sig
        history.append(bar)
    assert entry is not None, "expected bearish credit call spread entry"
    assert entry.meta.get("short_leg", "").startswith("SELL CE")


def test_zen_bullish_enters_put_credit():
    """Large up-move → both alphas high → credit put spread (SELL)."""
    strat = ZenCreditSpreadOvernightStrategy(
        params={
            "alpha_lookback_min": 50,
            "alpha2_lookback_min": 50,
            "entry_start": "10:15",
            "entry_end": "14:15",
            "bar_minutes": 5,
            "spread_width": 400,
            "sl_margin_pct": 0.35,
            "alpha_long": 0.8,
            "alpha_short": 0.2,
            "min_impulse_pts": 25,
        }
    )
    day = datetime(2026, 9, 7, 9, 15, tzinfo=IST)
    path = [24000.0] * 40
    path += [24000 + i * 80 for i in range(1, 12)]
    bars = _bars(len(path), day, path=path)
    history: list[Bar] = []
    entry = None
    for bar in bars:
        sig = strat.on_bar(bar, history)
        if sig.action is SignalAction.SELL and sig.meta.get("structure") == "put_credit":
            entry = sig
        history.append(bar)
    assert entry is not None, "expected bullish credit put spread entry"
    assert entry.meta.get("short_leg", "").startswith("SELL PE")
    assert "BUY PE" in str(entry.meta.get("long_leg", ""))
    assert entry.meta.get("fill_price", 0) > 0
    assert strat._structure == "put_credit"


def test_zen_outside_window_holds():
    strat = ZenCreditSpreadOvernightStrategy(
        params={"alpha_lookback_min": 50, "alpha2_lookback_min": 50, "bar_minutes": 5}
    )
    day = datetime(2026, 9, 7, 9, 15, tzinfo=IST)
    bars = _bars(10, day)
    history: list[Bar] = []
    last = None
    for bar in bars:
        last = strat.on_bar(bar, history)
        history.append(bar)
    assert last is not None
    assert last.action is SignalAction.HOLD
    assert strat._structure is None


def test_zen_margin_stop():
    strat = ZenCreditSpreadOvernightStrategy(
        params={
            "alpha_lookback_min": 50,
            "alpha2_lookback_min": 50,
            "bar_minutes": 5,
            "sl_margin_pct": 0.05,
            "spread_width": 400,
        }
    )
    strat._in_trade = True
    strat._structure = "put_credit"
    strat._entry_credit = 40.0
    strat._entry_day = datetime(2026, 9, 7, tzinfo=IST).date()
    strat._short_strike = 24000
    strat._long_strike = 23600
    strat._max_loss_pts = 100.0
    mark = _mark_to_close(22000, structure="put_credit", short_strike=24000, long_strike=23600, iv_proxy=0.2)
    assert mark - 40.0 >= 5.0
    bar = Bar(
        timestamp=datetime(2026, 9, 7, 12, 0, tzinfo=IST),
        open=22000,
        high=22000,
        low=21800,
        close=22000,
        volume=1,
    )
    history = _bars(20, datetime(2026, 9, 7, 10, 0, tzinfo=IST), base=24000)
    sig = strat.on_bar(bar, history)
    assert sig.action is SignalAction.FLAT
    assert "margin SL" in sig.reason
