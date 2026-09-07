from __future__ import annotations

from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from algo.domain.sessions import MARKET_TZ, MarketSession
from algo.domain.timeframes import Timeframe

IST = ZoneInfo("Asia/Kolkata")


class MarketCalendar:
    def __init__(
        self,
        holidays: set[date],
        session: MarketSession,
        timezone: ZoneInfo = MARKET_TZ,
        special_sessions: dict[date, MarketSession] | None = None,
    ) -> None:
        self.holidays = holidays
        self.session = session
        self.timezone = timezone
        self.special_sessions = special_sessions or {}

    @classmethod
    def from_yaml(cls, path: Path) -> MarketCalendar:
        with path.open() as fh:
            payload = yaml.safe_load(fh) or {}
        holidays = {_as_date(row["date"]) for row in payload.get("holidays", [])}
        open_h, open_m = (payload.get("regular_session") or {}).get("open", "09:15").split(":")
        close_h, close_m = (payload.get("regular_session") or {}).get("close", "15:30").split(":")
        session = MarketSession(open=time(int(open_h), int(open_m)), close=time(int(close_h), int(close_m)))
        special: dict[date, MarketSession] = {}
        for row in payload.get("special_sessions") or []:
            if not row.get("open") or not row.get("close"):
                continue
            oh, om = str(row["open"]).split(":")
            ch, cm = str(row["close"]).split(":")
            special[_as_date(row["date"])] = MarketSession(
                open=time(int(oh), int(om)),
                close=time(int(ch), int(cm)),
            )
        return cls(holidays=holidays, session=session, special_sessions=special)

    def is_weekend(self, day: date) -> bool:
        return day.weekday() >= 5

    def is_holiday(self, day: date) -> bool:
        return day in self.holidays

    def is_trading_day(self, day: date) -> bool:
        if day in self.special_sessions:
            return True
        if self.is_weekend(day) or self.is_holiday(day):
            return False
        return True

    def session_for(self, day: date) -> MarketSession | None:
        if not self.is_trading_day(day):
            return None
        return self.special_sessions.get(day, self.session)

    def expected_timestamps(self, start: date, end: date, timeframe: Timeframe) -> list[datetime]:
        """Bar-start timestamps in IST, returned timezone-aware."""
        out: list[datetime] = []
        day = start
        while day <= end:
            session = self.session_for(day)
            if session is None:
                day += timedelta(days=1)
                continue
            if timeframe is Timeframe.D1:
                out.append(datetime.combine(day, session.open, tzinfo=self.timezone))
                day += timedelta(days=1)
                continue
            cursor = datetime.combine(day, session.open, tzinfo=self.timezone)
            close = datetime.combine(day, session.close, tzinfo=self.timezone)
            step = timedelta(minutes=timeframe.minutes)
            while cursor < close:
                out.append(cursor)
                cursor += step
            day += timedelta(days=1)
        return out


def _as_date(value: date | str) -> date:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    return date.fromisoformat(str(value)[:10])
