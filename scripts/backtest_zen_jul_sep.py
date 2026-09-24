#!/usr/bin/env python3
"""Backtest Zen Credit Spread Overnight on NIFTY 5m for Jul–Sep (IST).

Uses DB candles + Dhan rolling option marks (when cached) + paper journal so
Analytics calendar / day-click shows CE/PE trades at Stratzy-scale qty.

Stratzy (Dhan Algos) reference — Sep 2026 calendar net ≈ ₹50,820 at max size:
  qty 260 (4×65 lots), width 400, overnight hold, next-day exit ~15:00.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from zoneinfo import ZoneInfo

from algo.config import get_settings
from algo.paper.journal import daily_report, list_trades
from algo.paper.session import reset_session
from algo.storage.db import get_engine

IST = ZoneInfo("Asia/Kolkata")

# Stratzy Past Trades (Sep 2026) — for console cross-check only.
STRATZY_SEPT: dict[str, float] = {
    "2026-09-01": 8307.00,
    "2026-09-03": 11245.00,
    "2026-09-07": 26796.25,
    "2026-09-08": -5469.75,
    "2026-09-10": -6061.25,
    "2026-09-15": 28616.25,
    "2026-09-17": 12642.50,
    "2026-09-21": -14153.75,
    "2026-09-22": -11102.00,
}


def main() -> None:
    settings = get_settings()
    get_engine(settings)

    # Jul 1 → today. ~75 bars/day × ~65 sessions ≈ 5k; load extra for alpha warmup.
    max_bars = 9000
    session = reset_session()
    # Max Stratzy capital tier ≈ 4 lots. Min ₹1L ≈ 1 lot (65).
    session.add_strategy(
        "zen_credit_spread",
        symbol="NIFTY",
        timeframe="5m",
        quantity=260,
        starting_cash=320_000.0,
        params={
            "hold_overnight": True,
            "spread_width": 400,
            "lot_size": 65,
            "starting_margin": 100_000.0,
            "sl_margin_pct": 0.35,
            "entry_weekdays": "0,1,3",  # Mon/Tue/Thu (Stratzy calendar pattern)
            "entry_start": "10:15",
            "entry_end": "14:15",
            # Stratzy spotlight: auto-exit next day 15:00 if SL not hit
            "overnight_exit": "15:00",
            "confirm_bars": 1,
            "tp_credit_frac": 0.70,
            "bar_minutes": 5,
        },
    )
    snap = session.run_replay_sync(max_bars=max_bars)
    print("message:", session.message)
    for s in snap.strategies:
        print(
            f"{s.name}: cash=₹{s.cash:,.2f} realized=₹{s.realized_pnl:,.2f} "
            f"unreal=₹{s.unrealized_pnl:,.2f} fills={len(s.fills)} qty={s.quantity}"
        )

    year = date.today().year
    from_d = f"{year}-07-01"
    to_d = date.today().isoformat()
    daily = daily_report(from_date=from_d, to_date=to_d, strategy_id="zen_credit_spread")
    by_month: dict[str, list] = defaultdict(list)
    for d in daily.get("days") or []:
        by_month[d["date"][:7]].append(d)

    print(f"\n=== Zen Credit daily (IST) {from_d} → {to_d} ===")
    for month in sorted(by_month):
        rows = by_month[month]
        m_pnl = sum(float(r.get("net_pnl") or 0) for r in rows)
        print(f"\n## {month}  month_net=₹{m_pnl:,.2f}  days={len(rows)}")
        for r in rows:
            print(
                f"  {r['date']}  trades={r.get('trades')}  "
                f"W/L={r.get('wins')}/{r.get('losses')}  "
                f"net=₹{float(r.get('net_pnl') or 0):,.2f}"
            )

    print("\n=== vs Stratzy Sep calendar ===")
    ours = {d["date"]: float(d.get("net_pnl") or 0) for d in daily.get("days") or []}
    hits = 0
    for day, sp in STRATZY_SEPT.items():
        o = ours.get(day)
        if o is None:
            print(f"  {day}: ours=—  stratzy=₹{sp:,.0f}")
            continue
        same = (o > 0 and sp > 0) or (o < 0 and sp < 0)
        hits += int(same)
        print(
            f"  {day}: ours=₹{o:,.0f}  stratzy=₹{sp:,.0f}  "
            f"{'✓' if same else '✗'}  Δ=₹{o - sp:,.0f}"
        )
    print(f"sign hits {hits}/{len(STRATZY_SEPT)}  Stratzy Sep net ₹{sum(STRATZY_SEPT.values()):,.0f}")

    trades = list_trades(strategy_id="zen_credit_spread", limit=500)
    window = []
    for t in trades:
        day = (t.get("exit_at") or t.get("entry_at") or "")[:10]
        if day and from_d <= day <= to_d:
            window.append(t)
    print(f"\n=== Sample trades ({len(window)} in window) ===")
    for t in window[:25]:
        meta = t.get("meta") or {}
        print(
            f"  {(t.get('entry_at') or '')[:16]}  qty={t.get('quantity')}  "
            f"{meta.get('structure')} {meta.get('short_leg')} / {meta.get('long_leg')}  "
            f"entry={t.get('entry_price')} exit={t.get('exit_price')}  "
            f"pnl=₹{t.get('realized_pnl')}  src={meta.get('prem_src')}"
        )


if __name__ == "__main__":
    main()
