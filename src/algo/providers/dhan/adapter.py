from __future__ import annotations

from datetime import date
from typing import Any

from algo.config import Settings
from algo.domain.models import Candle, Instrument, ProviderMapping
from algo.domain.timeframes import DHAN_INTRADAY_INTERVAL, Timeframe
from algo.ingest.rate_limiter import RateLimiter
from algo.providers.base import HistoricalDataProvider
from algo.providers.dhan.client import DhanClient
from algo.providers.dhan.instruments import filter_universe, parse_instrument_master_csv
from algo.providers.dhan.parser import parse_columnar_candles


class DhanHistoricalDataProvider(HistoricalDataProvider):
    name = "dhan"

    def __init__(self, settings: Settings, client: DhanClient | None = None) -> None:
        self.settings = settings
        self.client = client or DhanClient(
            client_id=settings.dhan_client_id,
            access_token=settings.dhan_access_token,
            base_url=settings.dhan_base_url,
            timeout=settings.timeout_seconds,
            limiter=RateLimiter(settings.requests_per_second, settings.requests_per_day),
        )

    def get_profile(self) -> dict[str, Any]:
        return self.client.get_json("/profile")

    def get_instruments(self) -> list[tuple[Instrument, ProviderMapping]]:
        csv_text = self.client.get_text(self.settings.instrument_master_url)
        parsed = parse_instrument_master_csv(csv_text, provider=self.name)
        return filter_universe(parsed, index_symbols=self.settings.universe_indices)

    def get_historical_candles(
        self,
        mapping: ProviderMapping,
        timeframe: Timeframe,
        start: date,
        end: date,
        *,
        include_oi: bool = False,
    ) -> tuple[list[Candle], dict[str, Any]]:
        if timeframe.is_intraday:
            payload = self.client.post_json(
                "/charts/intraday",
                {
                    "securityId": str(mapping.provider_instrument_id),
                    "exchangeSegment": mapping.exchange_segment,
                    "instrument": mapping.provider_instrument,
                    "interval": DHAN_INTRADAY_INTERVAL[timeframe],
                    "oi": include_oi,
                    "fromDate": start.isoformat(),
                    "toDate": end.isoformat(),
                },
            )
        else:
            payload = self.client.post_json(
                "/charts/historical",
                {
                    "securityId": str(mapping.provider_instrument_id),
                    "exchangeSegment": mapping.exchange_segment,
                    "instrument": mapping.provider_instrument,
                    "expiryCode": 0,
                    "oi": include_oi,
                    "fromDate": start.isoformat(),
                    "toDate": end.isoformat(),
                },
            )
        if not isinstance(payload, dict):
            payload = {}
        candles = parse_columnar_candles(
            payload,
            instrument_id=mapping.instrument_id,
            timeframe=timeframe.value,
            source=self.name,
        )
        return candles, payload
