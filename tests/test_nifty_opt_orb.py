from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from algo.paper.models import Bar, SignalAction
from algo.paper.strategy import NiftyOptionOrbStrategy

IST = ZoneInfo("Asia/Kolkata")


def _bars_day(premiums: list[float], start: datetime) -> list[Bar]:
    out = []
    for i, px in enumerate(premiums):
        ts = start + timedelta(minutes=i)
        out.append(Bar(timestamp=ts, open=px, high=px + 0.5, low=px - 0.5, close=px, volume=25000))
    return out


def test_orb_buys_after_range_break_and_stops():
    strat = NiftyOptionOrbStrategy(
        params={
            "session_open": "09:15",
            "range_minutes": 15,
            "hold_minutes": 15,
            "stop_points": 17,
            "target_points": 34,
            "option_type": "CE",
            "strike_mode": "ATM",
        }
    )
    day = datetime(2026, 9, 7, 9, 15, tzinfo=IST)
    # 15 mins building range around 100
    premiums = [100 + (i % 3) for i in range(15)]  # high ~102
    # breakout then hit stop (entry ~103, SL 86)
    premiums += [103, 104, 105, 85, 80]
    history: list[Bar] = []
    actions = []
    for bar in _bars_day(premiums, day):
        sig = strat.on_bar(bar, history)
        actions.append(sig.action)
        history.append(bar)
    assert SignalAction.BUY in actions
    assert SignalAction.FLAT in actions
