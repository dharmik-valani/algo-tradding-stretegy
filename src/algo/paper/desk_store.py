from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

from algo.config import ROOT

DESK_PATH = ROOT / "data" / "paper_desk.json"
RUNTIME_PATH = ROOT / "data" / "paper_runtime.json"


def load_desk() -> dict[str, Any]:
    if not DESK_PATH.exists():
        return {"strategies": [], "prefs": {"mode": "live", "poll_seconds": 15, "was_running": False}}
    try:
        return json.loads(DESK_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"strategies": [], "prefs": {"mode": "live", "poll_seconds": 15, "was_running": False}}


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


def save_desk(payload: dict[str, Any]) -> None:
    _atomic_write(DESK_PATH, payload)


def save_strategies(items: list[dict[str, Any]], prefs: dict[str, Any] | None = None) -> None:
    cur = load_desk()
    cur["strategies"] = items
    if prefs:
        cur["prefs"] = {**cur.get("prefs", {}), **prefs}
    save_desk(cur)


def load_runtime() -> dict[str, Any]:
    if not RUNTIME_PATH.exists():
        return {"runners": {}, "saved_at": None}
    try:
        data = json.loads(RUNTIME_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {"runners": {}, "saved_at": None}
        data.setdefault("runners", {})
        return data
    except Exception:
        return {"runners": {}, "saved_at": None}


def save_runtime(payload: dict[str, Any]) -> None:
    _atomic_write(RUNTIME_PATH, payload)


def clear_runtime() -> None:
    if RUNTIME_PATH.exists():
        try:
            RUNTIME_PATH.unlink()
        except Exception:
            pass
