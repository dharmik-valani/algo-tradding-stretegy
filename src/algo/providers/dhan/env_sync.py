"""Upsert keys in the project `.env` without rewriting unrelated lines."""

from __future__ import annotations

from pathlib import Path

from algo.config import ROOT

ENV_PATH = ROOT / ".env"


def upsert_dotenv(updates: dict[str, str], *, path: Path | None = None) -> Path:
    """Set or replace KEY=value lines. Empty values clear the key line to KEY=."""
    target = path or ENV_PATH
    lines: list[str] = []
    if target.exists():
        lines = target.read_text(encoding="utf-8").splitlines()

    pending = {str(k).strip(): "" if v is None else str(v) for k, v in updates.items() if str(k).strip()}
    seen: set[str] = set()
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            out.append(line)
            continue
        key = line.split("=", 1)[0].strip()
        if key in pending:
            out.append(f"{key}={pending[key]}")
            seen.add(key)
        else:
            out.append(line)
    for key, value in pending.items():
        if key not in seen:
            out.append(f"{key}={value}")

    text = "\n".join(out)
    if text and not text.endswith("\n"):
        text += "\n"
    target.write_text(text, encoding="utf-8")
    return target
