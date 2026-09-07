from __future__ import annotations

import time
from collections import deque
from threading import Lock


class RateLimiter:
    """Sliding-window limiter. Default matches Dhan Data API: 5 requests/second."""

    def __init__(self, requests_per_second: float = 5.0, requests_per_day: int = 100_000) -> None:
        if requests_per_second <= 0:
            raise ValueError("requests_per_second must be positive")
        self.requests_per_second = requests_per_second
        self.requests_per_day = requests_per_day
        self._second_hits: deque[float] = deque()
        self._day_hits: deque[float] = deque()
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
            if len(self._second_hits) >= self.requests_per_second:
                return max(0.01, self._second_hits[0] + 1.0 - now)
            self._second_hits.append(now)
            self._day_hits.append(now)
            return 0.0
