from __future__ import annotations

"""Persist Dhan access token (24h JWT) outside of process memory."""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from algo.config import ROOT

TOKEN_PATH = ROOT / "data" / "dhan_token.json"


def _parse_expiry(raw: str | None) -> datetime | None:
    if not raw:
        return None
    text = str(raw).strip()
    for fmt in (
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%d/%m/%Y %H:%M",
    ):
        try:
            return datetime.strptime(text.replace("Z", ""), fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    try:
        # ISO with offset
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def jwt_claims(token: str) -> dict[str, Any]:
    """Decode JWT payload without verifying signature (local metadata only)."""
    try:
        import base64

        parts = token.split(".")
        if len(parts) < 2:
            return {}
        pad = "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(parts[1] + pad))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def jwt_consumer_type(token: str) -> str:
    """SELF (web) can RenewToken; PARTNER (developer portal) cannot."""
    return str(jwt_claims(token).get("tokenConsumerType") or "").upper()


def jwt_expiry(token: str) -> datetime | None:
    """Read exp claim without verifying signature (local scheduling only)."""
    try:
        exp = jwt_claims(token).get("exp")
        if exp is None:
            return None
        return datetime.fromtimestamp(int(exp), tz=timezone.utc)
    except Exception:
        return None


def load_token_file() -> dict[str, Any]:
    if not TOKEN_PATH.exists():
        return {}
    try:
        data = json.loads(TOKEN_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_token(
    *,
    access_token: str,
    client_id: str = "",
    expiry_time: str | None = None,
    source: str = "manual",
) -> Path:
    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    access_token = access_token.strip()
    claims = jwt_claims(access_token)
    exp_dt = _parse_expiry(expiry_time) or jwt_expiry(access_token)
    consumer = str(claims.get("tokenConsumerType") or "").upper()
    payload = {
        "accessToken": access_token,
        "clientId": client_id or str(claims.get("dhanClientId") or ""),
        "expiryTime": expiry_time or (exp_dt.isoformat() if exp_dt else None),
        "expiryEpoch": int(exp_dt.timestamp()) if exp_dt else None,
        "consumerType": consumer or None,
        "renewable": consumer in {"", "SELF"},  # PARTNER / API-key flows cannot RenewToken
        "source": source,
        "updatedAt": datetime.now(tz=timezone.utc).isoformat(),
    }
    tmp = TOKEN_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(TOKEN_PATH)
    return TOKEN_PATH


def resolve_access_token(env_token: str = "") -> str:
    data = load_token_file()
    file_tok = str(data.get("accessToken") or "").strip()
    return file_tok or (env_token or "").strip()


def token_status(env_token: str = "", env_client_id: str = "") -> dict[str, Any]:
    data = load_token_file()
    token = resolve_access_token(env_token)
    exp = None
    if data.get("expiryEpoch"):
        try:
            exp = datetime.fromtimestamp(int(data["expiryEpoch"]), tz=timezone.utc)
        except Exception:
            exp = None
    if exp is None:
        exp = _parse_expiry(data.get("expiryTime")) or jwt_expiry(token)
    now = datetime.now(tz=timezone.utc)
    seconds_left = int((exp - now).total_seconds()) if exp else None
    consumer = str(data.get("consumerType") or jwt_consumer_type(token) or "")
    renewable = bool(data.get("renewable")) if "renewable" in data else consumer in {"", "SELF"}
    if consumer == "PARTNER":
        renewable = False
    return {
        "has_token": bool(token),
        "client_id": data.get("clientId") or env_client_id,
        "expiry_time": exp.isoformat() if exp else data.get("expiryTime"),
        "seconds_left": seconds_left,
        "expired": bool(exp and seconds_left is not None and seconds_left <= 0),
        "source": data.get("source") or ("env" if env_token else "none"),
        "token_path": str(TOKEN_PATH),
        "consumer_type": consumer or None,
        "renewable": renewable,
    }
