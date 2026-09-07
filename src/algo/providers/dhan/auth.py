"""Dhan access-token lifecycle for paper / data APIs.

Uses official ``dhanhq.DhanLogin`` (https://github.com/dhan-oss/DhanHQ-py).

- RenewToken: only for **SELF** tokens from web.dhan.co (not Developer Portal PARTNER)
- Generate (PIN+TOTP): optional; not used unless configured
- Partner portal tokens: paste daily; API renew is rejected by Dhan (DH-905)
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from algo.config import Settings, get_settings
from algo.providers.base import ProviderError
from algo.providers.dhan.token_store import (
    jwt_consumer_type,
    jwt_expiry,
    load_token_file,
    resolve_access_token,
    save_token,
    token_status,
)

logger = logging.getLogger(__name__)

# Renew when fewer than this many seconds remain (~6h buffer).
RENEW_BEFORE_SEC = 6 * 60 * 60
# Background rotator cadence.
ROTATOR_INTERVAL_SEC = 15 * 60


def _client_id(settings: Settings) -> str:
    data = load_token_file()
    return str(data.get("clientId") or settings.dhan_client_id or "").strip()


def current_token(settings: Settings) -> str:
    return resolve_access_token(settings.dhan_access_token)


def is_renewable_token(token: str) -> bool:
    """Dhan RenewToken only works for web SELF tokens — not PARTNER portal JWTs."""
    consumer = jwt_consumer_type(token)
    if consumer == "PARTNER":
        return False
    stored = load_token_file()
    if stored.get("renewable") is False:
        return False
    return consumer in {"", "SELF"}


def _login(client_id: str):
    from dhanhq import DhanLogin

    return DhanLogin(client_id)


def fetch_profile(*, client_id: str, access_token: str, timeout: float = 20.0) -> dict[str, Any]:
    del timeout  # DhanLogin uses requests defaults
    try:
        payload = _login(client_id).user_profile(access_token)
    except Exception as exc:
        raise ProviderError(str(exc), code="DH-901", retryable=False) from exc
    if not isinstance(payload, dict):
        raise ProviderError(f"Unexpected profile payload: {payload}", code="PROFILE")
    if payload.get("errorCode") or str(payload.get("status", "")).lower() in {"failure", "failed"}:
        raise ProviderError(
            str(payload.get("errorMessage") or payload.get("message") or payload),
            code=str(payload.get("errorCode") or "PROFILE"),
            retryable=False,
        )
    return payload


def renew_access_token(*, client_id: str, access_token: str, timeout: float = 20.0) -> dict[str, Any]:
    """Renew an *active* web SELF token via DhanLogin.renew_token.

    Docs: https://docs.dhanhq.co/api/v2/authentication/renew-token
    """
    del timeout
    if not is_renewable_token(access_token):
        raise ProviderError(
            "RenewToken not allowed for PARTNER / developer-portal tokens — "
            "paste a fresh token daily, or use a SELF token from web.dhan.co",
            code="DH-905",
            retryable=False,
        )
    try:
        payload = _login(client_id).renew_token(access_token)
    except Exception as exc:
        raise ProviderError(str(exc), code="RENEW", retryable=False) from exc
    if not isinstance(payload, dict) or not payload.get("accessToken"):
        raise ProviderError(f"Unexpected RenewToken payload: {payload}", code="RENEW")
    return payload


def regenerate_via_totp(settings: Settings) -> dict[str, Any]:
    """Mint a new token with PIN + TOTP (optional; not required for portal paste flow)."""
    client_id = _client_id(settings)
    pin = (getattr(settings, "dhan_pin", None) or "").strip()
    secret = (getattr(settings, "dhan_totp_secret", None) or "").strip()
    if not client_id or not pin or not secret:
        raise ProviderError(
            "TOTP generate skipped (not configured)",
            code="NO_TOTP",
        )
    try:
        import pyotp
    except ImportError as exc:
        raise ProviderError("pyotp required for TOTP regen — pip install pyotp", code="NO_PYOTP") from exc
    totp = pyotp.TOTP(secret).now()
    try:
        payload = _login(client_id).generate_token(pin, totp)
    except Exception as exc:
        raise ProviderError(str(exc), code="TOTP", retryable=False) from exc
    if not isinstance(payload, dict) or not payload.get("accessToken"):
        raise ProviderError(f"Unexpected generate_token payload: {payload}", code="TOTP")
    return payload


def _seconds_left(token: str) -> float | None:
    exp = jwt_expiry(token)
    data = load_token_file()
    if data.get("expiryEpoch") and exp is None:
        try:
            exp = datetime.fromtimestamp(int(data["expiryEpoch"]), tz=timezone.utc)
        except Exception:
            exp = None
    if exp is None:
        return None
    return (exp - datetime.now(tz=timezone.utc)).total_seconds()


def _persist(settings: Settings, payload: dict[str, Any], *, source: str) -> str:
    client_id = _client_id(settings)
    new_tok = str(payload.get("accessToken") or "").strip()
    if not new_tok:
        raise ProviderError("empty accessToken in auth payload", code="AUTH")
    save_token(
        access_token=new_tok,
        client_id=str(payload.get("dhanClientId") or client_id),
        expiry_time=str(payload.get("expiryTime") or "") or None,
        source=source,
    )
    settings.dhan_access_token = new_tok
    try:
        get_settings.cache_clear()
    except Exception:
        pass
    return new_tok


def ensure_fresh_token(settings: Settings, *, force_renew: bool = False) -> str:
    """Return a usable access token; renew SELF tokens when near expiry."""
    client_id = _client_id(settings)
    token = current_token(settings)
    if not client_id or not token:
        raise ProviderError(
            "DHAN_CLIENT_ID / access token missing — set in Settings or .env",
            code="NO_TOKEN",
        )

    seconds_left = _seconds_left(token)

    if seconds_left is not None and seconds_left <= 0:
        raise ProviderError(
            "Dhan access token expired — paste a fresh token from the Developer Portal "
            "(or a SELF token from web.dhan.co for auto-renew)",
            code="DH-901",
        )

    renewable = is_renewable_token(token)
    should_renew = renewable and (
        force_renew or (seconds_left is not None and seconds_left < RENEW_BEFORE_SEC)
    )
    if should_renew:
        try:
            payload = renew_access_token(client_id=client_id, access_token=token)
            return _persist(settings, payload, source="renew")
        except ProviderError:
            if force_renew:
                raise
            return token

    if force_renew and not renewable:
        raise ProviderError(
            "This is a PARTNER (developer portal) token — RenewToken is not allowed by Dhan. "
            "Paste a new token tomorrow, or switch to a SELF token from web.dhan.co for auto-renew.",
            code="DH-905",
        )

    if not load_token_file().get("accessToken") and token:
        save_token(access_token=token, client_id=client_id, source="env")
    return token


def dhan_connection_status(settings: Settings) -> dict[str, Any]:
    status = token_status(settings.dhan_access_token, settings.dhan_client_id)
    out: dict[str, Any] = {
        **status,
        "profile": None,
        "ok": False,
        "error": None,
        "auto_renew": TokenRotator.instance().running,
        "totp_configured": bool(
            (getattr(settings, "dhan_pin", None) or "").strip()
            and (getattr(settings, "dhan_totp_secret", None) or "").strip()
        ),
    }
    token = current_token(settings)
    client_id = _client_id(settings)
    if not token or not client_id:
        out["error"] = "missing credentials"
        return out
    try:
        profile = fetch_profile(client_id=client_id, access_token=token)
        out["profile"] = {
            "dhanClientId": profile.get("dhanClientId"),
            "tokenValidity": profile.get("tokenValidity"),
            "dataPlan": profile.get("dataPlan"),
            "dataValidity": profile.get("dataValidity"),
            "activeSegment": profile.get("activeSegment"),
        }
        out["ok"] = True
        plan = str(profile.get("dataPlan") or "").lower()
        out["data_api_active"] = plan in {"active", "true", "1", "yes"}
        if not out.get("renewable"):
            out["renew_note"] = (
                "PARTNER portal token: valid for trading/data, but cannot use RenewToken. "
                "Paste a new token each day, or use a SELF token from web.dhan.co for auto-renew."
            )
    except ProviderError as exc:
        out["error"] = str(exc)
        out["error_code"] = exc.code
    return out


class TokenRotator:
    """Background renewer for renewable (SELF) web tokens only."""

    _inst: TokenRotator | None = None
    _inst_lock = threading.Lock()

    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._on_renew: list[Callable[[str], None]] = []
        self._last_error: str | None = None
        self._last_renew_at: float | None = None

    @classmethod
    def instance(cls) -> TokenRotator:
        with cls._inst_lock:
            if cls._inst is None:
                cls._inst = cls()
            return cls._inst

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def on_renew(self, callback: Callable[[str], None]) -> None:
        if callback not in self._on_renew:
            self._on_renew.append(callback)

    def off_renew(self, callback: Callable[[str], None]) -> None:
        self._on_renew = [cb for cb in self._on_renew if cb is not callback]

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="dhan-token-rotator", daemon=True)
        self._thread.start()
        logger.info("Dhan token rotator started (every %ss)", ROTATOR_INTERVAL_SEC)

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        self._tick()
        while not self._stop.wait(ROTATOR_INTERVAL_SEC):
            self._tick()

    def _tick(self) -> None:
        try:
            settings = get_settings()
            before = current_token(settings)
            if before and not is_renewable_token(before):
                # Partner tokens: keep using until expiry; no RenewToken spam.
                self._last_error = None
                return
            token = ensure_fresh_token(settings, force_renew=False)
            self._last_error = None
            if token and token != before:
                self._last_renew_at = time.time()
                logger.info("Dhan access token rotated")
                for cb in list(self._on_renew):
                    try:
                        cb(token)
                    except Exception:
                        logger.exception("token renew callback failed")
        except Exception as exc:
            self._last_error = str(exc)
            logger.warning("Dhan token rotate skipped: %s", exc)
