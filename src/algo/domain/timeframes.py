from __future__ import annotations

from enum import Enum


class Timeframe(str, Enum):
    M1 = "1m"
    M5 = "5m"
    M15 = "15m"
    M25 = "25m"
    H1 = "1h"
    D1 = "1D"

    @property
    def is_intraday(self) -> bool:
        return self is not Timeframe.D1

    @property
    def minutes(self) -> int:
        return {
            Timeframe.M1: 1,
            Timeframe.M5: 5,
            Timeframe.M15: 15,
            Timeframe.M25: 25,
            Timeframe.H1: 60,
            Timeframe.D1: 24 * 60,
        }[self]


DHAN_INTRADAY_INTERVAL = {
    Timeframe.M1: "1",
    Timeframe.M5: "5",
    Timeframe.M15: "15",
    Timeframe.M25: "25",
    Timeframe.H1: "60",
}

# Dhan has no native 30-minute interval.
UNSUPPORTED_NATIVE = {"30m"}


def parse_timeframe(value: str) -> Timeframe:
    raw = value.strip()
    if raw in UNSUPPORTED_NATIVE:
        raise ValueError(
            "30m is not a native Dhan interval. Download 5m and resample later."
        )
    aliases = {
        "1": Timeframe.M1,
        "1m": Timeframe.M1,
        "5": Timeframe.M5,
        "5m": Timeframe.M5,
        "15": Timeframe.M15,
        "15m": Timeframe.M15,
        "25": Timeframe.M25,
        "25m": Timeframe.M25,
        "60": Timeframe.H1,
        "1h": Timeframe.H1,
        "60m": Timeframe.H1,
        "1d": Timeframe.D1,
        "1D": Timeframe.D1,
        "d": Timeframe.D1,
        "day": Timeframe.D1,
        "daily": Timeframe.D1,
    }
    if raw not in aliases:
        raise ValueError(f"Unknown timeframe: {value}")
    return aliases[raw]
