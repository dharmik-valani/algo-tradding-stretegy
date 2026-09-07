from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any


def save_raw_json(
    root: Path,
    *,
    provider: str,
    kind: str,
    instrument_id: str,
    timeframe: str,
    start: date,
    end: date,
    payload: Any,
) -> Path:
    safe_id = instrument_id.replace(":", "_")
    path = root / provider / kind / safe_id / timeframe
    path.mkdir(parents=True, exist_ok=True)
    file_path = path / f"{start.isoformat()}_{end.isoformat()}.json"
    file_path.write_text(json.dumps(payload, default=str))
    return file_path
