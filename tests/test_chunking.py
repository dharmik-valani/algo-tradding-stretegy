from datetime import date

from algo.domain.timeframes import Timeframe
from algo.ingest.chunking import date_chunks


def test_intraday_chunks_are_90_days():
    chunks = date_chunks(date(2025, 1, 1), date(2025, 7, 1), Timeframe.M5, intraday_days=90)
    assert chunks[0].start == date(2025, 1, 1)
    assert (chunks[0].end - chunks[0].start).days == 90
    assert chunks[-1].end == date(2025, 7, 1)
    assert all(c.end > c.start for c in chunks)


def test_empty_when_end_not_after_start():
    assert date_chunks(date(2025, 1, 1), date(2025, 1, 1), Timeframe.D1) == []
