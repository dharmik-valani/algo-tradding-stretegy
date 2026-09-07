from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from zoneinfo import ZoneInfo

MARKET_TZ = ZoneInfo("Asia/Kolkata")
UTC = ZoneInfo("UTC")


@dataclass(frozen=True)
class MarketSession:
    open: time
    close: time
    timezone: ZoneInfo = MARKET_TZ

    def contains(self, ts: datetime) -> bool:
        local = ts.astimezone(self.timezone)
        clock = local.timetz().replace(tzinfo=None)
        return self.open <= clock <= self.close


def default_nse_session() -> MarketSession:
    return MarketSession(open=time(9, 15), close=time(15, 30))
