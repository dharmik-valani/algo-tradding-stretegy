from __future__ import annotations

import json
import logging
import tempfile
from pathlib import Path
from typing import Any

from algo.config import ROOT

logger = logging.getLogger(__name__)

DESK_PATH = ROOT / "data" / "paper_desk.json"
RUNTIME_PATH = ROOT / "data" / "paper_runtime.json"

DESK_KEY = "paper_desk"
RUNTIME_KEY = "paper_runtime"

_DEFAULT_DESK = {"strategies": [], "prefs": {"mode": "live", "poll_seconds": 15, "was_running": False}}
_DEFAULT_RUNTIME = {"runners": {}, "saved_at": None}


def _use_db_state() -> bool:
    """Prefer DB-backed desk/runtime whenever SQLAlchemy engine is up (esp. Postgres)."""
    try:
        from algo.config import get_settings

        url = (get_settings().database_url or "").strip().lower()
        if url.startswith("postgres"):
            return True
        # SQLite can also use the table so a later migrate is seamless; still mirror to files.
        return True
    except Exception:
        return False


def _db_get(key: str) -> dict[str, Any] | None:
    try:
        from algo.storage.db import get_engine, session_scope
        from algo.storage.models import PaperAppStateRow

        get_engine()
        with session_scope() as db:
            row = db.get(PaperAppStateRow, key)
            if row is None or not row.value_json:
                return None
            data = json.loads(row.value_json)
            return data if isinstance(data, dict) else None
    except Exception as exc:
        logger.debug("desk_store db get %s failed: %s", key, exc)
        return None


def _db_put(key: str, payload: dict[str, Any]) -> None:
    from algo.storage.db import get_engine, session_scope
    from algo.storage.models import PaperAppStateRow

    get_engine()
    raw = json.dumps(payload, default=str)
    with session_scope() as db:
        row = db.get(PaperAppStateRow, key)
        if row is None:
            db.add(PaperAppStateRow(key=key, value_json=raw))
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


def _load_file(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return dict(default)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else dict(default)
    except Exception:
        return dict(default)


def load_desk() -> dict[str, Any]:
    if _use_db_state():
        data = _db_get(DESK_KEY)
        if data is not None:
            data.setdefault("strategies", [])
            data.setdefault("prefs", _DEFAULT_DESK["prefs"])
            return data
        # Seed DB from local file once (first Postgres boot after migrate).
        seeded = _load_file(DESK_PATH, _DEFAULT_DESK)
        try:
            _db_put(DESK_KEY, seeded)
        except Exception:
            pass
        return seeded
    return _load_file(DESK_PATH, _DEFAULT_DESK)


def save_desk(payload: dict[str, Any]) -> None:
    if _use_db_state():
        try:
            _db_put(DESK_KEY, payload)
        except Exception as exc:
            logger.warning("desk_store db save failed, writing file: %s", exc)
            _atomic_write(DESK_PATH, payload)
            return
        # Keep a local mirror for offline inspection (best-effort).
        try:
            _atomic_write(DESK_PATH, payload)
        except Exception:
            pass
        return
    _atomic_write(DESK_PATH, payload)


def save_strategies(items: list[dict[str, Any]], prefs: dict[str, Any] | None = None) -> None:
    cur = load_desk()
    cur["strategies"] = items
    if prefs:
        cur["prefs"] = {**cur.get("prefs", {}), **prefs}
    save_desk(cur)


def load_runtime() -> dict[str, Any]:
    if _use_db_state():
        data = _db_get(RUNTIME_KEY)
        if data is not None:
            data.setdefault("runners", {})
            return data
        seeded = _load_file(RUNTIME_PATH, _DEFAULT_RUNTIME)
        seeded.setdefault("runners", {})
        try:
            _db_put(RUNTIME_KEY, seeded)
        except Exception:
            pass
        return seeded
    data = _load_file(RUNTIME_PATH, _DEFAULT_RUNTIME)
    data.setdefault("runners", {})
    return data


def save_runtime(payload: dict[str, Any]) -> None:
    if _use_db_state():
        try:
            _db_put(RUNTIME_KEY, payload)
        except Exception as exc:
            logger.warning("runtime db save failed, writing file: %s", exc)
            _atomic_write(RUNTIME_PATH, payload)
            return
        try:
            _atomic_write(RUNTIME_PATH, payload)
        except Exception:
            pass
        return
    _atomic_write(RUNTIME_PATH, payload)


def clear_runtime() -> None:
    empty = {"runners": {}, "saved_at": None}
    if _use_db_state():
        try:
            _db_put(RUNTIME_KEY, empty)
        except Exception:
            pass
    if RUNTIME_PATH.exists():
        try:
            RUNTIME_PATH.unlink()
        except Exception:
            pass
