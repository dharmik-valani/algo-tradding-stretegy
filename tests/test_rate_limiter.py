from algo.ingest.rate_limiter import RateLimiter


def test_allows_configured_rate_then_waits():
    limiter = RateLimiter(requests_per_second=2, requests_per_day=100)
    assert limiter._wait_seconds(1000.0) == 0.0
    assert limiter._wait_seconds(1000.0) == 0.0
    wait = limiter._wait_seconds(1000.0)
    assert wait > 0
    assert limiter._wait_seconds(1001.1) == 0.0
