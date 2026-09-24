#!/usr/bin/env python3
"""Fit Zen credit-spread to Stratzy September calendar using Dhan rolling options.

Stratzy Past Trades (Sep 2026) only shows Mon/Tue/Thu PnL days — we treat that as
an entry-weekday filter (no Fri overnight → weekend gap).

Target calendar (max capital ~₹3.2L):
  1:+8307  3:+11245  7:+26796  8:-5469  10:-6061
  15:+28616  17:+12642  21:-14153  22:-11102
  net ≈ +₹50,821

Usage:
  PYTHONPATH=src .venv/bin/python scripts/zen_sept_fit.py
  PYTHONPATH=src .venv/bin/python scripts/zen_sept_fit.py --skip-download
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from algo.analysis.load import load_candles
from algo.config import get_settings
from algo.paper.analytics import summarize_closed_trades
from algo.paper.broker import PaperBroker
from algo.paper.models import Bar, Side, SignalAction
from algo.paper.zen_credit import ZenCreditSpreadOvernightStrategy
from algo.providers.base import ProviderError
from algo.providers.dhan.adapter import DhanHistoricalDataProvider
from algo.providers.dhan.rolling_options import (
    chunk_date_ranges,
    fetch_rolling_option,
    moneyness_label,
    series_to_ts_map,
)
from algo.storage.db import get_engine

IST = ZoneInfo("Asia/Kolkata")

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

CACHE = Path("data/rolling/nifty_zen_sept_2026.json")


def download_surface(
    *,
    start: date,
    end: date,
    width: int = 400,
    step: int = 50,
) -> dict[str, Any]:
    """Fetch ATM + wing rolling PE/CE 5m for [start, end)."""
    settings = get_settings()
    client = DhanHistoricalDataProvider(settings).client
    wing = max(1, int(width // step))
    jobs = [
        ("PUT", "ATM"),
        ("PUT", moneyness_label(-wing)),
        ("PUT", moneyness_label(-max(1, wing // 2))),
        ("CALL", "ATM"),
        ("CALL", moneyness_label(+wing)),
        ("CALL", moneyness_label(+max(1, wing // 2))),
    ]
    # Warmup: also need August for alpha lookbacks
    warm_start = start - timedelta(days=25)
    ranges = chunk_date_ranges(warm_start, end, max_days=28)
    raw: dict[str, dict[int, dict[str, float]]] = {}

    for opt, strike in jobs:
        key = f"{opt}:{strike}"
        merged: dict[int, dict[str, float]] = {}
        for a, b in ranges:
            print(f"  fetch {key} {a}→{b}…", flush=True)
            try:
                series = fetch_rolling_option(
                    client,
                    strike=strike,
                    option_type=opt,  # type: ignore[arg-type]
                    from_date=a,
                    to_date=b,
                    interval="5",
                    expiry_flag="WEEK",
                    expiry_code=1,
                )
                merged.update(series_to_ts_map(series))
            except ProviderError as exc:
                print(f"    skip {exc}", flush=True)
            time.sleep(0.25)
        raw[key] = merged
        print(f"  {key}: {len(merged)} bars", flush=True)

    # Join into flat surface rows
    pe_atm = raw.get("PUT:ATM", {})
    pe_w400 = raw.get(f"PUT:{moneyness_label(-wing)}", {})
    pe_w200 = raw.get(f"PUT:{moneyness_label(-max(1, wing // 2))}", {})
    ce_atm = raw.get("CALL:ATM", {})
    ce_w400 = raw.get(f"CALL:{moneyness_label(+wing)}", {})
    ce_w200 = raw.get(f"CALL:{moneyness_label(+max(1, wing // 2))}", {})
    all_ts = sorted(set(pe_atm) | set(ce_atm) | set(pe_w400) | set(ce_w400))
    surface: dict[str, dict[str, float]] = {}
    for ts in all_ts:
        pa = pe_atm.get(ts) or {}
        pw4 = pe_w400.get(ts) or {}
        pw2 = pe_w200.get(ts) or {}
        ca = ce_atm.get(ts) or {}
        cw4 = ce_w400.get(ts) or {}
        cw2 = ce_w200.get(ts) or {}
        if not (pa or ca):
            continue
        iv = pa.get("iv") or ca.get("iv") or 0.12
        surface[str(ts)] = {
            "pe_atm": float(pa.get("close") or 0),
            "pe_wing": float(pw4.get("close") or 0),
            "pe_wing_200": float(pw2.get("close") or 0),
            "pe_vol": float(pa.get("volume") or 0),
            "ce_atm": float(ca.get("close") or 0),
            "ce_wing": float(cw4.get("close") or 0),
            "ce_wing_200": float(cw2.get("close") or 0),
            "ce_vol": float(ca.get("volume") or 0),
            "iv": float(iv),
            "spot": float(pa.get("spot") or ca.get("spot") or 0),
        }
    payload = {"width": width, "step": step, "surface": surface}
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps(payload))
    print(f"Wrote {CACHE} ({len(surface)} joined bars)", flush=True)
    return payload


def load_surface() -> dict[int, dict[str, float]]:
    raw = json.loads(CACHE.read_text())
    return {int(k): v for k, v in raw["surface"].items()}


def _df_to_bars(df) -> list[Bar]:
    return [
        Bar(
            timestamp=ts.to_pydatetime(),
            open=float(row.open),
            high=float(row.high),
            low=float(row.low),
            close=float(row.close),
            volume=float(row.volume or 0),
        )
        for ts, row in df.iterrows()
    ]


def _apply(broker: PaperBroker, action: SignalAction, qty: int, px: float, ts: datetime, sid: str) -> None:
    pos = broker.position.quantity
    if action is SignalAction.SELL and pos >= 0:
        broker.submit_market(
            strategy_id=sid,
            instrument_id="NSE:INDEX:NIFTY",
            symbol="NIFTY",
            side=Side.SELL,
            quantity=qty if pos == 0 else pos + qty,
            last_price=px,
            ts=ts,
        )
    elif action is SignalAction.FLAT and pos != 0:
        broker.submit_market(
            strategy_id=sid,
            instrument_id="NSE:INDEX:NIFTY",
            symbol="NIFTY",
            side=Side.BUY if pos < 0 else Side.SELL,
            quantity=abs(pos),
            last_price=px,
            ts=ts,
        )


def run_config(
    bars: list[Bar],
    surface: dict[int, dict[str, float]],
    params: dict[str, Any],
    *,
    qty: int,
    cash: float,
    test_start: date,
    test_end: date,
) -> dict[str, Any]:
    strat = ZenCreditSpreadOvernightStrategy(params=params)
    strat.option_surface = surface
    strat.reset()
    broker = PaperBroker(starting_cash=cash, fee_bps=0.5, slippage_bps=1.0)
    broker.bind(strat.id, "NSE:INDEX:NIFTY", "NIFTY")
    history: list[Bar] = []
    daily: dict[str, float] = {}
    live = False
    start_dt = datetime.combine(test_start, datetime.min.time(), tzinfo=IST)
    end_dt = datetime.combine(test_end, datetime.max.time(), tzinfo=IST)

    for bar in bars:
        ts = bar.timestamp.astimezone(IST)
        if not live and ts.date() >= test_start:
            live = True
            if broker.position.quantity:
                broker.position.quantity = 0
                broker.position.avg_price = 0.0
            strat.reset()
            strat.option_surface = surface
        if ts > end_dt:
            break

        before = broker.realized_pnl
        sig = strat.on_bar(bar, history)
        history.append(bar)
        if not live:
            continue
        if sig.action is not SignalAction.HOLD:
            _apply(
                broker,
                sig.action,
                qty,
                float(sig.meta.get("fill_price") or bar.close),
                bar.timestamp,
                strat.id,
            )
        delta = broker.realized_pnl - before
        if abs(delta) > 1e-9:
            day = ts.date().isoformat()
            daily[day] = daily.get(day, 0.0) + delta

    stats = summarize_closed_trades(broker.closed_trades)
    # Compare to Stratzy calendar on overlapping days
    overlap_days = sorted(set(daily) & set(STRATZY_SEPT))
    sign_hits = sum(
        1
        for d in overlap_days
        if (daily[d] > 0 and STRATZY_SEPT[d] > 0) or (daily[d] < 0 and STRATZY_SEPT[d] < 0)
    )
    return {
        "params": params,
        "qty": qty,
        "net_pnl": round(broker.realized_pnl, 2),
        "daily_pnl": {k: round(v, 2) for k, v in sorted(daily.items())},
        "stratzy_net": round(sum(STRATZY_SEPT.values()), 2),
        "sign_hits": sign_hits,
        "overlap_days": len(overlap_days),
        **stats,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-download", action="store_true")
    ap.add_argument("--cash", type=float, default=320_000.0)
    args = ap.parse_args()

    get_engine(get_settings())
    start, end = date(2026, 9, 1), date(2026, 9, 23)

    if not args.skip_download or not CACHE.exists():
        print("Downloading Dhan rolling options (ATM ± wing)…", flush=True)
        download_surface(start=start, end=end, width=400, step=50)
    surface = load_surface()
    print(f"Surface bars: {len(surface)}", flush=True)

    # Spot bars: warmup + Sept
    df = load_candles("NIFTY", "5m", start="2026-08-01", end="2026-09-23")
    bars = _df_to_bars(df)
    print(f"Spot bars: {len(bars)}", flush=True)

    # Candidate configs — recursive search toward September green
    candidates: list[tuple[str, dict[str, Any], int]] = [
        (
            "stratzy_exact_mtwt",
            {
                "spread_width": 400,
                "sl_margin_pct": 0.35,
                "hold_overnight": True,
                "alpha_long": 0.8,
                "alpha_short": 0.2,
                "min_impulse_pts": 0,
                "entry_weekdays": "0,1,3",
                "lot_size": 65,
            },
            65,
        ),
        (
            "stratzy_tight_sl",
            {
                "spread_width": 400,
                "sl_margin_pct": 0.20,
                "hold_overnight": True,
                "alpha_long": 0.8,
                "alpha_short": 0.2,
                "min_impulse_pts": 0,
                "entry_weekdays": "0,1,3",
            },
            65,
        ),
        (
            "stratzy_strict_alpha",
            {
                "spread_width": 400,
                "sl_margin_pct": 0.35,
                "hold_overnight": True,
                "alpha_long": 0.85,
                "alpha_short": 0.15,
                "min_impulse_pts": 15,
                "entry_weekdays": "0,1,3",
            },
            65,
        ),
        (
            "low_risk_mtwt_overnight",
            {
                "spread_width": 200,
                "sl_margin_pct": 0.25,
                "hold_overnight": True,
                "alpha_long": 0.8,
                "alpha_short": 0.2,
                "min_impulse_pts": 0,
                "entry_weekdays": "0,1,3",
            },
            25,
        ),
        (
            "low_risk_mtwt_intraday",
            {
                "spread_width": 200,
                "sl_margin_pct": 0.25,
                "hold_overnight": False,
                "alpha_long": 0.8,
                "alpha_short": 0.2,
                "min_impulse_pts": 0,
                "entry_weekdays": "0,1,3",
            },
            25,
        ),
        (
            "half_size_stratzy",
            {
                "spread_width": 400,
                "sl_margin_pct": 0.30,
                "hold_overnight": True,
                "alpha_long": 0.8,
                "alpha_short": 0.2,
                "min_impulse_pts": 0,
                "entry_weekdays": "0,1,3",
            },
            32,
        ),
        (
            "mon_thu_only",
            {
                "spread_width": 400,
                "sl_margin_pct": 0.35,
                "hold_overnight": True,
                "alpha_long": 0.8,
                "alpha_short": 0.2,
                "entry_weekdays": "0,3",
            },
            65,
        ),
        (
            "all_days_stratzy",
            {
                "spread_width": 400,
                "sl_margin_pct": 0.35,
                "hold_overnight": True,
                "alpha_long": 0.8,
                "alpha_short": 0.2,
                "entry_weekdays": "all",
            },
            65,
        ),
    ]

    results = []
    for name, params, qty in candidates:
        print(f"\n=== {name} qty={qty} ===", flush=True)
        # If width 200, surface wings are for 400 — still usable as approx; prefer 400 configs for rolling marks
        r = run_config(
            bars,
            surface,
            params,
            qty=qty,
            cash=args.cash,
            test_start=start,
            test_end=end,
        )
        r["name"] = name
        results.append(r)
        print(
            f"  pnl ₹{r['net_pnl']:,.0f}  trades={r['trades']}  wr={r['win_rate']}  "
            f"sign_hits={r['sign_hits']}/{r['overlap_days']}",
            flush=True,
        )
        print(f"  daily: {r['daily_pnl']}", flush=True)

    ranked = sorted(results, key=lambda x: (x["net_pnl"], x["sign_hits"]), reverse=True)
    best = ranked[0]
    print("\n" + "=" * 60)
    print(f"BEST: {best['name']} → ₹{best['net_pnl']:,.0f} (Stratzy Sept ≈ ₹{best['stratzy_net']:,.0f})")
    print(f"params: {json.dumps(best['params'])}")
    print(f"qty: {best['qty']}")

    # If still red, expand search around best
    if best["net_pnl"] <= 0:
        print("\nStill red — expanding SL / alpha grid around best…", flush=True)
        base = dict(best["params"])
        qty = int(best["qty"])
        extra = []
        for sl in (0.15, 0.20, 0.25, 0.30, 0.40, 0.50):
            for al, ash in ((0.75, 0.25), (0.8, 0.2), (0.85, 0.15), (0.9, 0.1)):
                for hold in (True, False):
                    p = {
                        **base,
                        "sl_margin_pct": sl,
                        "alpha_long": al,
                        "alpha_short": ash,
                        "hold_overnight": hold,
                        "entry_weekdays": base.get("entry_weekdays") or "0,1,3",
                    }
                    extra.append((f"grid_sl{sl}_a{al}_{'on' if hold else 'in'}", p, qty))
        for name, params, qty in extra:
            r = run_config(bars, surface, params, qty=qty, cash=args.cash, test_start=start, test_end=end)
            r["name"] = name
            results.append(r)
            if r["net_pnl"] > 0:
                print(f"  GREEN {name}: ₹{r['net_pnl']:,.0f} trades={r['trades']}", flush=True)

        ranked = sorted(results, key=lambda x: (x["net_pnl"], x["sign_hits"]), reverse=True)
        best = ranked[0]
        print(f"\nAFTER GRID BEST: {best['name']} → ₹{best['net_pnl']:,.0f}")

    out = Path("data/logs") / "zen_sept_fit.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            {
                "stratzy_sept": STRATZY_SEPT,
                "stratzy_net": sum(STRATZY_SEPT.values()),
                "best": best,
                "top5": ranked[:5],
            },
            indent=2,
        )
    )
    print(f"Wrote {out}")

    if best["net_pnl"] > 0:
        print("\n✅ September green achieved under paper+rolling marks.")
    else:
        print("\n⚠️ September still red — need more edge (fixed-strike path / TP on credit).")


if __name__ == "__main__":
    main()
