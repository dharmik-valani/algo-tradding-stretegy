from __future__ import annotations

from algo.domain.models import Candle


def validate_candle(candle: Candle) -> list[str]:
    errors: list[str] = []
    if candle.open <= 0 or candle.high <= 0 or candle.low <= 0 or candle.close <= 0:
        errors.append("non-positive OHLC")
    if candle.high < max(candle.open, candle.close):
        errors.append("high < max(open, close)")
    if candle.low > min(candle.open, candle.close):
        errors.append("low > min(open, close)")
    if candle.volume < 0:
        errors.append("negative volume")
    if candle.open_interest is not None and candle.open_interest < 0:
        errors.append("negative open_interest")
    if candle.timestamp.tzinfo is None:
        errors.append("naive timestamp")
    return errors


def split_valid(candles: list[Candle]) -> tuple[list[Candle], list[tuple[Candle, list[str]]]]:
    good: list[Candle] = []
    bad: list[tuple[Candle, list[str]]] = []
    for candle in candles:
        issues = validate_candle(candle)
        if issues:
            bad.append((candle, issues))
        else:
            good.append(candle)
    return good, bad
