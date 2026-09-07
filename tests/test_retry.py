from datetime import date

from algo.ingest.retry import retry_call
from algo.providers.base import ProviderError


def test_retries_then_succeeds():
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise ProviderError("rate limit", code="DH-904", retryable=True)
        return "ok"

    assert retry_call(flaky, max_retries=5, backoff_seconds=0.0, sleeper=lambda _: None) == "ok"
    assert calls["n"] == 3


def test_non_retryable_raises_immediately():
    def boom():
        raise ProviderError("no plan", code="DH-902", retryable=False)

    try:
        retry_call(boom, max_retries=5, sleeper=lambda _: None)
        assert False, "should have raised"
    except ProviderError as exc:
        assert exc.code == "DH-902"
