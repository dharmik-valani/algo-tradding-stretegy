from __future__ import annotations

import time
from collections import deque
from threading import Lock


class RateLimiter:
    """Sliding-window limiter aligned with DhanHQ category limits.

    Docs (support + DhanHQ skills):
      - Quote APIs: 1 req/sec (marketfeed ltp/ohlc/quote)
      - Data APIs: 5 req/sec, 100_000/day (charts/historical)
      - Order APIs: 10/sec
      - Non-Trading: 20/sec
    """

    def __init__(self, requests_per_second: float = 5.0, requests_per_day: int = 100_000) -> None:
        if requests_per_second <= 0:
            raise ValueError("requests_per_second must be positive")
        self.requests_per_second = float(requests_per_second)
        self.requests_per_day = int(requests_per_day)
        # Burst in a 1s window. Sub-1 rps (Quote) → burst 1 + min spacing.
        if self.requests_per_second >= 1.0:
            self._burst = max(1, int(self.requests_per_second))
            self._min_interval = 0.0
        else:
            self._burst = 1
            self._min_interval = 1.0 / self.requests_per_second
        self._second_hits: deque[float] = deque()
        self._day_hits: deque[float] = deque()
        self._last_hit = 0.0
        self._lock = Lock()

    def acquire(self, now: float | None = None) -> None:
        while True:
            wait = self._wait_seconds(now or time.monotonic())
            if wait <= 0:
                return
            time.sleep(wait)

    def _wait_seconds(self, now: float) -> float:
        with self._lock:
            second_cut = now - 1.0
            day_cut = now - 86400.0
            while self._second_hits and self._second_hits[0] <= second_cut:
                self._second_hits.popleft()
            while self._day_hits and self._day_hits[0] <= day_cut:
                self._day_hits.popleft()
            if len(self._day_hits) >= self.requests_per_day:
                return max(0.01, self._day_hits[0] + 86400.0 - now)
            # Enforce min spacing (critical for Quote APIs = 1/s).
            since_last = now - self._last_hit
            if self._last_hit > 0 and since_last < self._min_interval:
                return max(0.01, self._min_interval - since_last)
            if len(self._second_hits) >= self._burst:
                return max(0.01, self._second_hits[0] + 1.0 - now)
            self._second_hits.append(now)
            self._day_hits.append(now)
            self._last_hit = now
            return 0.0
