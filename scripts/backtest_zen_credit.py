#!/usr/bin/env python3
"""Zen credit-spread backtest — Stratzy-aligned params + BS overnight theta.

Usage:
  cd AI-TRADING && PYTHONPATH=src .venv/bin/python scripts/backtest_zen_credit.py --days 30
  PYTHONPATH=src .venv/bin/python scripts/backtest_zen_credit.py --days 90
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from algo.analysis.load import load_candles
from algo.config import get_settings
from algo.paper.analytics import summarize_closed_trades
from algo.paper.broker import PaperBroker
from algo.paper.models import Bar, Side, SignalAction
from algo.paper.zen_credit import ZenCreditSpreadOvernightStrategy
from algo.storage.db import get_engine

IST = ZoneInfo("Asia/Kolkata")


@dataclass(frozen=True)
class Variant:
    name: str
    params: dict[str, Any]
    quantity: int
    note: str


VARIANTS: list[Variant] = [
    Variant(
        "stratzy_exact_400w_65lot",
        {
            "spread_width": 400,
            "sl_margin_pct": 0.35,
            "lot_size": 65,
            "hold_overnight": True,
            "alpha_long": 0.8,
            "alpha_short": 0.2,
            "min_impulse_pts": 0,
        },
        65,
        "Exact Stratzy About defaults (Dhan Algos) — max loss ~₹20–25k/trade",
    ),
    Variant(
        "low_risk_overnight_200w_25",
        {
            "spread_width": 200,
            "sl_margin_pct": 0.25,
            "lot_size": 25,
            "hold_overnight": True,
            "alpha_long": 0.8,
            "alpha_short": 0.2,
            "min_impulse_pts": 0,
        },
        25,
        "Same overnight logic, half wing + smaller lot",
    ),
    Variant(
        "low_risk_intraday_200w_25",
        {
            "spread_width": 200,
            "sl_margin_pct": 0.25,
            "lot_size": 25,
            "hold_overnight": False,
            "alpha_long": 0.8,
            "alpha_short": 0.2,
            "min_impulse_pts": 0,
        },
        25,
        "No overnight — flatten by 14:15",
    ),
]


def _df_to_bars(df) -> list[Bar]:
    bars: list[Bar] = []
    for ts, row in df.iterrows():
        bars.append(
            Bar(
                timestamp=ts.to_pydatetime(),
                open=float(row.open),
                high=float(row.high),
                low=float(row.low),
                close=float(row.close),
                volume=float(row.volume or 0),
            )
        )
    return bars


def _apply_signal(
    broker: PaperBroker,
    *,
    strategy_id: str,
    instrument_id: str,
    symbol: str,
    action: SignalAction,
    qty: int,
    fill_px: float,
    ts: datetime,
) -> None:
    pos = broker.position.quantity
    if action is SignalAction.SELL and pos >= 0:
        need = qty if pos == 0 else pos + qty
        if need > 0:
            broker.submit_market(
                strategy_id=strategy_id,
                instrument_id=instrument_id,
                symbol=symbol,
                side=Side.SELL,
                quantity=qty if pos == 0 else need,
                last_price=fill_px,
                ts=ts,
            )
    elif action is SignalAction.BUY and pos <= 0:
        need = qty if pos == 0 else abs(pos) + qty
        broker.submit_market(
            strategy_id=strategy_id,
            instrument_id=instrument_id,
            symbol=symbol,
            side=Side.BUY,
            quantity=qty if pos == 0 else need,
            last_price=fill_px,
            ts=ts,
        )
    elif action is SignalAction.FLAT and pos != 0:
        side = Side.SELL if pos > 0 else Side.BUY
        broker.submit_market(
            strategy_id=strategy_id,
            instrument_id=instrument_id,
            symbol=symbol,
            side=side,
            quantity=abs(pos),
            last_price=fill_px,
            ts=ts,
        )


def _est_max_loss_inr(variant: Variant) -> float:
    width = float(variant.params.get("spread_width", 400))
    credit = max(5.0, width * 0.15)
    return round((width - credit) * variant.quantity, 0)


def _run_with_warmup_gate(
    bars: list[Bar],
    variant: Variant,
    *,
    cash: float,
    test_start: datetime,
) -> dict[str, Any]:
    strat = ZenCreditSpreadOvernightStrategy(params=dict(variant.params))
    strat.reset()
    broker = PaperBroker(starting_cash=cash, fee_bps=1.0, slippage_bps=2.0)
    broker.bind(strat.id, "NSE:INDEX:NIFTY", "NIFTY")
    history: list[Bar] = []
    daily: dict[str, float] = {}
    peak_equity = cash
    max_dd = 0.0
    live = False

    for bar in bars:
        if not live and bar.timestamp.astimezone(IST) >= test_start:
            live = True
            if broker.position.quantity != 0:
                broker.position.quantity = 0
                broker.position.avg_price = 0.0
            strat.reset()

        before = broker.realized_pnl
        sig = strat.on_bar(bar, history)
        history.append(bar)

        if not live:
            continue

        fill_px = float(sig.meta.get("fill_price") or bar.close)
        if sig.action is not SignalAction.HOLD:
            _apply_signal(
                broker,
                strategy_id=strat.id,
                instrument_id="NSE:INDEX:NIFTY",
                symbol="NIFTY",
                action=sig.action,
                qty=variant.quantity,
                fill_px=fill_px,
                ts=bar.timestamp,
            )
        delta = broker.realized_pnl - before
        day = bar.timestamp.astimezone(IST).date().isoformat()
        if abs(delta) > 1e-9:
            daily[day] = daily.get(day, 0.0) + delta

        equity = cash + broker.realized_pnl
        if broker.position.quantity != 0 and sig.meta.get("mark_price") is not None:
            mark = float(sig.meta["mark_price"])
            entry = float(broker.position.avg_price or 0)
            equity += (entry - mark) * abs(broker.position.quantity)
        peak_equity = max(peak_equity, equity)
        max_dd = max(max_dd, peak_equity - equity)

    stats = summarize_closed_trades(broker.closed_trades)
    return {
        "name": variant.name,
        "note": variant.note,
        "params": variant.params,
        "quantity": variant.quantity,
        "est_max_loss_inr": _est_max_loss_inr(variant),
        "realized_pnl": round(broker.realized_pnl, 2),
        "cash": round(broker.cash, 2),
        "open_qty": broker.position.quantity,
        "fills": len(broker.fills),
        "worst_day_pnl": round(min(daily.values()), 2) if daily else 0.0,
        "best_day_pnl": round(max(daily.values()), 2) if daily else 0.0,
        "max_drawdown_inr": round(max_dd, 2),
        "daily_pnl": {k: round(v, 2) for k, v in sorted(daily.items())},
        **stats,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Zen credit-spread backtest")
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--warmup-days", type=int, default=20)
    parser.add_argument("--cash", type=float, default=320_000.0)
    parser.add_argument("--end", type=str, default="")
    args = parser.parse_args()

    settings = get_settings()
    get_engine(settings)

    print("Loading NIFTY 5m candles…", flush=True)
    probe = load_candles("NIFTY", "5m", start=(datetime.now(IST).date() - timedelta(days=5)).isoformat())
    if probe.empty:
        raise SystemExit("No NIFTY 5m candles in DB.")

    end_dt = datetime.fromisoformat(args.end).replace(tzinfo=IST) if args.end else probe.index.max().to_pydatetime()
    if end_dt.tzinfo is None:
        end_dt = end_dt.replace(tzinfo=IST)
    test_start = (end_dt.astimezone(IST).date() - timedelta(days=args.days)).isoformat()
    load_start = (end_dt.astimezone(IST).date() - timedelta(days=args.days + args.warmup_days)).isoformat()
    end_s = end_dt.astimezone(IST).date().isoformat()
    test_start_dt = datetime.fromisoformat(test_start).replace(tzinfo=IST)

    df = load_candles("NIFTY", "5m", start=load_start, end=end_s)
    if df.empty:
        raise SystemExit(f"No NIFTY 5m bars between {load_start} and {end_s}")

    bars = _df_to_bars(df)
    print(f"Loaded {len(bars)} NIFTY 5m bars ({load_start} → {end_s})", flush=True)
    print(f"Test window: {test_start} → {end_s}  |  cash ₹{args.cash:,.0f}", flush=True)
    print("Pricing: Black–Scholes + weekly expiry (overnight theta modeled)\n", flush=True)

    results: list[dict[str, Any]] = []
    for v in VARIANTS:
        print(f"  running {v.name}…", flush=True)
        raw = _run_with_warmup_gate(bars, v, cash=args.cash, test_start=test_start_dt)
        results.append(raw)
        print(
            f"    → pnl ₹{raw['net_pnl']:,.0f}  trades={raw['trades']}  "
            f"wr={raw['win_rate']}%  worstDay ₹{raw['worst_day_pnl']:,.0f}",
            flush=True,
        )

    ranked = sorted(results, key=lambda r: (r["net_pnl"], -r["max_drawdown_inr"]), reverse=True)

    print(f"\n{'variant':<32} {'pnl':>10} {'trades':>6} {'wr%':>6} {'PF':>6} {'worstDay':>10} {'maxDD':>10}")
    print("-" * 90)
    for r in ranked:
        wr = "—" if r["win_rate"] is None else f"{r['win_rate']:.1f}"
        pf = "—" if r["profit_factor"] is None else f"{r['profit_factor']:.2f}"
        print(
            f"{r['name']:<32} {r['net_pnl']:>10,.0f} {r['trades']:>6} {wr:>6} {pf:>6} "
            f"{r['worst_day_pnl']:>10,.0f} {r['max_drawdown_inr']:>10,.0f}"
        )

    best = ranked[0]
    print(f"\nBest by net PnL: {best['name']} → ₹{best['net_pnl']:,.0f}")
    print(f"  {best['note']}")
    print(f"  params: {json.dumps(best['params'])}")

    out = Path("data/logs") / f"zen_backtest_{test_start}_{end_s}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"window": [test_start, end_s], "results": ranked}, indent=2))
    print(f"\nWrote {out}")


if __name__ == "__main__":
    main()
