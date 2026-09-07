from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date
from typing import Any

from algo.domain.models import Candle, Instrument, ProviderMapping
from algo.domain.timeframes import Timeframe


class ProviderError(Exception):
    def __init__(self, message: str, *, code: str | None = None, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class HistoricalDataProvider(ABC):
    name: str

    @abstractmethod
    def get_instruments(self) -> list[tuple[Instrument, ProviderMapping]]:
        raise NotImplementedError

    @abstractmethod
    def get_historical_candles(
        self,
        mapping: ProviderMapping,
        timeframe: Timeframe,
        start: date,
        end: date,
        *,
        include_oi: bool = False,
    ) -> tuple[list[Candle], dict[str, Any]]:
        """Return canonical candles plus the raw provider payload."""
        raise NotImplementedError

    def get_expired_contracts(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("Expired-contract fetch is not implemented for this provider")
