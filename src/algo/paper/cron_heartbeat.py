from __future__ import annotations

"""Market-hours heartbeat for free Render keep-alive.

GitHub Actions ``schedule`` is best-effort and often skips or delays.
Any external ping (UptimeRobot / cron-job.org / Actions) that hits
``/api/cron/heartbeat`` will:

1. Keep the free instance awake
2. Auto-wake live paper on NSE weekdays 08:00–15:45 IST
3. Auto-sleep after 15:45 IST
4. Send pre-market / post-market Brevo digests **once per IST day**
   (so a late ping still delivers the email)
"""

from datetime import datetime, time
from typing import Any
from zoneinfo import ZoneInfo

from algo.paper.desk_store import _db_get, _db_put, _use_db_state

IST = ZoneInfo("Asia/Kolkata")
OPS_KEY = "paper_ops"

# NSE cash session window used for wake/sleep automation.
WAKE_FROM = time(8, 0)
WAKE_UNTIL = time(15, 45)
# Emails fire on first heartbeat after these clocks (once per day).
OPEN_REPORT_AFTER = time(8, 50)
CLOSE_REPORT_AFTER = time(15, 35)


def _ops_state() -> dict[str, Any]:
    if _use_db_state():
        data = _db_get(OPS_KEY)
        if isinstance(data, dict):
            return data
    return {}


def _save_ops(data: dict[str, Any]) -> None:
    if _use_db_state():
        try:
            _db_put(OPS_KEY, data)
        except Exception:
            pass


def run_heartbeat(*, poll_seconds: float = 15.0, send_email: bool = True) -> dict[str, Any]:
    """Idempotent keep-alive + wake/sleep + once-daily digests. Never raises."""
    now = datetime.now(tz=IST)
    clock = now.time().replace(tzinfo=None)
    weekday = now.weekday() < 5  # Mon–Fri
    today = now.date().isoformat()
    out: dict[str, Any] = {
        "ok": True,
        "service": "paper-desk",
        "ist": now.strftime("%Y-%m-%d %H:%M:%S IST"),
        "weekday": weekday,
        "actions": [],
    }

    # --- Session wake / sleep ---
    try:
        from algo.paper.session import get_session

        session = get_session()
        out["strategies"] = len(session.runners)
        out["running"] = bool(session.running)
        out["mode"] = session.mode

        if weekday and WAKE_FROM <= clock < WAKE_UNTIL:
            if not session.runners:
                out["actions"].append("no_strategies")
            elif session.running and session.mode == "live":
                out["actions"].append("already_live")
            else:
                try:
                    # Best-effort token refresh (same as /api/session/wake)
                    try:
                        from algo.config import get_settings
                        from algo.providers.dhan.auth import TokenRotator, ensure_fresh_token

                        ensure_fresh_token(get_settings(), force_renew=False)
                        TokenRotator.instance().start()
                        out["token_refresh"] = "ok"
                    except Exception as exc:
                        out["token_refresh"] = f"skipped:{exc}"[:120]
                    session.start_live(poll_seconds=poll_seconds)
                    session.persist_desk()
                    out["actions"].append("woke")
                    out["running"] = True
                    out["mode"] = "live"
                except Exception as exc:
                    out["actions"].append(f"wake_failed:{exc}"[:160])
                    out["ok"] = False
        elif weekday and clock >= WAKE_UNTIL:
            if session.running:
                try:
                    session.stop()
                    session.persist_desk()
                    out["actions"].append("slept")
                    out["running"] = False
                except Exception as exc:
                    out["actions"].append(f"sleep_failed:{exc}"[:160])
            else:
                out["actions"].append("already_stopped")
        else:
            out["actions"].append("off_hours_keepalive")
    except Exception as exc:
        out["session_error"] = str(exc)[:200]
        out["ok"] = False

    # --- Once-daily digests (tolerate late/missed GitHub cron) ---
    if send_email and weekday:
        ops = _ops_state()
        try:
            if clock >= OPEN_REPORT_AFTER and ops.get("last_open_report") != today:
                from algo.paper.health_report import run_market_report

                report = run_market_report("open", send=True)
                out["open_report"] = {
                    "email": report.get("email"),
                    "summary": (report.get("health") or {}).get("summary"),
                }
                ops["last_open_report"] = today
                ops["last_open_report_at"] = now.isoformat()
                _save_ops(ops)
                out["actions"].append("open_report_sent")
            if clock >= CLOSE_REPORT_AFTER and ops.get("last_close_report") != today:
                from algo.paper.health_report import run_market_report

                report = run_market_report("close", send=True)
                out["close_report"] = {
                    "email": report.get("email"),
                    "summary": (report.get("health") or {}).get("summary"),
                }
                ops["last_close_report"] = today
                ops["last_close_report_at"] = now.isoformat()
                _save_ops(ops)
                out["actions"].append("close_report_sent")
        except Exception as exc:
            out["report_error"] = str(exc)[:200]
            # Keep ok=true so external monitors don't flap on email issues

    out["message"] = ", ".join(out["actions"]) or "ok"
    return out
