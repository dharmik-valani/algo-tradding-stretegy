"""Persist Dhan access token for local + server (Render).

Priority on read: ``paper_app_state`` DB row → ``data/dhan_token.json`` → env.
Writes always update DB (when available) and mirror to the JSON file.
On Render the filesystem is ephemeral; Postgres keeps renewals alive across restarts.
"""

from __future__ import annotations

import json
import logging
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from algo.config import ROOT

logger = logging.getLogger(__name__)

TOKEN_PATH = ROOT / "data" / "dhan_token.json"
TOKEN_KEY = "dhan_token"
IST = ZoneInfo("Asia/Kolkata")


def _parse_expiry(raw: str | None) -> datetime | None:
    if not raw:
        return None
    text = str(raw).strip()
    # Dhan profile tokenValidity is IST wall clock without offset.
    for fmt, tz in (
        ("%d/%m/%Y %H:%M", IST),
        ("%Y-%m-%dT%H:%M:%S.%f", timezone.utc),
        ("%Y-%m-%dT%H:%M:%S", timezone.utc),
        ("%Y-%m-%d %H:%M:%S", timezone.utc),
    ):
        try:
            return datetime.strptime(text.replace("Z", ""), fmt).replace(tzinfo=tz).astimezone(
                timezone.utc
            )
        except ValueError:
            continue
    try:
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


def _db_get() -> dict[str, Any] | None:
    try:
        from algo.storage.db import get_engine, session_scope
        from algo.storage.models import PaperAppStateRow

        get_engine()
        with session_scope() as db:
            row = db.get(PaperAppStateRow, TOKEN_KEY)
            if row is None or not row.value_json:
                return None
            data = json.loads(row.value_json)
            return data if isinstance(data, dict) else None
    except Exception as exc:
        logger.debug("dhan token db get failed: %s", exc)
        return None


def _db_put(payload: dict[str, Any]) -> None:
    from algo.storage.db import get_engine, session_scope
    from algo.storage.models import PaperAppStateRow

    get_engine()
    raw = json.dumps(payload, default=str)
    with session_scope() as db:
        row = db.get(PaperAppStateRow, TOKEN_KEY)
        if row is None:
            db.add(PaperAppStateRow(key=TOKEN_KEY, value_json=raw))
        else:
            row.value_json = raw


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(payload, indent=2, default=str)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=str(path.parent),
        delete=False,
        prefix=f".{path.name}.",
        suffix=".tmp",
    ) as tmp:
        tmp.write(raw)
        tmp.flush()
        tmp_name = tmp.name
    Path(tmp_name).replace(path)


def _load_file() -> dict[str, Any]:
    if not TOKEN_PATH.exists():
        return {}
    try:
        data = json.loads(TOKEN_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def load_token_file() -> dict[str, Any]:
    """Load token blob — DB first, then local JSON (legacy name kept for callers)."""
    db_data = _db_get()
    if db_data and str(db_data.get("accessToken") or "").strip():
        return db_data
    file_data = _load_file()
    if file_data and str(file_data.get("accessToken") or "").strip():
        # Seed DB once so Render / Postgres boots pick it up.
        try:
            _db_put(file_data)
        except Exception:
            pass
        return file_data
    return db_data or file_data or {}


def _build_payload(
    *,
    access_token: str,
    client_id: str = "",
    expiry_time: str | None = None,
    source: str = "manual",
) -> dict[str, Any]:
    access_token = access_token.strip()
    claims = jwt_claims(access_token)
    # Prefer JWT exp (authoritative) over ambiguous profile wall-clock strings.
    exp_dt = jwt_expiry(access_token) or _parse_expiry(expiry_time)
    consumer = str(claims.get("tokenConsumerType") or "").upper()
    return {
        "accessToken": access_token,
        "clientId": client_id or str(claims.get("dhanClientId") or ""),
        "expiryTime": exp_dt.isoformat() if exp_dt else (expiry_time or None),
        "expiryEpoch": int(exp_dt.timestamp()) if exp_dt else None,
        "consumerType": consumer or None,
        "renewable": consumer in {"", "SELF"},
        "source": source,
        "updatedAt": datetime.now(tz=timezone.utc).isoformat(),
        "storage": "db+file",
    }


def save_token(
    *,
    access_token: str,
    client_id: str = "",
    expiry_time: str | None = None,
    source: str = "manual",
) -> Path:
    payload = _build_payload(
        access_token=access_token,
        client_id=client_id,
        expiry_time=expiry_time,
        source=source,
    )
    try:
        _db_put(payload)
    except Exception as exc:
        logger.warning("dhan token db save failed, using file only: %s", exc)
    try:
        _atomic_write(TOKEN_PATH, payload)
    except Exception as exc:
        logger.warning("dhan token file save failed: %s", exc)
    return TOKEN_PATH


def resolve_access_token(env_token: str = "") -> str:
    data = load_token_file()
    stored = str(data.get("accessToken") or "").strip()
    return stored or (env_token or "").strip()


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
        exp = jwt_expiry(token) or _parse_expiry(data.get("expiryTime"))
    now = datetime.now(tz=timezone.utc)
    seconds_left = int((exp - now).total_seconds()) if exp else None
    consumer = str(data.get("consumerType") or jwt_consumer_type(token) or "")
    if consumer == "SELF":
        renewable = True
    elif consumer == "PARTNER":
        renewable = False
    else:
        renewable = bool(data.get("renewable")) if "renewable" in data else consumer in {""}
    db_blob = _db_get()
    db_ok = bool(db_blob and str(db_blob.get("accessToken") or "").strip())
    return {
        "has_token": bool(token),
        "client_id": data.get("clientId") or env_client_id,
        "expiry_time": exp.isoformat() if exp else data.get("expiryTime"),
        "seconds_left": seconds_left,
        "expired": bool(exp and seconds_left is not None and seconds_left <= 0),
        "source": data.get("source") or ("env" if env_token else "none"),
        "token_path": str(TOKEN_PATH),
        "storage": "db" if db_ok else ("file" if TOKEN_PATH.exists() else "env"),
        "consumer_type": consumer or None,
        "renewable": renewable,
    }
