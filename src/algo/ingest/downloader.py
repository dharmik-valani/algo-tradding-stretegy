from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from algo.config import Settings
from algo.domain.calendar import MarketCalendar
from algo.domain.models import QualityReport
from algo.domain.timeframes import Timeframe
from algo.domain.validation import split_valid
from algo.ingest.chunking import date_chunks
from algo.ingest.quality import build_quality_report
from algo.ingest.retry import retry_call
from algo.providers.base import HistoricalDataProvider
from algo.storage.db import session_scope
from algo.storage.raw import save_raw_json
from algo.storage.models import DownloadJobRow
from algo.storage.repositories import (
    candle_stats,
    create_job,
    get_instrument_by_symbol,
    get_mapping,
    insert_candles,
    load_candles,
    mapping_to_domain,
    save_quality,
    upsert_instruments,
)


@dataclass
class DownloadResult:
    instrument_id: str
    timeframe: str
    rows_inserted: int
    chunks: int
    quality: QualityReport


class HistoricalDownloader:
    def __init__(self, settings: Settings, provider: HistoricalDataProvider) -> None:
        self.settings = settings
        self.provider = provider
        self.calendar = MarketCalendar.from_yaml(settings.calendar_path)

    def sync_instruments(self) -> int:
        items = self.provider.get_instruments()
        with session_scope(self.settings) as session:
            count = upsert_instruments(session, items)
        if self.settings.save_raw:
            save_raw_json(
                self.settings.raw_dir,
                provider=self.provider.name,
                kind="instruments",
                instrument_id="universe",
                timeframe="na",
                start=date.today(),
                end=date.today(),
                payload={"count": count, "ids": [inst.id for inst, _ in items]},
            )
        return count

    def download(
        self,
        *,
        symbol: str,
        timeframe: Timeframe,
        start: date,
        end: date,
        expiry: date | None = None,
        strike: float | None = None,
        option_type: str | None = None,
    ) -> DownloadResult:
        with session_scope(self.settings) as session:
            instrument = get_instrument_by_symbol(
                session, symbol, expiry=expiry, strike=strike, option_type=option_type
            )
            if instrument is None:
                raise ValueError(
                    f"Unknown instrument {symbol}. Run `algo instruments sync` first."
                )
            mapping_row = get_mapping(session, instrument.id, self.provider.name)
            if mapping_row is None:
                raise ValueError(f"No {self.provider.name} mapping for {instrument.id}")
            mapping = mapping_to_domain(mapping_row)
            job = create_job(
                session,
                instrument_id=instrument.id,
                timeframe=timeframe.value,
                provider=self.provider.name,
                start=start,
                end=end,
            )
            instrument_id = instrument.id
            job_id = job.id

        chunks = date_chunks(
            start,
            end,
            timeframe,
            intraday_days=self.settings.intraday_chunk_days,
            daily_days=self.settings.daily_chunk_days,
        )
        include_oi = mapping.provider_instrument not in {"INDEX", "EQUITY"}
        inserted = 0
        try:
            for chunk in chunks:
                candles, payload = retry_call(
                    lambda c=chunk: self.provider.get_historical_candles(
                        mapping, timeframe, c.start, c.end, include_oi=include_oi
                    ),
                    max_retries=self.settings.max_retries,
                )
                if self.settings.save_raw:
                    save_raw_json(
                        self.settings.raw_dir,
                        provider=self.provider.name,
                        kind="candles",
                        instrument_id=instrument_id,
                        timeframe=timeframe.value,
                        start=chunk.start,
                        end=chunk.end,
                        payload=payload,
                    )
                valid, _invalid = split_valid(candles)
                with session_scope(self.settings) as session:
                    added = insert_candles(session, valid)
                    inserted += added
                    job_row = session.get(DownloadJobRow, job_id)
                    if job_row is not None:
                        job_row.last_chunk_end = chunk.end
                        job_row.rows_inserted = inserted
                        job_row.status = "running"
            with session_scope(self.settings) as session:
                stored = load_candles(session, instrument_id, timeframe.value)
                quality = build_quality_report(
                    stored,
                    instrument_id=instrument_id,
                    timeframe=timeframe,
                    start=start,
                    end=end,
                    calendar=self.calendar,
                    provider=self.provider.name,
                )
                save_quality(session, quality)
                job_row = session.get(DownloadJobRow, job_id)
                if job_row is not None:
                    job_row.status = "completed"
                    job_row.rows_inserted = inserted
                    job_row.error = None
        except Exception as exc:
            with session_scope(self.settings) as session:
                job_row = session.get(DownloadJobRow, job_id)
                if job_row is not None:
                    job_row.status = "failed"
                    job_row.error = str(exc)
                    job_row.rows_inserted = inserted
            raise

        return DownloadResult(
            instrument_id=instrument_id,
            timeframe=timeframe.value,
            rows_inserted=inserted,
            chunks=len(chunks),
            quality=quality,
        )

    def status(self, symbol: str, timeframe: Timeframe) -> dict:
        with session_scope(self.settings) as session:
            instrument = get_instrument_by_symbol(session, symbol)
            if instrument is None:
                raise ValueError(f"Unknown instrument {symbol}")
            stats = candle_stats(session, instrument.id, timeframe.value)
            stored = load_candles(session, instrument.id, timeframe.value)
        first = stats["first"]
        last = stats["last"]
        start = first.date() if first is not None else date.today()
        end = last.date() if last is not None else date.today()
        quality = build_quality_report(
            stored,
            instrument_id=instrument.id,
            timeframe=timeframe,
            start=start,
            end=end,
            calendar=self.calendar,
            provider=self.provider.name,
        )
        return {
            "instrument_id": instrument.id,
            "symbol": symbol,
            "timeframe": timeframe.value,
            "count": stats["count"],
            "first": first,
            "last": last,
            "quality": quality,
        }
