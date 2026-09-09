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
    full_report_csv,
    full_report_html,
    list_selections,
    list_trades,
    report_summary,
    trades_csv,
)
from algo.paper.session import get_session, reload_session_from_disk, reset_session

STATIC = Path(__file__).parent / "static"

app = FastAPI(title="Algo Paper Desk", version="0.1.0")
app.mount("/static", StaticFiles(directory=STATIC), name="static")


def _require_cron_secret(
    x_cron_secret: str | None,
    cron_secret_query: str | None = None,
) -> None:
    """If CRON_SECRET is set on the service, cron callers must send matching header or query."""
    expected = (os.environ.get("CRON_SECRET") or "").strip()
    if not expected:
        return
    got = (x_cron_secret or "").strip() or (cron_secret_query or "").strip()
    if got != expected:
        raise HTTPException(status_code=401, detail="invalid cron secret")


class AddStrategyBody(BaseModel):
    strategy_id: str
    symbol: str = "NIFTY"
    timeframe: str = "5m"
    quantity: int = 1
    starting_cash: float = 500_000.0
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


@app.get("/api/health/deep")
def health_deep(
    x_cron_secret: str | None = Header(default=None, alias="X-Cron-Secret"),
) -> dict:
    """Detailed pre-market / ops check: session, Dhan REST LTP, WebSocket, journal."""
    _require_cron_secret(x_cron_secret)
    from algo.paper.health_report import build_deep_health

    return build_deep_health(probe_ltp=True)


@app.post("/api/report/open")
def report_open(
    send_email: bool = Query(True, description="Send email digest when SMTP is configured"),
    x_cron_secret: str | None = Header(default=None, alias="X-Cron-Secret"),
) -> dict:
    """Pre-market digest: deep health + email to REPORT_EMAIL_TO (default Gmail)."""
    _require_cron_secret(x_cron_secret)
    from algo.paper.health_report import run_market_report

    return run_market_report("open", send=send_email)


@app.post("/api/report/close")
def report_close(
    send_email: bool = Query(True, description="Send email digest when SMTP is configured"),
    x_cron_secret: str | None = Header(default=None, alias="X-Cron-Secret"),
) -> dict:
    """Post-market digest: deep health + email, then caller may sleep the session."""
    _require_cron_secret(x_cron_secret)
    from algo.paper.health_report import run_market_report

    return run_market_report("close", send=send_email)


@app.api_route("/api/cron/heartbeat", methods=["GET", "POST"])
def cron_heartbeat(
    poll_seconds: float = Query(15.0, ge=5.0, le=120.0),
    send_email: bool = Query(True),
    cron_secret: str | None = Query(default=None),
    x_cron_secret: str | None = Header(default=None, alias="X-Cron-Secret"),
) -> dict:
    """External keep-alive + auto wake/sleep + once-daily digests (IST).

    Prefer this over bare ``/api/health`` for UptimeRobot / cron-job.org.
    Auth: ``X-Cron-Secret`` header **or** ``?cron_secret=`` query (for GET monitors).
    """
    _require_cron_secret(x_cron_secret, cron_secret)
    from algo.paper.cron_heartbeat import run_heartbeat

    return run_heartbeat(poll_seconds=poll_seconds, send_email=send_email)


@app.post("/api/session/reload")
def session_reload() -> dict:
    """Reload desk + runtime from shared database (Supabase) / files.

    Use when laptop UI drifted from Render: both share DATABASE_URL; this
    drops in-memory strategies and re-reads the Postgres ``paper_desk`` blob.
    """
    session = reload_session_from_disk()
    snap = session.snapshot().model_dump(mode="json")
    return {
        "ok": True,
        "strategies": len(snap.get("strategies") or []),
        "running": bool(session.running),
        "message": snap.get("message") or session.message,
        "desk": [
            {
                "instance_id": s.get("instance_id"),
                "name": s.get("name"),
                "enabled": s.get("enabled"),
            }
            for s in (snap.get("strategies") or [])
        ],
    }


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
    renew_info: dict[str, Any] = {}
    try:
        from algo.config import get_settings
        from algo.providers.dhan.auth import TokenRotator, ensure_fresh_token

        ensure_fresh_token(get_settings(), force_renew=False)
        TokenRotator.instance().start()
        renew_info = {"token_refresh": "ok"}
    except Exception as exc:
        renew_info = {"token_refresh": "skipped", "token_error": str(exc)}
    session = get_session()
    if session.running and session.mode == "live":
        return {
            "ok": True,
            "already_running": True,
            "running": True,
            "strategies": len(session.runners),
            "message": session.message,
            **renew_info,
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
        **renew_info,
    }


@app.post("/api/cron/dhan-renew")
def cron_dhan_renew(
    force: bool = Query(False, description="Force RenewToken even if not near expiry"),
    x_cron_secret: str | None = Header(default=None, alias="X-Cron-Secret"),
) -> dict:
    """Server cron: refresh SELF token via RenewToken and persist to Postgres."""
    _require_cron_secret(x_cron_secret)
    from algo.config import get_settings
    from algo.providers.dhan.auth import TokenRotator, dhan_connection_status, ensure_fresh_token
    from algo.providers.dhan.token_store import resolve_access_token

    settings = get_settings()
    before = resolve_access_token(settings.dhan_access_token)
    try:
        after = ensure_fresh_token(settings, force_renew=force)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    get_settings.cache_clear()
    TokenRotator.instance().start()
    status = dhan_connection_status(get_settings())
    return {
        "ok": True,
        "rotated": bool(after and after != before),
        "storage": status.get("storage"),
        "consumer_type": status.get("consumer_type"),
        "seconds_left": status.get("seconds_left"),
        "renewable": status.get("renewable"),
        "profile_ok": status.get("ok"),
        "error": status.get("error"),
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
    """Replace desk/runtime from a local export. Requires PAPER_SYNC_TOKEN.

    Writes into shared Postgres (``paper_app_state``) when DATABASE_URL is set,
    and mirrors files for offline inspection.
    """
    import base64
    import json
    import os
    from pathlib import Path

    from algo.config import ROOT
    from algo.paper.desk_store import save_desk, save_runtime

    expected = (os.environ.get("PAPER_SYNC_TOKEN") or "").strip()
    if not expected or body.sync_token.strip() != expected:
        raise HTTPException(status_code=401, detail="invalid sync token")

    data_dir = ROOT / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    written: list[str] = []

    def _decode(b64: str | None) -> bytes | None:
        if not b64:
            return None
        return base64.b64decode(b64)

    try:
        desk_raw = _decode(body.desk_json_b64)
        runtime_raw = _decode(body.runtime_json_b64)
        sqlite_raw = _decode(body.sqlite_b64)
        if desk_raw is not None:
            payload = json.loads(desk_raw.decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("desk JSON must be an object")
            save_desk(payload)  # Postgres SSoT + file mirror
            written.append("paper_desk")
        if runtime_raw is not None:
            payload = json.loads(runtime_raw.decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("runtime JSON must be an object")
            save_runtime(payload)
            written.append("paper_runtime")
        if sqlite_raw is not None:
            # Only useful for SQLite local; ignored on next boot if Postgres is configured.
            path = data_dir / "algo.db"
            path.write_bytes(sqlite_raw)
            written.append("algo.db")
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


class DhanCredsBody(BaseModel):
    pin: str | None = None
    totp_secret: str | None = None


@app.get("/api/dhan/status")
def dhan_status() -> dict:
    from algo.config import get_settings
    from algo.providers.dhan.auth import TokenRotator, dhan_connection_status

    # Keep RenewToken / TOTP remint cycling while the desk UI is open.
    TokenRotator.instance().start()
    return dhan_connection_status(get_settings())


@app.post("/api/dhan/token")
def dhan_set_token(body: DhanTokenBody) -> dict:
    from algo.config import get_settings
    from algo.providers.dhan.auth import TokenRotator, dhan_connection_status, fetch_profile
    from algo.providers.dhan.env_sync import upsert_dotenv
    from algo.providers.dhan.token_store import (
        jwt_claims,
        jwt_consumer_type,
        jwt_expiry,
        save_token,
    )
    import time

    settings = get_settings()
    token = body.access_token.strip()
    if not token:
        raise HTTPException(status_code=400, detail="access_token required")
    client_id = (body.client_id or settings.dhan_client_id or "").strip()
    claims_cid = str(jwt_claims(token).get("dhanClientId") or "").strip()
    client_id = client_id or claims_cid
    if not client_id:
        raise HTTPException(status_code=400, detail="DHAN_CLIENT_ID missing in .env")

    profile: dict | None = None
    profile_error: str | None = None
    try:
        profile = fetch_profile(client_id=client_id, access_token=token)
    except Exception as exc:
        profile_error = str(exc)
        exp = jwt_expiry(token)
        consumer = jwt_consumer_type(token)
        # Allow save when JWT looks usable — Dhan sometimes returns DH-906 overnight.
        if not exp or exp.timestamp() <= time.time():
            raise HTTPException(status_code=400, detail=f"Token rejected by Dhan: {exc}") from exc
        if consumer not in {"", "SELF", "PARTNER"}:
            raise HTTPException(status_code=400, detail=f"Token rejected by Dhan: {exc}") from exc

    cid = str((profile or {}).get("dhanClientId") or client_id)
    save_token(
        access_token=token,
        client_id=cid,
        expiry_time=str((profile or {}).get("tokenValidity") or "") or None,
        source="ui" if profile else "ui-unverified",
    )
    settings.dhan_access_token = token
    settings.dhan_client_id = cid
    try:
        upsert_dotenv({"DHAN_ACCESS_TOKEN": token, "DHAN_CLIENT_ID": cid})
    except Exception:
        pass
    get_settings.cache_clear()
    TokenRotator.instance().start()
    status = dhan_connection_status(get_settings())
    if profile_error:
        status["warning"] = (
            f"Saved to DB anyway (storage={status.get('storage')}), but Dhan profile failed: {profile_error}"
        )
    return status


@app.post("/api/dhan/credentials")
def dhan_set_credentials(body: DhanCredsBody) -> dict:
    """Store PIN + TOTP secret for generateAccessToken auto-remint."""
    from algo.config import get_settings
    from algo.providers.dhan.auth import TokenRotator, dhan_connection_status, totp_configured
    from algo.providers.dhan.env_sync import upsert_dotenv

    settings = get_settings()
    updates: dict[str, str] = {}
    if body.pin is not None:
        pin = body.pin.strip()
        if pin and (not pin.isdigit() or len(pin) != 6):
            raise HTTPException(status_code=400, detail="PIN must be a 6-digit numeric code")
        updates["DHAN_PIN"] = pin
        settings.dhan_pin = pin
    if body.totp_secret is not None:
        secret = body.totp_secret.replace(" ", "").strip().upper()
        updates["DHAN_TOTP_SECRET"] = secret
        settings.dhan_totp_secret = secret
    if not updates:
        raise HTTPException(status_code=400, detail="Provide pin and/or totp_secret")
    try:
        upsert_dotenv(updates)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Could not write .env: {exc}") from exc
    get_settings.cache_clear()
    TokenRotator.instance().start()
    status = dhan_connection_status(get_settings())
    status["saved_keys"] = list(updates.keys())
    status["totp_configured"] = totp_configured(get_settings())
    return status


@app.post("/api/dhan/renew")
def dhan_renew() -> dict:
    """Refresh when near expiry (safe). Early RenewToken is refused — it burns the JWT."""
    from algo.config import get_settings
    from algo.providers.dhan.auth import TokenRotator, dhan_connection_status, ensure_fresh_token

    settings = get_settings()
    try:
        # force_renew=True still refuses if >12h remain (see ensure_fresh_token).
        ensure_fresh_token(settings, force_renew=True)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    get_settings.cache_clear()
    TokenRotator.instance().start()
    return dhan_connection_status(get_settings())


@app.post("/api/dhan/generate")
def dhan_generate() -> dict:
    """Always mint via PIN+TOTP (generateAccessToken), ignoring RenewToken."""
    from algo.config import get_settings
    from algo.providers.dhan.auth import (
        TokenRotator,
        apply_auth_payload,
        dhan_connection_status,
        regenerate_via_totp,
    )

    settings = get_settings()
    try:
        payload = regenerate_via_totp(settings)
        apply_auth_payload(settings, payload, source="totp")
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    get_settings.cache_clear()
    TokenRotator.instance().start()
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


@app.get("/api/analytics/board")
def analytics_board(
    from_date: str | None = None,
    to_date: str | None = None,
    strategy_id: str | None = None,
) -> dict:
    """Journal-backed analytics for day/week/month + strategy filters."""
    from algo.paper.journal import daily_report, report_summary

    summary = report_summary(
        from_date=from_date or None,
        to_date=to_date or None,
        strategy_id=strategy_id or None,
    )
    daily = daily_report(
        from_date=from_date or None,
        to_date=to_date or None,
        strategy_id=strategy_id or None,
    )
    # Ranked list for the date range (all strategies) so the dropdown stays top→down
    # even while a single strategy filter is active.
    strategy_rank = (
        summary.get("by_strategy")
        if not strategy_id
        else report_summary(
            from_date=from_date or None,
            to_date=to_date or None,
            strategy_id=None,
        ).get("by_strategy")
    )
    session = get_session()
    snap = session.snapshot()
    strategies = list(snap.strategies or [])
    if strategy_id:
        strategies = [s for s in strategies if s.strategy_id == strategy_id]
    invested = round(sum(float(s.starting_cash or 0) for s in strategies), 2)
    generated = round(invested + float(summary.get("net_pnl") or 0), 2)
    live = session.analytics()
    overall = live.get("overall") or {}
    return {
        "ok": True,
        "summary": summary,
        "daily": daily,
        "strategy_rank": strategy_rank or [],
        "capital": {
            "invested": invested,
            "generated": generated,
            "net_pnl": float(summary.get("net_pnl") or 0),
            "strategies_in_filter": len(strategies),
        },
        "live": {
            "running": live.get("running"),
            "closed_trades": overall.get("trades") or overall.get("closed_trades"),
            "realized_pnl": overall.get("net_pnl") or overall.get("realized_pnl"),
            "win_rate": overall.get("win_rate"),
            "profit_factor": overall.get("profit_factor"),
            "desk_strategies": len(live.get("strategies") or []),
        },
        "from_date": from_date,
        "to_date": to_date,
        "strategy_id": strategy_id,
    }


@app.get("/api/feed")
def feed_diagnostics() -> dict:
    """Live feed + rate-limit diagnostics (why a 429 happened)."""
    from algo.paper import session as session_mod

    sess = session_mod._SESSION
    if sess is None or sess._quotes is None:
        return {"ok": True, "booting": sess is None, "running": bool(sess and sess.running)}
    out = sess._quotes.diagnostics()
    out["ok"] = True
    out["running"] = bool(sess.running)
    out["poll_seconds_base"] = sess.poll_seconds
    out["enabled_strategies"] = sum(1 for r in sess.runners.values() if r.enabled)
    return out


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
                starting_cash=500_000.0,
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
def reports_selections(
    session_id: str | None = None,
    strategy_id: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
) -> dict:
    rows = list_selections(
        session_id=session_id,
        strategy_id=strategy_id or None,
        from_date=from_date or None,
        to_date=to_date or None,
        limit=500,
    )
    return {"selections": rows, "count": len(rows)}


@app.get("/api/reports/summary")
def reports_summary(
    from_date: str | None = None,
    to_date: str | None = None,
    strategy_id: str | None = None,
) -> dict:
    return report_summary(from_date=from_date, to_date=to_date, strategy_id=strategy_id)


@app.get("/api/reports/daily")
def reports_daily(
    from_date: str | None = None,
    to_date: str | None = None,
    strategy_id: str | None = None,
) -> dict:
    return daily_report(from_date=from_date, to_date=to_date, strategy_id=strategy_id)


@app.get("/api/reports/calendar")
def reports_calendar(
    year: int | None = None,
    month: int | None = None,
    strategy_id: str | None = None,
) -> dict:
    now = datetime.now()
    return calendar_pnl(
        year=year or now.year,
        month=month or now.month,
        strategy_id=strategy_id or None,
    )


@app.get("/api/reports/export.csv")
def reports_export_csv(
    from_date: str | None = Query(None),
    to_date: str | None = Query(None),
    strategy_id: str | None = Query(None),
    kind: str | None = Query("trades"),
) -> Response:
    """CSV export. kind=trades|strategy|daily|basket|full."""
    from algo.paper.journal import daily_report_csv, selections_csv, strategy_report_csv

    k = (kind or "trades").lower().strip()
    if k in {"full", "all"}:
        csv_text = full_report_csv(from_date=from_date, to_date=to_date, strategy_id=strategy_id)
        filename = "paper_report_full.csv"
    elif k in {"strategy", "strategies"}:
        csv_text = strategy_report_csv(from_date=from_date, to_date=to_date, strategy_id=strategy_id)
        filename = "paper_strategy_report.csv"
    elif k in {"daily", "day"}:
        csv_text = daily_report_csv(from_date=from_date, to_date=to_date, strategy_id=strategy_id)
        filename = "paper_daily_report.csv"
    elif k in {"basket", "selections", "selection"}:
        csv_text = selections_csv(from_date=from_date, to_date=to_date, strategy_id=strategy_id)
        filename = "paper_basket_selections.csv"
    else:
        csv_text = trades_csv(from_date=from_date, to_date=to_date, strategy_id=strategy_id)
        filename = "paper_trades.csv"
    if from_date or to_date:
        stem = filename.replace(".csv", "")
        filename = f"{stem}_{from_date or 'start'}_{to_date or 'end'}.csv"
    return Response(
        content=csv_text,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/reports/export.pdf")
def reports_export_pdf(
    from_date: str | None = Query(None),
    to_date: str | None = Query(None),
    strategy_id: str | None = Query(None),
) -> Response:
    """Printable HTML report — open and use Print → Save as PDF."""
    html = full_report_html(from_date=from_date, to_date=to_date, strategy_id=strategy_id)
    return Response(content=html, media_type="text/html; charset=utf-8")
