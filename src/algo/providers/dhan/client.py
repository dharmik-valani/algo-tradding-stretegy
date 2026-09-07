from __future__ import annotations

from typing import Any

import httpx

from algo.ingest.rate_limiter import RateLimiter
from algo.providers.base import ProviderError
from algo.providers.dhan.parser import parse_error_payload

RETRYABLE_HTTP = {429, 500, 502, 503, 504}


class DhanClient:
    def __init__(
        self,
        *,
        client_id: str,
        access_token: str,
        base_url: str,
        timeout: float,
        limiter: RateLimiter,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.client_id = client_id
        self.access_token = access_token
        self.base_url = base_url.rstrip("/")
        self.limiter = limiter
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

    def get_json(self, path: str) -> Any:
        return self._request("GET", path)

    def post_json(self, path: str, body: dict[str, Any]) -> Any:
        return self._request("POST", path, json=body)

    def get_text(self, url: str) -> str:
        self.limiter.acquire()
        try:
            response = self._http.get(url, timeout=60.0)
        except httpx.HTTPError as exc:
            raise ProviderError(f"network error: {exc}", retryable=True) from exc
        if response.status_code >= 400:
            raise ProviderError(f"HTTP {response.status_code} fetching {url}", retryable=response.status_code in RETRYABLE_HTTP)
        return response.text

    def _request(self, method: str, path: str, json: dict[str, Any] | None = None) -> Any:
        self.limiter.acquire()
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
            raise ProviderError(message, code=code, retryable=retryable or response.status_code in RETRYABLE_HTTP)
        if isinstance(payload, dict) and str(payload.get("status", "")).lower() in {"failure", "failed"}:
            message, code, retryable = parse_error_payload(payload, response.status_code)
            raise ProviderError(message, code=code, retryable=retryable)
        return payload
