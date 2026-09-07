from __future__ import annotations

import random
import time
from collections.abc import Callable
from typing import TypeVar

from algo.providers.base import ProviderError

T = TypeVar("T")

RETRYABLE_HTTP = {429, 500, 502, 503, 504}


def retry_call(
    fn: Callable[[], T],
    *,
    max_retries: int = 5,
    backoff_seconds: float = 1.0,
    backoff_max: float = 30.0,
    sleeper: Callable[[float], None] = time.sleep,
) -> T:
    last: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            return fn()
        except ProviderError as exc:
            last = exc
            if not exc.retryable or attempt == max_retries:
                raise
            delay = min(backoff_max, backoff_seconds * (2**attempt))
            delay += random.uniform(0, delay * 0.1)
            sleeper(delay)
        except Exception:
            raise
    assert last is not None
    raise last
