from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from algo.config import Settings, get_settings
from algo.storage.models import Base

_ENGINE: Engine | None = None
_SESSION: sessionmaker[Session] | None = None


def get_engine(settings: Settings | None = None) -> Engine:
    global _ENGINE, _SESSION
    settings = settings or get_settings()
    url = settings.resolve_db_url()
    if url.startswith("sqlite"):
        path = Path(url.replace("sqlite:///", ""))
        path.parent.mkdir(parents=True, exist_ok=True)
        engine = create_engine(url, connect_args={"check_same_thread": False})
    else:
        engine = create_engine(url)
    Base.metadata.create_all(engine)
    _ENGINE = engine
    _SESSION = sessionmaker(bind=engine, expire_on_commit=False)
    return engine


def reset_engine() -> None:
    global _ENGINE, _SESSION
    if _ENGINE is not None:
        _ENGINE.dispose()
    _ENGINE = None
    _SESSION = None


@contextmanager
def session_scope(settings: Settings | None = None) -> Iterator[Session]:
    if _SESSION is None:
        get_engine(settings)
    assert _SESSION is not None
    session = _SESSION()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
