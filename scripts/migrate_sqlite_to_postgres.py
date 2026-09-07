#!/usr/bin/env python3
"""Copy paper_* (+ app state) from local SQLite into Postgres (Supabase).

Usage:
  DATABASE_URL='postgresql://postgres:...@db.REF.supabase.co:5432/postgres' \\
    PYTHONPATH=src python scripts/migrate_sqlite_to_postgres.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from algo.config import ROOT as ALGO_ROOT, get_settings
from algo.storage.models import (
    Base,
    PaperAppStateRow,
    PaperFillJournalRow,
    PaperSelectionRow,
    PaperSessionRow,
    PaperTradeRow,
)

SQLITE_PATH = ALGO_ROOT / "data" / "algo.db"
DESK_PATH = ALGO_ROOT / "data" / "paper_desk.json"
RUNTIME_PATH = ALGO_ROOT / "data" / "paper_runtime.json"

TABLES = (
    PaperSessionRow,
    PaperSelectionRow,
    PaperTradeRow,
    PaperFillJournalRow,
)


def _pg_url(raw: str) -> str:
    url = raw.strip()
    if url.startswith("postgres://"):
        return "postgresql+psycopg://" + url.removeprefix("postgres://")
    if url.startswith("postgresql://") and "+psycopg" not in url:
        return "postgresql+psycopg://" + url.removeprefix("postgresql://")
    return url


def main() -> int:
    pg = os.environ.get("DATABASE_URL") or get_settings().database_url
    if not pg or pg.startswith("sqlite"):
        print("Set DATABASE_URL to your Supabase Postgres URI first.", file=sys.stderr)
        return 1
    if not SQLITE_PATH.exists():
        print(f"Missing {SQLITE_PATH}", file=sys.stderr)
        return 1

    sqlite_engine = create_engine(f"sqlite:///{SQLITE_PATH}", connect_args={"check_same_thread": False})
    pg_engine = create_engine(_pg_url(pg), pool_pre_ping=True)
    Base.metadata.create_all(pg_engine)

    Src = sessionmaker(bind=sqlite_engine, expire_on_commit=False)
    Dst = sessionmaker(bind=pg_engine, expire_on_commit=False)

    with Src() as src, Dst() as dst:
        # Wipe paper tables on destination (idempotent re-run for testing).
        for model in reversed(TABLES):
            dst.query(model).delete()
        dst.query(PaperAppStateRow).delete()
        dst.commit()

        counts: dict[str, int] = {}
        for model in TABLES:
            rows = src.query(model).all()
            for row in rows:
                dst.merge(row)
            counts[model.__tablename__] = len(rows)
        dst.commit()

        # Desk / runtime blobs
        desk = json.loads(DESK_PATH.read_text()) if DESK_PATH.exists() else {"strategies": [], "prefs": {}}
        runtime = json.loads(RUNTIME_PATH.read_text()) if RUNTIME_PATH.exists() else {"runners": {}}
        dst.merge(PaperAppStateRow(key="paper_desk", value_json=json.dumps(desk, default=str)))
        dst.merge(PaperAppStateRow(key="paper_runtime", value_json=json.dumps(runtime, default=str)))
        dst.commit()

        # Sanity
        with pg_engine.connect() as conn:
            n = conn.execute(text("select count(*) from paper_trades")).scalar()
        print("Migrated:", counts)
        print("paper_app_state: desk+runtime")
        print("postgres paper_trades:", n)
    print("OK — point local + Render DATABASE_URL at this Postgres.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
