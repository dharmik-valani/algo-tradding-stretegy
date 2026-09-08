from __future__ import annotations

from typing import Any, Literal

import httpx

from algo.ingest.rate_limiter import RateLimiter
from algo.providers.base import ProviderError
from algo.providers.dhan.parser import parse_error_payload

RETRYABLE_HTTP = {429, 500, 502, 503, 504}

# DhanHQ: /marketfeed/* are Quote APIs (1 rps). /charts/* are Data APIs (5 rps).
ApiCategory = Literal["quote", "data", "non_trading"]


def _category_for_path(path: str) -> ApiCategory:
    p = path.split("?", 1)[0]
    if p.startswith("/marketfeed/"):
        return "quote"
    if p.startswith("/charts/"):
        return "data"
    return "non_trading"


class DhanClient:
    """HTTP client with per-category DhanHQ rate limits."""

    def __init__(
        self,
        *,
        client_id: str,
        access_token: str,
        base_url: str,
        timeout: float,
        limiter: RateLimiter | None = None,
        quote_limiter: RateLimiter | None = None,
        data_limiter: RateLimiter | None = None,
        non_trading_limiter: RateLimiter | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.client_id = client_id
        self.access_token = access_token
        self.base_url = base_url.rstrip("/")
        # Backward compat: single limiter becomes data limiter.
        self.data_limiter = data_limiter or limiter or RateLimiter(4.0, 100_000)
        # Official Quote APIs = 1/s — use 0.95 spacing to stay under the ceiling.
        self.quote_limiter = quote_limiter or RateLimiter(0.95, 100_000)
        self.non_trading_limiter = non_trading_limiter or RateLimiter(15.0, 100_000)
        # Alias used by older call sites
        self.limiter = self.data_limiter
        self._http = httpx.Client(timeout=timeout, transport=transport)

    def close(self) -> None:
        self._http.close()

    def headers(self) -> dict[str, str]:
        return {
            "access-token": self.access_token,
            "client-id": self.client_id,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def _limiter_for(self, category: ApiCategory) -> RateLimiter:
        if category == "quote":
            return self.quote_limiter
        if category == "non_trading":
            return self.non_trading_limiter
        return self.data_limiter

    def get_json(self, path: str, *, category: ApiCategory | None = None) -> Any:
        return self._request("GET", path, category=category)

    def post_json(
        self, path: str, body: dict[str, Any], *, category: ApiCategory | None = None
    ) -> Any:
        return self._request("POST", path, json=body, category=category)

    def get_text(self, url: str) -> str:
        self.non_trading_limiter.acquire()
        try:
            response = self._http.get(url, timeout=60.0)
        except httpx.HTTPError as exc:
            raise ProviderError(f"network error: {exc}", retryable=True) from exc
        if response.status_code >= 400:
            raise ProviderError(
                f"HTTP {response.status_code} fetching {url}",
                retryable=response.status_code in RETRYABLE_HTTP,
            )
        return response.text

    def _request(
        self,
        method: str,
        path: str,
        json: dict[str, Any] | None = None,
        *,
        category: ApiCategory | None = None,
    ) -> Any:
        cat = category or _category_for_path(path)
        self._limiter_for(cat).acquire()
        url = path if path.startswith("http") else f"{self.base_url}{path}"
        try:
            response = self._http.request(method, url, headers=self.headers(), json=json)
        except httpx.HTTPError as exc:
            raise ProviderError(f"network error: {exc}", retryable=True) from exc
        payload: Any
        try:
            payload = response.json() if response.content else None
        except ValueError:
            payload = {"message": response.text}
        if response.status_code >= 400:
            message, code, retryable = parse_error_payload(payload, response.status_code)
            raise ProviderError(
                message, code=code, retryable=retryable or response.status_code in RETRYABLE_HTTP
            )
        if isinstance(payload, dict) and str(payload.get("status", "")).lower() in {
            "failure",
            "failed",
        }:
            message, code, retryable = parse_error_payload(payload, response.status_code)
            raise ProviderError(message, code=code, retryable=retryable)
        return payload
