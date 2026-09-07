from datetime import datetime, timezone

from algo.paper.models import Bar, SignalAction
from algo.paper.strategy import EmaCrossStrategy


def test_ema_cross_buys_on_golden_cross():
    strat = EmaCrossStrategy(params={"fast": 3, "slow": 5})
    # declining then rising series to force cross
    closes = [10, 9, 8, 7, 7, 8, 9, 11, 13, 15, 17]
    history: list[Bar] = []
    last_signal = None
    for i, close in enumerate(closes):
        bar = Bar(
            timestamp=datetime(2026, 1, 2, 4, i, tzinfo=timezone.utc),
            open=close,
            high=close,
            low=close,
            close=close,
        )
        last_signal = strat.on_bar(bar, history)
        history.append(bar)
    assert last_signal is not None
    actions = []
    strat = EmaCrossStrategy(params={"fast": 3, "slow": 5})
    history = []
    for i, close in enumerate(closes):
        bar = Bar(
            timestamp=datetime(2026, 1, 2, 4, i, tzinfo=timezone.utc),
            open=close,
            high=close,
            low=close,
            close=close,
        )
        sig = strat.on_bar(bar, history)
        actions.append(sig.action)
        history.append(bar)
    assert SignalAction.BUY in actions
