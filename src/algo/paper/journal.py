from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from typing import Any

from algo.config import Settings, get_settings
from algo.storage.db import get_engine, session_scope
from algo.storage.models import (
    PaperFillJournalRow,
    PaperSelectionRow,
    PaperSessionRow,
    PaperTradeRow,
)

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _sync_serial_pk(db, *, table: str, seq: str) -> None:
    """Keep Postgres serial sequences ahead of MAX(id) (SQLite→PG drift)."""
    from sqlalchemy import text

    try:
        mx = int(db.execute(text(f"select coalesce(max(id), 0) from {table}")).scalar() or 0)
        db.execute(text("select setval(:s, :m, true)"), {"s": seq, "m": max(mx, 1)})
    except Exception as exc:
        logger.warning("serial sync failed for %s/%s: %s", table, seq, exc)


def ensure_paper_tables(settings: Settings | None = None) -> None:
    get_engine(settings or get_settings())


def start_journal_session(*, mode: str, message: str = "") -> str:
    ensure_paper_tables()
    sid = str(uuid.uuid4())
    with session_scope() as db:
        db.add(
            PaperSessionRow(
                id=sid,
                mode=mode,
                started_at=_utcnow(),
                message=message or None,
            )
        )
    return sid


def end_journal_session(session_id: str | None, message: str = "") -> None:
    if not session_id:
        return
    with session_scope() as db:
        row = db.get(PaperSessionRow, session_id)
        if row is None:
            return
        row.ended_at = _utcnow()
        if message:
            row.message = message


def record_selections(
    *,
    session_id: str,
    strategy_id: str,
    instance_id: str,
    rows: list[dict[str, Any]],
) -> None:
    if not session_id or not rows:
        return
    ensure_paper_tables()
    now = _utcnow()
    try:
        with session_scope() as db:
            _sync_serial_pk(db, table="paper_selections", seq="paper_selections_id_seq")
            for r in rows:
                db.add(
                    PaperSelectionRow(
                        session_id=session_id,
                        strategy_id=strategy_id,
                        instance_id=instance_id,
                        symbol=str(r.get("symbol", "")).upper(),
                        mode=str(r.get("mode", "")),
                        pct_change=r.get("pct_change"),
                        open_px=r.get("open"),
                        high_px=r.get("high"),
                        low_px=r.get("low"),
                        close_px=r.get("close"),
                        prev_close=r.get("prev_close"),
                        meta_json=json.dumps(r),
                        selected_at=now,
                    )
                )
    except Exception as exc:
        # Never block live trading if journal PK/sequence drifts.
        logger.warning("record_selections failed (%s/%s): %s", strategy_id, instance_id, exc)


def open_trade(
    *,
    session_id: str,
    strategy_id: str,
    instance_id: str,
    symbol: str,
    side: str,
    quantity: int,
    entry_price: float,
    stop_price: float | None,
    target_price: float | None,
    entry_at: datetime,
    reason: str = "",
    meta: dict[str, Any] | None = None,
    fee: float = 0.0,
) -> str:
    ensure_paper_tables()
    trade_id = str(uuid.uuid4())
    try:
        with session_scope() as db:
            _sync_serial_pk(db, table="paper_fill_journal", seq="paper_fill_journal_id_seq")
            db.add(
                PaperTradeRow(
                    id=trade_id,
                    session_id=session_id,
                    strategy_id=strategy_id,
                    instance_id=instance_id,
                    symbol=symbol.upper(),
                    side=side.upper(),
                    status="open",
                    quantity=quantity,
                    entry_price=entry_price,
                    stop_price=stop_price,
                    target_price=target_price,
                    entry_at=entry_at,
                    fees=fee,
                    meta_json=json.dumps(meta or {}),
                )
            )
            db.add(
                PaperFillJournalRow(
                    trade_id=trade_id,
                    session_id=session_id,
                    strategy_id=strategy_id,
                    instance_id=instance_id,
                    symbol=symbol.upper(),
                    side="BUY" if side.upper() == "LONG" else "SELL",
                    quantity=quantity,
                    price=entry_price,
                    fee=fee,
                    reason=reason or "entry",
                    filled_at=entry_at,
                    meta_json=json.dumps(meta or {}),
                )
            )
    except Exception as exc:
        logger.warning("open_trade journal failed (%s %s): %s", strategy_id, symbol, exc)
    return trade_id


def close_trade(
    *,
    trade_id: str | None,
    session_id: str,
    strategy_id: str,
    instance_id: str,
    symbol: str,
    side: str,
    quantity: int,
    exit_price: float,
    exit_at: datetime,
    realized_pnl: float,
    reason: str = "",
    fee: float = 0.0,
    meta: dict[str, Any] | None = None,
) -> None:
    if not trade_id:
        return
    ensure_paper_tables()
    try:
        with session_scope() as db:
            _sync_serial_pk(db, table="paper_fill_journal", seq="paper_fill_journal_id_seq")
            row = db.get(PaperTradeRow, trade_id)
            if row is not None:
                row.status = "closed"
                row.exit_price = exit_price
                row.exit_at = exit_at
                row.realized_pnl = realized_pnl
                row.exit_reason = reason
                row.fees = float(row.fees or 0) + fee
            db.add(
                PaperFillJournalRow(
                    trade_id=trade_id,
                    session_id=session_id,
                    strategy_id=strategy_id,
                    instance_id=instance_id,
                    symbol=symbol.upper(),
                    side="SELL" if side.upper() == "LONG" else "BUY",
                    quantity=quantity,
                    price=exit_price,
                    fee=fee,
                    reason=reason or "exit",
                    filled_at=exit_at,
                    meta_json=json.dumps(meta or {}),
                )
            )
    except Exception as exc:
        logger.warning("close_trade journal failed (%s %s): %s", strategy_id, symbol, exc)


def list_trades(
    *,
    strategy_id: str | None = None,
    symbol: str | None = None,
    status: str | None = None,
    instance_id: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    ensure_paper_tables()
    with session_scope() as db:
        q = db.query(PaperTradeRow).order_by(PaperTradeRow.entry_at.desc())
        if strategy_id:
            q = q.filter(PaperTradeRow.strategy_id == strategy_id)
        if symbol:
            q = q.filter(PaperTradeRow.symbol == symbol.upper())
        if status:
            q = q.filter(PaperTradeRow.status == status)
        if instance_id:
            q = q.filter(PaperTradeRow.instance_id == instance_id)
        rows = q.limit(limit).all()
        return [_trade_dict(r) for r in rows]


def list_open_trades(
    *,
    instance_id: str | None = None,
    strategy_id: str | None = None,
    limit: int = 500,
) -> list[dict[str, Any]]:
    """Open paper trades still marked open in the journal (survive process death)."""
    return list_trades(
        strategy_id=strategy_id,
        instance_id=instance_id,
        status="open",
        limit=limit,
    )


def list_selections(*, session_id: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
    ensure_paper_tables()
    with session_scope() as db:
        q = db.query(PaperSelectionRow).order_by(PaperSelectionRow.selected_at.desc())
        if session_id:
            q = q.filter(PaperSelectionRow.session_id == session_id)
        rows = q.limit(limit).all()
        return [
            {
                "id": r.id,
                "session_id": r.session_id,
                "strategy_id": r.strategy_id,
                "instance_id": r.instance_id,
                "symbol": r.symbol,
                "mode": r.mode,
                "pct_change": r.pct_change,
                "open": r.open_px,
                "high": r.high_px,
                "low": r.low_px,
                "close": r.close_px,
                "prev_close": r.prev_close,
                "selected_at": r.selected_at.isoformat() if r.selected_at else None,
            }
            for r in rows
        ]


def report_summary(
    *,
    from_date: str | None = None,
    to_date: str | None = None,
    strategy_id: str | None = None,
) -> dict[str, Any]:
    ensure_paper_tables()
    trades = list_trades(strategy_id=strategy_id, limit=5000)
    if from_date or to_date:
        filtered = []
        for t in trades:
            day = _trade_day(t)
            if not day:
                continue
            if from_date and day < from_date:
                continue
            if to_date and day > to_date:
                continue
            filtered.append(t)
        trades = filtered
    closed = [t for t in trades if t["status"] == "closed"]
    pnls = [float(t["realized_pnl"] or 0) for t in closed]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    by_strategy: dict[str, list[float]] = {}
    for t in closed:
        by_strategy.setdefault(t["strategy_id"], []).append(float(t["realized_pnl"] or 0))
    strat_rows = []
    for sid, vals in by_strategy.items():
        w = [v for v in vals if v > 0]
        l = [v for v in vals if v < 0]
        strat_rows.append(
            {
                "strategy_id": sid,
                "trades": len(vals),
                "wins": len(w),
                "losses": len(l),
                "win_rate": round(len(w) / len(vals) * 100, 2) if vals else None,
                "avg_win": round(sum(w) / len(w), 2) if w else None,
                "avg_loss": round(sum(l) / len(l), 2) if l else None,
                "net_pnl": round(sum(vals), 2),
            }
        )
    return {
        "trades": len(trades),
        "open": sum(1 for t in trades if t["status"] == "open"),
        "closed": len(closed),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / len(closed) * 100, 2) if closed else None,
        "avg_win": round(sum(wins) / len(wins), 2) if wins else None,
        "avg_loss": round(sum(losses) / len(losses), 2) if losses else None,
        "net_pnl": round(sum(pnls), 2) if pnls else 0.0,
        "by_strategy": strat_rows,
        "from_date": from_date,
        "to_date": to_date,
    }


IST = ZoneInfo("Asia/Kolkata")


def _as_ist(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(IST)


def _fmt_ist(dt: datetime | None) -> str:
    local = _as_ist(dt)
    if local is None:
        return ""
    return local.strftime("%Y-%m-%d %H:%M:%S")


def _parse_iso_dt(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        text = str(raw).strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _trade_day(t: dict[str, Any]) -> str | None:
    """Prefer exit day for closed PnL; else entry day — always Asia/Kolkata."""
    raw = t.get("exit_at") or t.get("entry_at")
    dt = _parse_iso_dt(raw) if isinstance(raw, str) else None
    if dt is None and hasattr(raw, "tzinfo"):
        dt = raw  # type: ignore[assignment]
    local = _as_ist(dt) if isinstance(dt, datetime) else None
    if local is None:
        return str(raw)[:10] if raw else None
    return local.strftime("%Y-%m-%d")


def daily_report(
    *,
    from_date: str | None = None,
    to_date: str | None = None,
    strategy_id: str | None = None,
) -> dict[str, Any]:
    trades = list_trades(strategy_id=strategy_id, limit=5000)
    by_day: dict[str, dict[str, Any]] = {}
    for t in trades:
        day = _trade_day(t)
        if not day:
            continue
        if from_date and day < from_date:
            continue
        if to_date and day > to_date:
            continue
        bucket = by_day.setdefault(
            day,
            {"date": day, "trades": 0, "closed": 0, "open": 0, "wins": 0, "losses": 0, "net_pnl": 0.0, "symbols": set()},
        )
        bucket["trades"] += 1
        bucket["symbols"].add(t["symbol"])
        if t["status"] == "open":
            bucket["open"] += 1
            continue
        bucket["closed"] += 1
        pnl = float(t.get("realized_pnl") or 0)
        bucket["net_pnl"] += pnl
        if pnl > 0:
            bucket["wins"] += 1
        elif pnl < 0:
            bucket["losses"] += 1
    rows = []
    for day in sorted(by_day.keys(), reverse=True):
        b = by_day[day]
        closed = b["closed"]
        rows.append(
            {
                "date": day,
                "trades": b["trades"],
                "closed": closed,
                "open": b["open"],
                "wins": b["wins"],
                "losses": b["losses"],
                "win_rate": round(b["wins"] / closed * 100, 2) if closed else None,
                "net_pnl": round(b["net_pnl"], 2),
                "symbols": sorted(b["symbols"]),
            }
        )
    return {"days": rows, "count": len(rows)}


def calendar_pnl(*, year: int, month: int) -> dict[str, Any]:
    """Month grid of daily net PnL for closed trades."""
    from calendar import monthrange

    prefix = f"{year:04d}-{month:02d}"
    daily = daily_report(from_date=f"{prefix}-01", to_date=f"{prefix}-{monthrange(year, month)[1]:02d}")
    by = {d["date"]: d["net_pnl"] for d in daily["days"]}
    first_weekday, n_days = monthrange(year, month)  # Mon=0
    cells = []
    # Pad leading blanks (convert to Sun=0 style optional — use Mon-start)
    for _ in range(first_weekday):
        cells.append({"date": None, "pnl": None})
    for day in range(1, n_days + 1):
        d = f"{prefix}-{day:02d}"
        cells.append({"date": d, "pnl": by.get(d)})
    return {
        "year": year,
        "month": month,
        "cells": cells,
        "month_pnl": round(sum(v for v in by.values()), 2),
        "days_traded": len(by),
    }


def trades_csv(
    *,
    from_date: str | None = None,
    to_date: str | None = None,
    strategy_id: str | None = None,
) -> str:
    import csv
    import io

    trades = list_trades(strategy_id=strategy_id, limit=5000)
    out = io.StringIO()
    fields = [
        "trade_day",
        "id",
        "strategy_id",
        "instance_id",
        "symbol",
        "side",
        "status",
        "quantity",
        "entry_price",
        "exit_price",
        "stop_price",
        "target_price",
        "entry_at",
        "exit_at",
        "realized_pnl",
        "fees",
        "exit_reason",
    ]
    w = csv.DictWriter(out, fieldnames=fields, extrasaction="ignore")
    w.writeheader()
    for t in trades:
        day = _trade_day(t)
        if from_date and day and day < from_date:
            continue
        if to_date and day and day > to_date:
            continue
        row = dict(t)
        row["trade_day"] = day or ""
        row["entry_at"] = _fmt_ist(_parse_iso_dt(t.get("entry_at"))) or (t.get("entry_at") or "")
        row["exit_at"] = _fmt_ist(_parse_iso_dt(t.get("exit_at"))) or (t.get("exit_at") or "")
        w.writerow(row)
    return out.getvalue()


def full_report_csv(
    *,
    from_date: str | None = None,
    to_date: str | None = None,
    strategy_id: str | None = None,
) -> str:
    """Multi-section CSV: summary, by strategy, by day, then trade lines."""
    import csv
    import io

    summary = report_summary(from_date=from_date, to_date=to_date, strategy_id=strategy_id)
    daily = daily_report(from_date=from_date, to_date=to_date, strategy_id=strategy_id)
    trades = list_trades(strategy_id=strategy_id, limit=5000)
    filtered = []
    for t in trades:
        day = _trade_day(t)
        if from_date and day and day < from_date:
            continue
        if to_date and day and day > to_date:
            continue
        filtered.append(t)

    out = io.StringIO()
    w = csv.writer(out)

    w.writerow(["PAPER DESK REPORT"])
    w.writerow(["Generated (IST)", datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")])
    w.writerow(["From", from_date or "all"])
    w.writerow(["To", to_date or "all"])
    w.writerow(["Strategy filter", strategy_id or "all"])
    w.writerow([])

    w.writerow(["SUMMARY"])
    w.writerow(["Trades", "Open", "Closed", "Wins", "Losses", "Win rate %", "Avg win", "Avg loss", "Net PnL"])
    w.writerow(
        [
            summary.get("trades"),
            summary.get("open"),
            summary.get("closed"),
            summary.get("wins"),
            summary.get("losses"),
            summary.get("win_rate"),
            summary.get("avg_win"),
            summary.get("avg_loss"),
            summary.get("net_pnl"),
        ]
    )
    w.writerow([])

    w.writerow(["BY STRATEGY"])
    w.writerow(["strategy_id", "trades", "wins", "losses", "win_rate %", "avg_win", "avg_loss", "net_pnl"])
    for row in summary.get("by_strategy") or []:
        w.writerow(
            [
                row.get("strategy_id"),
                row.get("trades"),
                row.get("wins"),
                row.get("losses"),
                row.get("win_rate"),
                row.get("avg_win"),
                row.get("avg_loss"),
                row.get("net_pnl"),
            ]
        )
    w.writerow([])

    w.writerow(["BY DAY"])
    w.writerow(["date", "trades", "closed", "open", "wins", "losses", "win_rate %", "net_pnl", "symbols"])
    for d in daily.get("days") or []:
        w.writerow(
            [
                d.get("date"),
                d.get("trades"),
                d.get("closed"),
                d.get("open"),
                d.get("wins"),
                d.get("losses"),
                d.get("win_rate"),
                d.get("net_pnl"),
                ", ".join(d.get("symbols") or []),
            ]
        )
    w.writerow([])

    w.writerow(["TRADES"])
    trade_fields = [
        "trade_day",
        "strategy_id",
        "instance_id",
        "symbol",
        "side",
        "status",
        "quantity",
        "entry_price",
        "exit_price",
        "stop_price",
        "target_price",
        "entry_at",
        "exit_at",
        "realized_pnl",
        "fees",
        "exit_reason",
    ]
    w.writerow(trade_fields)
    for t in filtered:
        day = _trade_day(t) or ""
        w.writerow(
            [
                day,
                t.get("strategy_id"),
                t.get("instance_id"),
                t.get("symbol"),
                t.get("side"),
                t.get("status"),
                t.get("quantity"),
                t.get("entry_price"),
                t.get("exit_price"),
                t.get("stop_price"),
                t.get("target_price"),
                _fmt_ist(_parse_iso_dt(t.get("entry_at"))) or (t.get("entry_at") or ""),
                _fmt_ist(_parse_iso_dt(t.get("exit_at"))) or (t.get("exit_at") or ""),
                t.get("realized_pnl"),
                t.get("fees"),
                t.get("exit_reason"),
            ]
        )
    return out.getvalue()


def full_report_html(
    *,
    from_date: str | None = None,
    to_date: str | None = None,
    strategy_id: str | None = None,
) -> str:
    """Printable HTML report (browser → Save as PDF)."""
    from html import escape

    summary = report_summary(from_date=from_date, to_date=to_date, strategy_id=strategy_id)
    daily = daily_report(from_date=from_date, to_date=to_date, strategy_id=strategy_id)
    trades = list_trades(strategy_id=strategy_id, limit=5000)
    filtered = []
    for t in trades:
        day = _trade_day(t)
        if from_date and day and day < from_date:
            continue
        if to_date and day and day > to_date:
            continue
        filtered.append(t)

    def money(v: Any) -> str:
        if v is None:
            return "—"
        try:
            return f"{float(v):,.2f}"
        except (TypeError, ValueError):
            return str(v)

    strat_rows = "".join(
        f"<tr><td>{escape(str(r.get('strategy_id') or ''))}</td>"
        f"<td>{r.get('trades')}</td><td>{r.get('wins')}</td><td>{r.get('losses')}</td>"
        f"<td>{r.get('win_rate') if r.get('win_rate') is not None else '—'}</td>"
        f"<td>{money(r.get('avg_win'))}</td><td>{money(r.get('avg_loss'))}</td>"
        f"<td>{money(r.get('net_pnl'))}</td></tr>"
        for r in (summary.get("by_strategy") or [])
    ) or "<tr><td colspan='8'>No closed trades</td></tr>"

    day_rows = "".join(
        f"<tr><td>{escape(str(d.get('date') or ''))}</td>"
        f"<td>{d.get('trades')}</td><td>{d.get('closed')}</td>"
        f"<td>{d.get('win_rate') if d.get('win_rate') is not None else '—'}</td>"
        f"<td>{money(d.get('net_pnl'))}</td>"
        f"<td>{escape(', '.join(d.get('symbols') or []))}</td></tr>"
        for d in (daily.get("days") or [])
    ) or "<tr><td colspan='6'>No daily data</td></tr>"

    trade_rows = "".join(
        f"<tr><td>{escape(str(_trade_day(t) or ''))}</td>"
        f"<td>{escape(str(t.get('strategy_id') or ''))}</td>"
        f"<td>{escape(str(t.get('symbol') or ''))}</td>"
        f"<td>{escape(str(t.get('side') or ''))}</td>"
        f"<td>{money(t.get('entry_price'))}</td>"
        f"<td>{money(t.get('exit_price'))}</td>"
        f"<td>{money(t.get('stop_price'))}</td>"
        f"<td>{money(t.get('target_price'))}</td>"
        f"<td>{money(t.get('realized_pnl'))}</td>"
        f"<td>{escape(str(t.get('status') or ''))}"
        f"{(' · ' + escape(str(t.get('exit_reason')))) if t.get('exit_reason') else ''}</td></tr>"
        for t in filtered[:500]
    ) or "<tr><td colspan='10'>No trades</td></tr>"

    period = f"{from_date or 'all'} → {to_date or 'all'}"
    filt = strategy_id or "all strategies"
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <title>Paper Desk Report</title>
  <style>
    body {{ font-family: Georgia, "Times New Roman", serif; color: #1a1a1a; margin: 2rem; }}
    h1 {{ font-size: 1.6rem; margin: 0 0 0.25rem; }}
    h2 {{ font-size: 1.1rem; margin: 1.4rem 0 0.5rem; border-bottom: 1px solid #ccc; padding-bottom: 0.25rem; }}
    .meta {{ color: #555; font-size: 0.92rem; margin-bottom: 1rem; }}
    .kpis {{ display: flex; flex-wrap: wrap; gap: 1rem; margin: 1rem 0; }}
    .kpi {{ border: 1px solid #ddd; padding: 0.6rem 0.85rem; min-width: 7rem; }}
    .kpi span {{ display: block; font-size: 0.75rem; color: #666; text-transform: uppercase; }}
    .kpi strong {{ font-size: 1.15rem; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 0.88rem; margin-bottom: 0.5rem; }}
    th, td {{ border: 1px solid #ddd; padding: 0.35rem 0.45rem; text-align: left; }}
    th {{ background: #f4f4f4; }}
    .actions {{ margin: 1rem 0 1.5rem; }}
    .actions button {{ font-size: 1rem; padding: 0.45rem 0.9rem; cursor: pointer; }}
    @media print {{
      .actions {{ display: none; }}
      body {{ margin: 0.6rem; }}
    }}
  </style>
</head>
<body>
  <div class="actions">
    <button type="button" onclick="window.print()">Print / Save as PDF</button>
  </div>
  <h1>Paper Desk — Trading Report</h1>
  <p class="meta">Period: {escape(period)} · Filter: {escape(filt)} · Generated: {escape(datetime.now(IST).strftime("%Y-%m-%d %H:%M IST"))} · Times Asia/Kolkata</p>
  <div class="kpis">
    <div class="kpi"><span>Trades</span><strong>{summary.get("trades", 0)}</strong></div>
    <div class="kpi"><span>Closed</span><strong>{summary.get("closed", 0)}</strong></div>
    <div class="kpi"><span>Win rate</span><strong>{summary.get("win_rate") if summary.get("win_rate") is not None else "—"}%</strong></div>
    <div class="kpi"><span>Net PnL</span><strong>₹{money(summary.get("net_pnl"))}</strong></div>
  </div>

  <h2>By strategy</h2>
  <table>
    <thead><tr><th>Strategy</th><th>Trades</th><th>Wins</th><th>Losses</th><th>Win %</th><th>Avg win</th><th>Avg loss</th><th>Net PnL</th></tr></thead>
    <tbody>{strat_rows}</tbody>
  </table>

  <h2>Day-wise</h2>
  <table>
    <thead><tr><th>Date</th><th>Trades</th><th>Closed</th><th>Win %</th><th>Net PnL</th><th>Symbols</th></tr></thead>
    <tbody>{day_rows}</tbody>
  </table>

  <h2>Trade details</h2>
  <table>
    <thead><tr><th>Day</th><th>Strategy</th><th>Symbol</th><th>Side</th><th>Entry</th><th>Exit</th><th>Stop</th><th>Target</th><th>PnL</th><th>Status</th></tr></thead>
    <tbody>{trade_rows}</tbody>
  </table>
  <p class="meta">Showing up to 500 trades. Use CSV full export for the complete file.</p>
</body>
</html>"""


def _trade_dict(r: PaperTradeRow) -> dict[str, Any]:
    return {
        "id": r.id,
        "session_id": r.session_id,
        "strategy_id": r.strategy_id,
        "instance_id": r.instance_id,
        "symbol": r.symbol,
        "side": r.side,
        "status": r.status,
        "quantity": r.quantity,
        "entry_price": r.entry_price,
        "exit_price": r.exit_price,
        "stop_price": r.stop_price,
        "target_price": r.target_price,
        "entry_at": r.entry_at.isoformat() if r.entry_at else None,
        "exit_at": r.exit_at.isoformat() if r.exit_at else None,
        "realized_pnl": r.realized_pnl,
        "fees": r.fees,
        "exit_reason": r.exit_reason,
        "meta": json.loads(r.meta_json) if r.meta_json else {},
    }
