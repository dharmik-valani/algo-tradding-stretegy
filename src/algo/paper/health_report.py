from __future__ import annotations

"""Deep health checks + morning/evening email digests for Paper Desk."""

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from algo.paper.email_report import report_email_to, send_email, smtp_configured

IST = ZoneInfo("Asia/Kolkata")


def build_deep_health(*, probe_ltp: bool = True) -> dict[str, Any]:
    """REST + WebSocket + session diagnostics. Safe for cron; never raises."""
    now = datetime.now(tz=IST)
    out: dict[str, Any] = {
        "ok": True,
        "service": "paper-desk",
        "ist": now.strftime("%Y-%m-%d %H:%M:%S IST"),
        "checks": {},
        "issues": [],
    }
    checks: dict[str, Any] = out["checks"]
    issues: list[str] = out["issues"]

    # --- Session / desk ---
    try:
        from algo.paper import session as session_mod

        sess = session_mod._SESSION
        ready = session_mod._SESSION_READY.is_set()
        checks["session_ready"] = ready
        if sess is None:
            checks["session"] = {"present": False, "booting": True}
            issues.append("Session still booting")
            out["ok"] = False
        else:
            strat_rows = []
            for key, runner in sess.runners.items():
                enabled = bool(getattr(runner, "enabled", True))
                sid = getattr(getattr(runner, "strategy", None), "id", None) or key
                name = getattr(getattr(runner, "strategy", None), "name", None) or sid
                last_sig = getattr(runner, "last_signal", None)
                strat_rows.append(
                    {
                        "instance_id": key,
                        "strategy_id": sid,
                        "name": name,
                        "enabled": enabled,
                        "last_signal": last_sig,
                    }
                )
            checks["session"] = {
                "present": True,
                "running": bool(sess.running),
                "mode": sess.mode,
                "strategies": len(sess.runners),
                "enabled": sum(1 for r in strat_rows if r["enabled"]),
                "message": (sess.message or "")[:240],
                "desk": strat_rows,
            }
            if not sess.runners:
                issues.append("No strategies on desk")
                out["ok"] = False
            elif not sess.running and now.weekday() < 5 and 9 <= now.hour < 15:
                issues.append("Live session not running during market hours")
                # Soft fail — wake cron may still start it
                out["ok"] = False
    except Exception as exc:
        checks["session"] = {"error": str(exc)[:200]}
        issues.append(f"Session check failed: {exc}")
        out["ok"] = False

    # --- Feed / WebSocket ---
    try:
        from algo.paper import session as session_mod

        sess = session_mod._SESSION
        quotes = getattr(sess, "_quotes", None) if sess else None
        if quotes is None:
            checks["feed"] = {"status": "no-quotes-yet", "ws": None}
            if sess and sess.running:
                issues.append("Live running but quote provider not attached")
                out["ok"] = False
        else:
            diag = quotes.diagnostics()
            checks["feed"] = diag
            ws = diag.get("ws") or {}
            if not ws.get("connected") and sess and sess.running:
                issues.append(f"WebSocket not connected ({ws.get('status') or ws.get('last_error') or 'unknown'})")
                # WS may reconnect; mark degraded not hard-fail if REST ok
                out["degraded"] = True
    except Exception as exc:
        checks["feed"] = {"error": str(exc)[:200]}
        issues.append(f"Feed check failed: {exc}")
        out["degraded"] = True

    # --- REST / live LTP probe (NIFTY) ---
    if probe_ltp:
        try:
            from algo.config import get_settings
            from algo.paper.quotes import INDEX_LTP_KEYS
            from algo.providers.dhan.client import DhanClient

            settings = get_settings()
            has_token = bool(settings.dhan_access_token and settings.dhan_client_id)
            checks["dhan_credentials"] = {"present": has_token}
            if not has_token:
                issues.append("DHAN_CLIENT_ID / DHAN_ACCESS_TOKEN missing")
                out["ok"] = False
            else:
                from algo.paper import session as session_mod

                sess = session_mod._SESSION
                q = getattr(sess, "_quotes", None) if sess else None
                price = None
                ts = None
                src = None
                if q is not None:
                    try:
                        price, ts, src = q.get_ltp(
                            "NIFTY", allow_rest=True, wait_ws_sec=1.0, max_stale_sec=600
                        )
                    except Exception:
                        price = None
                if price is None:
                    # Lightweight REST-only probe — does not open a second WebSocket.
                    from algo.paper.quotes import _extract_ltp
                    from algo.providers.dhan.auth import current_token

                    segment, sec_id = INDEX_LTP_KEYS["NIFTY"]
                    live_url = settings.yaml_config.get("dhan", {}).get(
                        "base_url", "https://api.dhan.co/v2"
                    )
                    token = current_token(settings) or settings.dhan_access_token
                    client = DhanClient(
                        client_id=settings.dhan_client_id,
                        access_token=token,
                        base_url=live_url,
                        timeout=getattr(settings, "timeout_seconds", 30.0) or 30.0,
                    )
                    try:
                        payload = client.post_json(
                            "/marketfeed/ltp", {segment: [int(sec_id)]}
                        )
                        price = _extract_ltp(payload, segment, sec_id)
                        src = "dhan-rest"
                        ts = datetime.now(tz=IST)
                    finally:
                        try:
                            client.close()
                        except Exception:
                            pass
                checks["rest_ltp"] = {
                    "symbol": "NIFTY",
                    "price": price,
                    "source": src,
                    "ts": ts.isoformat() if hasattr(ts, "isoformat") else str(ts),
                }
        except Exception as exc:
            checks["rest_ltp"] = {"error": str(exc)[:240]}
            issues.append(f"NIFTY LTP probe failed: {exc}")
            out["ok"] = False
            out["degraded"] = True

    # --- DB / reports ---
    try:
        from algo.paper.journal import report_summary

        summary = report_summary()
        checks["journal"] = {
            "trades": summary.get("trades"),
            "open": summary.get("open"),
            "closed": summary.get("closed"),
            "net_pnl": summary.get("net_pnl"),
        }
    except Exception as exc:
        checks["journal"] = {"error": str(exc)[:200]}
        issues.append(f"Journal/DB check failed: {exc}")
        out["degraded"] = True

    out["smtp_configured"] = smtp_configured()
    out["report_email_to"] = report_email_to()
    if issues and out.get("ok") and out.get("degraded"):
        # keep ok true for soft degradation
        pass
    out["summary"] = (
        "ALL CHECKS PASSED"
        if out["ok"] and not out.get("degraded")
        else ("DEGRADED — see issues" if out.get("degraded") and out["ok"] else "FAILED — see issues")
    )
    return out


def format_health_email(kind: str, health: dict[str, Any]) -> tuple[str, str, str]:
    """Return subject, text, html for open/close digest."""
    ist = health.get("ist") or datetime.now(tz=IST).strftime("%Y-%m-%d %H:%M:%S IST")
    label = "PRE-MARKET" if kind == "open" else "POST-MARKET" if kind == "close" else kind.upper()
    status = health.get("summary") or ("OK" if health.get("ok") else "ISSUES")
    subject = f"[Paper Desk] {label} {status} · {ist}"

    sess = (health.get("checks") or {}).get("session") or {}
    feed = (health.get("checks") or {}).get("feed") or {}
    ltp = (health.get("checks") or {}).get("rest_ltp") or {}
    journal = (health.get("checks") or {}).get("journal") or {}
    ws = feed.get("ws") if isinstance(feed, dict) else {}
    desk = sess.get("desk") or []

    lines = [
        f"Paper Desk {label} health report",
        f"Time: {ist}",
        f"Status: {status}",
        "",
        "=== RENDER / SESSION ===",
        f"Running: {sess.get('running')}  mode={sess.get('mode')}",
        f"Strategies on desk: {sess.get('strategies')} (enabled {sess.get('enabled')})",
        f"Message: {sess.get('message') or '—'}",
        "",
        "=== FEED ===",
        f"Feed: {feed.get('feed_status') if isinstance(feed, dict) else feed}",
        f"WebSocket connected: {(ws or {}).get('connected')}  status={(ws or {}).get('status')}",
        f"WS ticks cached: {(ws or {}).get('cache_ticks')}  wanted={(ws or {}).get('wanted')}",
        f"WS last error: {(ws or {}).get('last_error') or '—'}",
        f"REST cooling: {feed.get('rest_cooling') if isinstance(feed, dict) else None}",
        "",
        "=== API PROBE ===",
        f"NIFTY LTP: {ltp.get('price')}  source={ltp.get('source')}  err={ltp.get('error') or '—'}",
        f"Dhan creds present: {(health.get('checks') or {}).get('dhan_credentials', {}).get('present')}",
        "",
        "=== JOURNAL ===",
        f"Trades={journal.get('trades')} open={journal.get('open')} closed={journal.get('closed')} net={journal.get('net_pnl')}",
        "",
        "=== STRATEGIES ===",
    ]
    if desk:
        for row in desk:
            lines.append(
                f"- {row.get('name')} [{row.get('strategy_id')}] "
                f"enabled={row.get('enabled')} signal={row.get('last_signal')}"
            )
    else:
        lines.append("(none)")

    issues = health.get("issues") or []
    lines.extend(["", "=== ISSUES ==="])
    if issues:
        lines.extend(f"- {i}" for i in issues)
    else:
        lines.append("(none)")

    lines.extend(
        [
            "",
            "This is an automated digest from GitHub Actions cron (IST).",
            "Keep-alive continues every ~10 minutes during market hours.",
        ]
    )
    text = "\n".join(lines)

    color = "#1a7a4c" if health.get("ok") and not health.get("degraded") else "#b33a2a"
    html = f"""
    <div style="font-family:system-ui,sans-serif;max-width:640px">
      <h2 style="color:{color}">Paper Desk · {label}</h2>
      <p><b>{status}</b><br/>{ist}</p>
      <h3>Session</h3>
      <ul>
        <li>Running: {sess.get('running')} ({sess.get('mode')})</li>
        <li>Strategies: {sess.get('strategies')} enabled={sess.get('enabled')}</li>
        <li>{sess.get('message') or ''}</li>
      </ul>
      <h3>Feed / APIs</h3>
      <ul>
        <li>Feed: {feed.get('feed_status') if isinstance(feed, dict) else feed}</li>
        <li>WebSocket: connected={(ws or {}).get('connected')} status={(ws or {}).get('status')}</li>
        <li>NIFTY LTP: {ltp.get('price')} ({ltp.get('source') or ltp.get('error') or '—'})</li>
      </ul>
      <h3>Journal</h3>
      <p>trades={journal.get('trades')} open={journal.get('open')} closed={journal.get('closed')} net={journal.get('net_pnl')}</p>
      <h3>Issues</h3>
      <ul>{''.join(f'<li>{i}</li>' for i in issues) or '<li>none</li>'}</ul>
    </div>
    """
    return subject, text, html


def run_market_report(kind: str, *, send: bool = True) -> dict[str, Any]:
    """kind: open | close | health"""
    health = build_deep_health(probe_ltp=True)
    subject, text, html = format_health_email(kind, health)
    mail: dict[str, Any] = {"skipped": True, "reason": "send=false"}
    if send:
        mail = send_email(subject=subject, body_text=text, body_html=html)
    return {
        "kind": kind,
        "health": health,
        "email": mail,
        "subject": subject,
    }
