"""Dhan access-token lifecycle for paper / data APIs.

Docs: https://dhanhq.co/docs/v2/authentication/

- RenewToken: only for **active SELF** tokens from web.dhan.co (not PARTNER / portal)
- generateAccessToken (PIN + TOTP): mints a fresh token after expiry — works for daily auto-refresh
- Partner / Developer Portal tokens: cannot RenewToken; paste daily OR enable TOTP + set secret
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

import requests

from algo.config import Settings, get_settings
from algo.providers.base import ProviderError
from algo.providers.dhan.env_sync import upsert_dotenv
from algo.providers.dhan.token_store import (
    jwt_claims,
    jwt_consumer_type,
    jwt_expiry,
    load_token_file,
    resolve_access_token,
    save_token,
    token_status,
)

logger = logging.getLogger(__name__)

AUTH_BASE = "https://auth.dhan.co"
API_BASE = "https://api.dhan.co/v2"

# Renew / remint when fewer than this many seconds remain (~12h buffer = once per day).
# IMPORTANT: Dhan RenewToken *expires the current JWT first*. A failed renew can leave
# you with no token — never call it early / for testing.
RENEW_BEFORE_SEC = 12 * 60 * 60
# Background rotator cadence.
ROTATOR_INTERVAL_SEC = 15 * 60
# Do not RenewToken until the JWT has lived at least this long (avoid mint-then-kill).
MIN_TOKEN_AGE_BEFORE_RENEW_SEC = 30 * 60


def _client_id(settings: Settings) -> str:
    data = load_token_file()
    return str(data.get("clientId") or settings.dhan_client_id or "").strip()


def current_token(settings: Settings) -> str:
    return resolve_access_token(settings.dhan_access_token)


def totp_configured(settings: Settings) -> bool:
    pin = (getattr(settings, "dhan_pin", None) or "").strip()
    secret = (getattr(settings, "dhan_totp_secret", None) or "").strip()
    return bool(pin and secret)


def is_renewable_token(token: str) -> bool:
    """Dhan RenewToken only works for web SELF tokens — not PARTNER portal JWTs.

    JWT claims win over stale ``data/dhan_token.json`` metadata (e.g. leftover
    ``renewable: false`` from a previous PARTNER paste).
    """
    consumer = jwt_consumer_type(token)
    if consumer == "PARTNER":
        return False
    if consumer == "SELF":
        return True
    stored = load_token_file()
    if stored.get("accessToken") == token and stored.get("renewable") is False:
        return False
    return consumer in {""} or bool(stored.get("renewable"))


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


def _token_age_sec(token: str) -> float | None:
    try:
        iat = jwt_claims(token).get("iat")
        if iat is None:
            return None
        return time.time() - float(iat)
    except Exception:
        return None


def renew_access_token(*, client_id: str, access_token: str, timeout: float = 20.0) -> dict[str, Any]:
    """Renew an *active* web SELF token via GET ``/v2/RenewToken``.

    Warning: Dhan expires the current token as soon as renew is accepted/attempted.
    Only call when near expiry and profile still works.
    """
    if not is_renewable_token(access_token):
        raise ProviderError(
            "RenewToken not allowed for PARTNER / developer-portal tokens — "
            "use PIN+TOTP generateAccessToken, or paste a fresh token / SELF token from web.dhan.co",
            code="DH-905",
            retryable=False,
        )
    # Confirm token still works before asking Dhan to expire it.
    try:
        fetch_profile(client_id=client_id, access_token=access_token)
    except ProviderError as exc:
        raise ProviderError(
            f"Skipping RenewToken — current token already rejected by profile: {exc}",
            code=exc.code or "DH-901",
            retryable=False,
        ) from exc

    url = f"{API_BASE}/RenewToken"
    # Community + SDK: GET with access-token only is most reliable; include client id too.
    headers = {"access-token": access_token, "dhanClientId": client_id}
    last_err: Exception | None = None
    try:
        response = requests.get(url, headers=headers, timeout=timeout)
        payload = response.json() if response.content else {}
        if response.status_code == 200 and isinstance(payload, dict) and payload.get("accessToken"):
            return payload
        msg = (
            (payload.get("errorMessage") if isinstance(payload, dict) else None)
            or (payload.get("message") if isinstance(payload, dict) else None)
            or payload
            or response.text
        )
        last_err = ProviderError(str(msg), code=str((payload or {}).get("errorCode") or "RENEW"))
    except Exception as exc:
        last_err = exc
    try:
        payload = _login(client_id).renew_token(access_token)
        if isinstance(payload, dict) and payload.get("accessToken"):
            return payload
    except Exception as exc:
        last_err = exc
    raise ProviderError(
        f"RenewToken failed (current JWT may have been invalidated by Dhan): {last_err}",
        code="RENEW",
        retryable=False,
    )


def regenerate_via_totp(settings: Settings) -> dict[str, Any]:
    """Mint a new token via PIN + TOTP (generateAccessToken).

    curl POST 'https://auth.dhan.co/app/generateAccessToken?dhanClientId=…&pin=…&totp=…'
    Requires TOTP enabled on the Dhan account and DHAN_PIN + DHAN_TOTP_SECRET in .env.
    """
    client_id = _client_id(settings)
    pin = (getattr(settings, "dhan_pin", None) or "").strip()
    secret = (getattr(settings, "dhan_totp_secret", None) or "").strip()
    if not client_id or not pin or not secret:
        raise ProviderError(
            "Set DHAN_PIN and DHAN_TOTP_SECRET (TOTP base32 from web.dhan.co → Setup TOTP) "
            "to auto-mint tokens after expiry",
            code="NO_TOTP",
        )
    try:
        import pyotp
    except ImportError as exc:
        raise ProviderError("pyotp required for TOTP regen — pip install pyotp", code="NO_PYOTP") from exc

    totp = pyotp.TOTP(secret.replace(" ", "").upper()).now()
    url = f"{AUTH_BASE}/app/generateAccessToken"
    params = {"dhanClientId": client_id, "pin": pin, "totp": totp}
    try:
        response = requests.post(url, params=params, timeout=30)
        payload = response.json() if response.content else {}
    except Exception as exc:
        raise ProviderError(str(exc), code="TOTP", retryable=False) from exc

    if response.status_code == 200 and isinstance(payload, dict) and payload.get("accessToken"):
        return payload

    # SDK fallback (same endpoint)
    try:
        payload = _login(client_id).generate_token(pin, totp)
        if isinstance(payload, dict) and payload.get("accessToken"):
            return payload
    except Exception as exc:
        raise ProviderError(str(exc), code="TOTP", retryable=False) from exc

    msg = (
        (payload.get("errorMessage") if isinstance(payload, dict) else None)
        or (payload.get("message") if isinstance(payload, dict) else None)
        or payload
        or response.text
    )
    raise ProviderError(str(msg), code=str((payload or {}).get("errorCode") or "TOTP"), retryable=False)


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


def apply_auth_payload(settings: Settings, payload: dict[str, Any], *, source: str) -> str:
    client_id = _client_id(settings)
    new_tok = str(payload.get("accessToken") or "").strip()
    if not new_tok:
        raise ProviderError("empty accessToken in auth payload", code="AUTH")
    cid = str(payload.get("dhanClientId") or client_id)
    save_token(
        access_token=new_tok,
        client_id=cid,
        expiry_time=str(payload.get("expiryTime") or "") or None,
        source=source,
    )
    settings.dhan_access_token = new_tok
    if cid:
        settings.dhan_client_id = cid
    try:
        upsert_dotenv({"DHAN_ACCESS_TOKEN": new_tok, "DHAN_CLIENT_ID": cid or settings.dhan_client_id})
    except Exception:
        logger.warning("Could not sync DHAN_ACCESS_TOKEN into .env", exc_info=True)
    try:
        get_settings.cache_clear()
    except Exception:
        pass
    return new_tok


def ensure_fresh_token(settings: Settings, *, force_renew: bool = False) -> str:
    """Return a usable access token.

    Priority:
    1. SELF near expiry → RenewToken (Dhan expires the old JWT — only when <12h left)
    2. Expired or renew failed → generateAccessToken when PIN+TOTP configured
    3. Otherwise return current token

    ``force_renew`` still respects the near-expiry window so we never burn a fresh
    24h token by calling RenewToken too early.
    """
    client_id = _client_id(settings)
    token = current_token(settings)
    if not client_id:
        raise ProviderError("DHAN_CLIENT_ID missing — set in Settings or .env", code="NO_TOKEN")
    if not token and not totp_configured(settings):
        raise ProviderError(
            "Access token missing — paste one, or configure DHAN_PIN + DHAN_TOTP_SECRET",
            code="NO_TOKEN",
        )

    seconds_left = _seconds_left(token) if token else -1.0
    expired = bool(token and seconds_left is not None and seconds_left <= 0) or not token
    renewable = bool(token) and is_renewable_token(token)
    near_expiry = seconds_left is not None and seconds_left < RENEW_BEFORE_SEC
    age = _token_age_sec(token) if token else None
    old_enough = age is None or age >= MIN_TOKEN_AGE_BEFORE_RENEW_SEC

    # Expired: only TOTP (or manual paste) can help — RenewToken rejects expired JWTs.
    if expired:
        if totp_configured(settings):
            logger.info("Dhan token missing/expired — minting via PIN+TOTP")
            payload = regenerate_via_totp(settings)
            return apply_auth_payload(settings, payload, source="totp")
        raise ProviderError(
            "Dhan access token expired. RenewToken cannot revive an expired JWT. "
            "Paste a fresh token, or set DHAN_TOTP_SECRET (+ DHAN_PIN) for auto generateAccessToken.",
            code="DH-901",
        )

    # Active SELF → RenewToken only near expiry (and not brand-new).
    should_renew = renewable and near_expiry and old_enough
    if force_renew and renewable and not near_expiry:
        raise ProviderError(
            f"Refusing early RenewToken — token still has ~{int((seconds_left or 0) / 3600)}h left. "
            f"Dhan expires the current JWT on renew; auto-refresh runs when <{RENEW_BEFORE_SEC // 3600}h remain.",
            code="RENEW_EARLY",
        )
    if should_renew:
        try:
            payload = renew_access_token(client_id=client_id, access_token=token)
            return apply_auth_payload(settings, payload, source="renew")
        except ProviderError as exc:
            if totp_configured(settings):
                logger.warning("RenewToken failed (%s) — trying PIN+TOTP", exc)
                payload = regenerate_via_totp(settings)
                return apply_auth_payload(settings, payload, source="totp")
            if force_renew:
                raise
            return token

    # PARTNER (or non-renewable) near expiry → TOTP remint
    if totp_configured(settings) and near_expiry:
        try:
            payload = regenerate_via_totp(settings)
            return apply_auth_payload(settings, payload, source="totp")
        except ProviderError:
            if force_renew:
                raise
            return token

    if force_renew and not renewable:
        raise ProviderError(
            "This is a PARTNER (developer portal) token — RenewToken is not allowed by Dhan. "
            "Paste a new token, use a SELF token from web.dhan.co, or set DHAN_PIN + DHAN_TOTP_SECRET "
            "to auto-mint via generateAccessToken.",
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
        "totp_configured": totp_configured(settings),
        "pin_configured": bool((getattr(settings, "dhan_pin", None) or "").strip()),
    }
    token = current_token(settings)
    client_id = _client_id(settings)
    if not token or not client_id:
        out["error"] = "missing credentials"
        if totp_configured(settings):
            out["renew_note"] = "No token on file — click Generate / Renew to mint via PIN+TOTP."
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
        if out.get("renewable"):
            out["renew_note"] = "SELF token: RenewToken will refresh before expiry."
        elif out.get("totp_configured"):
            out["renew_note"] = (
                "PARTNER token + TOTP configured: will mint a new token via generateAccessToken "
                "before/at expiry (RenewToken is not used)."
            )
        else:
            out["renew_note"] = (
                "PARTNER portal token: cannot RenewToken. Paste daily, switch to SELF from "
                "web.dhan.co, or add DHAN_TOTP_SECRET for auto generateAccessToken."
            )
    except ProviderError as exc:
        out["error"] = str(exc)
        out["error_code"] = exc.code
        if totp_configured(settings):
            out["renew_note"] = "Token rejected — try Generate / Renew (PIN+TOTP remint)."
    return out


class TokenRotator:
    """Background refresher: RenewToken for SELF, or PIN+TOTP remint when configured."""

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
            if before and not is_renewable_token(before) and not totp_configured(settings):
                left = _seconds_left(before)
                if left is not None and left <= 0:
                    self._last_error = (
                        "PARTNER token expired — paste a fresh token or set DHAN_TOTP_SECRET"
                    )
                else:
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
