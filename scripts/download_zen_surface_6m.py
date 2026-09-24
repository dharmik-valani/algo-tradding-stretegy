#!/usr/bin/env python3
"""Download 6 months of Dhan rolling ATM±wing marks for Zen credit backtests."""

from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from pathlib import Path

# Allow running as script from repo root
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from algo.providers.base import ProviderError  # noqa: E402
from algo.providers.dhan.adapter import DhanHistoricalDataProvider  # noqa: E402
from algo.providers.dhan.rolling_options import (  # noqa: E402
    chunk_date_ranges,
    fetch_rolling_option,
    moneyness_label,
    series_to_ts_map,
)
from algo.config import get_settings  # noqa: E402
import time

OUT = Path("data/rolling/nifty_zen_6m_2026.json")


def main() -> None:
    start = date(2026, 3, 20)
    end = date(2026, 9, 24)
    width, step = 400, 50
    wing = max(1, width // step)
    settings = get_settings()
    client = DhanHistoricalDataProvider(settings).client
    jobs = [
        ("PUT", "ATM"),
        ("PUT", moneyness_label(-wing)),
        ("PUT", moneyness_label(-max(1, wing // 2))),
        ("CALL", "ATM"),
        ("CALL", moneyness_label(+wing)),
        ("CALL", moneyness_label(+max(1, wing // 2))),
    ]
    warm = start - timedelta(days=5)
    ranges = chunk_date_ranges(warm, end, max_days=28)
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
            time.sleep(0.3)
        raw[key] = merged
        print(f"  {key}: {len(merged)} bars", flush=True)

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
        surface[str(ts)] = {
            "pe_atm": float(pa.get("close") or 0),
            "pe_wing": float(pw4.get("close") or 0),
            "pe_wing_200": float(pw2.get("close") or 0),
            "pe_vol": float(pa.get("volume") or 0),
            "ce_atm": float(ca.get("close") or 0),
            "ce_wing": float(cw4.get("close") or 0),
            "ce_wing_200": float(cw2.get("close") or 0),
            "ce_vol": float(ca.get("volume") or 0),
            "iv": float(pa.get("iv") or ca.get("iv") or 0.12),
            "spot": float(pa.get("spot") or ca.get("spot") or 0),
        }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({"width": width, "step": step, "surface": surface}))
    # Keep sept cache as a symlink-friendly copy of overlapping months for desk replay
    sept = Path("data/rolling/nifty_zen_sept_2026.json")
    sept.write_text(OUT.read_text())
    print(f"Wrote {OUT} ({len(surface)} bars) and refreshed {sept}", flush=True)


if __name__ == "__main__":
    main()
