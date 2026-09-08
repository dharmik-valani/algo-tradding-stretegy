from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from algo.paper.instruments import instrument_catalog
from algo.paper.journal import (
    calendar_pnl,
    daily_report,
    list_selections,
    list_trades,
    report_summary,
    trades_csv,
)
from algo.paper.session import get_session, reload_session_from_disk, reset_session

STATIC = Path(__file__).parent / "static"

app = FastAPI(title="Algo Paper Desk", version="0.1.0")
app.mount("/static", StaticFiles(directory=STATIC), name="static")


def _require_cron_secret(x_cron_secret: str | None) -> None:
    """If CRON_SECRET is set on the service, cron callers must send matching header."""
    expected = (os.environ.get("CRON_SECRET") or "").strip()
    if not expected:
        return
    if (x_cron_secret or "").strip() != expected:
        raise HTTPException(status_code=401, detail="invalid cron secret")


class AddStrategyBody(BaseModel):
    strategy_id: str
    symbol: str = "NIFTY"
    timeframe: str = "5m"
    quantity: int = 1
    starting_cash: float = 100_000.0
    params: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True
    asset_kind: str | None = None
    instance_id: str | None = None


class EnableBody(BaseModel):
    enabled: bool


class StartBody(BaseModel):
    mode: str = "replay"  # replay | live
    max_bars: int | None = 2000
    poll_seconds: float = 15.0


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/api/health")
def health() -> dict:
    """Liveness probe + free-tier keep-alive target (must stay cheap/fast)."""
    out: dict[str, Any] = {"ok": True, "service": "paper-desk"}
    try:
        # Avoid get_session() — first call can restore desk + auto-resume (network).
        # Probe must answer even while that is still in progress.
        from algo.paper import session as session_mod

        sess = session_mod._SESSION
        if sess is None:
            out["running"] = None
            out["booting"] = True
        else:
            out["running"] = bool(sess.running)
            out["mode"] = sess.mode
            out["strategies"] = len(sess.runners)
            out["ready"] = session_mod._SESSION_READY.is_set()
    except Exception:
        # Never fail the probe — Render / cron only need ok=true.
        out["running"] = None
    return out


@app.post("/api/session/wake")
def wake_live(
    poll_seconds: float = Query(15.0, ge=5.0, le=120.0),
    x_cron_secret: str | None = Header(default=None, alias="X-Cron-Secret"),
) -> dict:
    """Idempotent live start for free-tier cron (pre-market wake + keep-alive).

    - If already running: no-op success
    - If strategies exist and idle: start live paper
    """
    _require_cron_secret(x_cron_secret)
    session = get_session()
    if session.running and session.mode == "live":
        return {
            "ok": True,
            "already_running": True,
            "running": True,
            "strategies": len(session.runners),
            "message": session.message,
        }
    if not session.runners:
        raise HTTPException(
            status_code=400,
            detail="No strategies on desk — open the UI once and add strategies first",
        )
    try:
        session.start_live(poll_seconds=poll_seconds)
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    session.persist_desk()
    return {
        "ok": True,
        "already_running": False,
        "running": True,
        "strategies": len(session.runners),
        "message": session.message,
    }


@app.post("/api/session/sleep")
def sleep_live(
    x_cron_secret: str | None = Header(default=None, alias="X-Cron-Secret"),
) -> dict:
    """Stop live session after market close (optional cron). Idempotent."""
    _require_cron_secret(x_cron_secret)
    session = get_session()
    if not session.running:
        return {"ok": True, "already_stopped": True, "running": False}
    session.stop()
    session.persist_desk()
    return {"ok": True, "already_stopped": False, "running": False, "message": session.message}


class SnapshotRestoreBody(BaseModel):
    """Base64 payloads from a laptop `data/` folder (one-shot sync to Render)."""

    sync_token: str
    desk_json_b64: str | None = None
    runtime_json_b64: str | None = None
    sqlite_b64: str | None = None


@app.post("/api/admin/restore-snapshot")
def restore_snapshot(body: SnapshotRestoreBody) -> dict:
    """Replace desk/runtime/SQLite from a local export. Requires PAPER_SYNC_TOKEN."""
    import base64
    import os
    from pathlib import Path

    from algo.config import ROOT

    expected = (os.environ.get("PAPER_SYNC_TOKEN") or "").strip()
    if not expected or body.sync_token.strip() != expected:
        raise HTTPException(status_code=401, detail="invalid sync token")

    data_dir = ROOT / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    written: list[str] = []

    def _write(name: str, b64: str | None) -> None:
        if not b64:
            return
        raw = base64.b64decode(b64)
        path = data_dir / name
        path.write_bytes(raw)
        written.append(name)

    try:
        _write("paper_desk.json", body.desk_json_b64)
        _write("paper_runtime.json", body.runtime_json_b64)
        _write("algo.db", body.sqlite_b64)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"decode/write failed: {exc}") from exc

    if not written:
        raise HTTPException(status_code=400, detail="nothing to restore")

    session = reload_session_from_disk()
    snap = session.snapshot().model_dump(mode="json")
    return {
        "ok": True,
        "written": written,
        "strategies": len(snap.get("strategies") or []),
        "message": snap.get("message"),
    }


class DhanTokenBody(BaseModel):
    access_token: str
    client_id: str | None = None


@app.get("/api/dhan/status")
def dhan_status() -> dict:
    from algo.config import get_settings
    from algo.providers.dhan.auth import TokenRotator, dhan_connection_status

    # Keep RenewToken cycling while the desk UI is open (even before Start live).
    TokenRotator.instance().start()
    return dhan_connection_status(get_settings())


@app.post("/api/dhan/token")
def dhan_set_token(body: DhanTokenBody) -> dict:
    from algo.config import get_settings
    from algo.providers.dhan.auth import dhan_connection_status, fetch_profile
    from algo.providers.dhan.token_store import save_token

    settings = get_settings()
    token = body.access_token.strip()
    if not token:
        raise HTTPException(status_code=400, detail="access_token required")
    client_id = (body.client_id or settings.dhan_client_id or "").strip()
    if not client_id:
        raise HTTPException(status_code=400, detail="DHAN_CLIENT_ID missing in .env")
    try:
        profile = fetch_profile(client_id=client_id, access_token=token)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Token rejected by Dhan: {exc}") from exc
    save_token(
        access_token=token,
        client_id=str(profile.get("dhanClientId") or client_id),
        expiry_time=str(profile.get("tokenValidity") or "") or None,
        source="ui",
    )
    settings.dhan_access_token = token
    get_settings.cache_clear()
    from algo.providers.dhan.auth import TokenRotator

    TokenRotator.instance().start()
    return dhan_connection_status(get_settings())


@app.post("/api/dhan/renew")
def dhan_renew() -> dict:
    from algo.config import get_settings
    from algo.providers.dhan.auth import dhan_connection_status, ensure_fresh_token

    settings = get_settings()
    try:
        ensure_fresh_token(settings, force_renew=True)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    get_settings.cache_clear()
    return dhan_connection_status(get_settings())


@app.get("/api/strategies/catalog")
def catalog() -> list[dict]:
    return get_session().available_strategies()


@app.get("/api/instruments")
def instruments() -> dict:
    return instrument_catalog()


@app.get("/api/session")
def session_state() -> dict:
    return get_session().snapshot().model_dump(mode="json")


@app.get("/api/analytics")
def analytics() -> dict:
    return get_session().analytics()


@app.post("/api/session/reset")
def session_reset() -> dict:
    return reset_session().snapshot().model_dump(mode="json")


@app.post("/api/session/strategies/orb-pair")
def add_orb_pair() -> dict:
    """Add both NIFTY Opt ORB Call + Put with defaults (market-open pair)."""
    session = get_session()
    added = []
    errors = []
    for strategy_id in ("nifty_opt_orb_ce", "nifty_opt_orb_pe"):
        try:
            state = session.add_strategy(
                strategy_id,
                symbol="NIFTY",
                timeframe="1m",
                quantity=1,
                starting_cash=100_000.0,
                asset_kind="option",
                enabled=True,
            )
            added.append(state.instance_id)
        except ValueError as exc:
            errors.append(str(exc))
    snap = session.snapshot().model_dump(mode="json")
    snap["orb_pair"] = {"added": added, "errors": errors}
    if added:
        session.message = f"ORB pair on desk: {', '.join(added)}"
        snap["message"] = session.message
    return snap


@app.post("/api/session/strategies")
def add_strategy(body: AddStrategyBody) -> dict:
    try:
        state = get_session().add_strategy(
            body.strategy_id,
            symbol=body.symbol,
            timeframe=body.timeframe,
            quantity=body.quantity,
            starting_cash=body.starting_cash,
            params=body.params or None,
            enabled=body.enabled,
            asset_kind=body.asset_kind,
            instance_id=body.instance_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return get_session().snapshot().model_dump(mode="json")


@app.delete("/api/session/strategies/{instance_id}")
def remove_strategy(instance_id: str) -> dict:
    try:
        get_session().remove_strategy(instance_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return get_session().snapshot().model_dump(mode="json")


@app.post("/api/session/strategies/{instance_id}/enable")
def enable_strategy(instance_id: str, body: EnableBody) -> dict:
    try:
        get_session().set_enabled(instance_id, body.enabled)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return get_session().snapshot().model_dump(mode="json")


@app.post("/api/session/strategies/{instance_id}/start")
def start_strategy(instance_id: str, body: StartBody | None = None) -> dict:
    """Enable one strategy and ensure live paper session is running."""
    session = get_session()
    try:
        session.set_enabled(instance_id, True)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    poll = float((body.poll_seconds if body else None) or session.poll_seconds or 15)
    if not session.running:
        try:
            session.start_live(poll_seconds=poll)
        except RuntimeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    session.persist_desk()
    return session.snapshot().model_dump(mode="json")


@app.post("/api/session/strategies/{instance_id}/pause")
def pause_strategy(instance_id: str) -> dict:
    """Pause one strategy (others keep running if session is live)."""
    try:
        get_session().set_enabled(instance_id, False)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return get_session().snapshot().model_dump(mode="json")


@app.post("/api/session/start")
def start(body: StartBody) -> dict:
    session = get_session()
    try:
        if body.mode == "live":
            session.start_live(poll_seconds=body.poll_seconds)
        elif body.mode == "replay":
            session.start_replay(max_bars=body.max_bars)
        else:
            raise HTTPException(status_code=400, detail="mode must be replay or live")
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    session.persist_desk()
    return session.snapshot().model_dump(mode="json")


@app.post("/api/session/stop")
def stop() -> dict:
    session = get_session()
    session.stop()
    session.persist_desk()
    return session.snapshot().model_dump(mode="json")


@app.get("/api/reports/trades")
def reports_trades(
    strategy_id: str | None = None,
    symbol: str | None = None,
    status: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
) -> dict:
    trades = list_trades(strategy_id=strategy_id, symbol=symbol, status=status, limit=500)
    if from_date or to_date:
        filtered = []
        for t in trades:
            day = (t.get("exit_at") or t.get("entry_at") or "")[:10]
            if from_date and day and day < from_date:
                continue
            if to_date and day and day > to_date:
                continue
            filtered.append(t)
        trades = filtered
    return {"trades": trades, "count": len(trades)}


@app.get("/api/reports/selections")
def reports_selections(session_id: str | None = None) -> dict:
    rows = list_selections(session_id=session_id, limit=500)
    return {"selections": rows, "count": len(rows)}


@app.get("/api/reports/summary")
def reports_summary(
    from_date: str | None = None,
    to_date: str | None = None,
    strategy_id: str | None = None,
) -> dict:
    return report_summary(from_date=from_date, to_date=to_date, strategy_id=strategy_id)


@app.get("/api/reports/daily")
def reports_daily(from_date: str | None = None, to_date: str | None = None) -> dict:
    return daily_report(from_date=from_date, to_date=to_date)


@app.get("/api/reports/calendar")
def reports_calendar(
    year: int | None = None,
    month: int | None = None,
) -> dict:
    now = datetime.now()
    return calendar_pnl(year=year or now.year, month=month or now.month)


@app.get("/api/reports/export.csv")
def reports_export_csv(
    from_date: str | None = Query(None),
    to_date: str | None = Query(None),
    strategy_id: str | None = Query(None),
) -> Response:
    csv_text = trades_csv(from_date=from_date, to_date=to_date, strategy_id=strategy_id)
    filename = "paper_trades.csv"
    if from_date or to_date:
        filename = f"paper_trades_{from_date or 'start'}_{to_date or 'end'}.csv"
    return Response(
        content=csv_text,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
