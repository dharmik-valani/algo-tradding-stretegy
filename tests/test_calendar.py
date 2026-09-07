from datetime import date
from pathlib import Path

from algo.domain.calendar import MarketCalendar
from algo.domain.timeframes import Timeframe

CAL = Path(__file__).resolve().parents[1] / "data" / "calendars" / "nse.yaml"


def test_republic_day_2026_is_holiday():
    calendar = MarketCalendar.from_yaml(CAL)
    assert calendar.is_holiday(date(2026, 1, 26))
    assert not calendar.is_trading_day(date(2026, 1, 26))
    assert calendar.is_trading_day(date(2026, 1, 27))


def test_weekend_not_trading_day():
    calendar = MarketCalendar.from_yaml(CAL)
    assert not calendar.is_trading_day(date(2026, 1, 24))  # Saturday


def test_expected_5m_bars_on_trading_day():
    calendar = MarketCalendar.from_yaml(CAL)
    bars = calendar.expected_timestamps(date(2026, 1, 27), date(2026, 1, 27), Timeframe.M5)
    # 09:15 → 15:30 exclusive of close = 375 minutes / 5 = 75
    assert len(bars) == 75
    assert bars[0].hour == 9 and bars[0].minute == 15
    assert bars[-1].hour == 15 and bars[-1].minute == 25
