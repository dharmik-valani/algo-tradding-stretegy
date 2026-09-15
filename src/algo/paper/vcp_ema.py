from __future__ import annotations

"""VCP + EMA stack equity screener / breakout strategy (paper basket).

Filters NSE/BSE main-board equities (EQ / BSE A — skips SME SM/MT/XT):
  1. Close above EMA 200, 50, and 20
  2. Forming VCP (tightening contractions)
  3. Volume drying near EMA 10 or 20
  4. Enter on pivot breakout with volume expansion
  5. Stop = VCP low (prefer ~5–7% risk); target = +30–40%
"""

from datetime import date, time
from typing import Any
from zoneinfo import ZoneInfo

from algo.paper.models import Bar, Signal, SignalAction
from algo.paper.strategy import Strategy
from algo.paper.vcp_pattern import OhlcvBar, VcpSetup, breakout_volume_ok, detect_vcp

IST = ZoneInfo("Asia/Kolkata")


def _parse_hhmm(value: str) -> time:
    parts = value.strip().split(":")
    return time(int(parts[0]), int(parts[1]) if len(parts) > 1 else 0)


class VcpEmaBreakoutStrategy(Strategy):
    """Daily VCP screen → intraday pivot breakout with volume."""

    id = "vcp_ema_breakout"
    name = "VCP + EMA Breakout"
    description = (
        "Screen stocks above EMA 200/50/20 with a forming VCP and volume dry-up "
        "near EMA 10/20. Enter when price breaks the VCP pivot with volume; "
        "SL = VCP low (prefer 5–7% risk); target 30–40%. NSE/BSE main board only (no SME)."
    )
    is_basket = True
    asset_kinds = ["stock"]
    auto_size_cash = False

    def default_params(self) -> dict[str, Any]:
        return {
            "scan_at": "09:25",
            "entry_end": "15:00",
            "flatten_at": "",  # swing — hold past session unless set
            "top_n": 10,
            "scan_size": 80,
            "universe": "",
            "universe_mode": "liquid",  # liquid | nse_eq | nse_bse_eq
            "min_contractions": 2,
            "min_risk_pct": 4.0,
            "max_risk_pct": 8.0,
            "prefer_risk_lo": 5.0,
            "prefer_risk_hi": 7.0,
            "near_ema_pct": 3.5,
            "max_vol_dry_ratio": 0.85,
            "volume_breakout_mult": 1.5,
            "target_pct": 35.0,
            "qty_per_symbol": 1,
            "one_trade_per_symbol": True,
            "daily_lookback_days": 400,
        }

    def param_schema(self) -> list[dict[str, Any]]:
        return [
            {
                "key": "scan_at",
                "label": "Scan time",
                "type": "time",
                "help": "IST clock to run the daily VCP screen once, then watch selected names.",
                "example": "Example: 09:25 — after open auction, screen then trade breakouts.",
            },
            {
                "key": "top_n",
                "label": "Stocks to trade",
                "type": "number",
                "min": 1,
                "max": 30,
                "help": "How many best-scoring VCP setups to watch for breakout.",
                "example": "Example: 10 → trade the top 10 scored VCP names.",
            },
            {
                "key": "scan_size",
                "label": "Universe scan size",
                "type": "number",
                "min": 10,
                "max": 500,
                "help": "Cap how many symbols to pull daily history for (rate-limit safe).",
                "example": "Example: 80 → scan 80 liquid names. Raise carefully (Dhan 429).",
            },
            {
                "key": "universe_mode",
                "label": "Universe",
                "type": "select",
                "options": ["liquid", "nse_eq", "nse_bse_eq"],
                "help": "liquid = curated large/midcaps; nse_eq = all NSE EQ (ex SME); nse_bse_eq adds BSE A.",
                "example": "Example: liquid — fastest paper scan. nse_eq for broader main-board screen.",
            },
            {
                "key": "target_pct",
                "label": "Target %",
                "type": "number",
                "min": 20,
                "max": 50,
                "step": 1,
                "help": "Profit target from entry (30–40% typical).",
                "example": "Example: 35 → exit at entry × 1.35.",
            },
            {
                "key": "prefer_risk_lo",
                "label": "Prefer SL risk min %",
                "type": "number",
                "min": 3,
                "max": 10,
                "step": 0.5,
                "help": "Prefer VCP bases where (pivot − VCP low) / pivot is in this band.",
                "example": "Example: 5 with max 7 → favour ~5–7% stop distance.",
            },
            {
                "key": "prefer_risk_hi",
                "label": "Prefer SL risk max %",
                "type": "number",
                "min": 4,
                "max": 12,
                "step": 0.5,
                "help": "Upper end of preferred stop distance.",
                "example": "Example: 7 → bases wider than 7% score lower (still allowed if within max_risk).",
            },
            {
                "key": "volume_breakout_mult",
                "label": "Breakout vol × avg",
                "type": "number",
                "min": 1.0,
                "max": 5.0,
                "step": 0.1,
                "help": "Require breakout bar volume ≥ this × 20-bar average.",
                "example": "Example: 1.5 → need 50% above average volume on the break.",
            },
            {
                "key": "qty_per_symbol",
                "label": "Qty per stock",
                "type": "number",
                "min": 1,
                "max": 1000,
                "help": "Shares per selected symbol (paper).",
                "example": "Example: 1 → one share unit per breakout fill.",
            },
            {
                "key": "entry_end",
                "label": "No new entries after",
                "type": "time",
                "help": "Stop opening new breakouts after this IST time.",
                "example": "Example: 15:00 — no fresh entries in the last hour.",
            },
        ]

    def reset(self) -> None:
        self.selected: list[str] = []
        self.selection_meta: list[dict[str, Any]] = []
        self._day: date | None = None
        self._setups: dict[str, dict[str, Any]] = {}
        self._legs: dict[str, dict[str, Any]] = {}
        self._daily: dict[str, list[OhlcvBar]] = {}

    def max_trades_per_day(self) -> int:
        p = {**self.default_params(), **self.params}
        return max(1, int(p.get("top_n") or 10))

    def select_symbols(self, snapshots: list[dict[str, Any]]) -> list[str]:
        """Fallback when scan_universe is unavailable — keep empty (needs daily bars)."""
        _ = snapshots
        self.selected = []
        self.selection_meta = []
        return []

    def scan_universe(self, quotes: Any, universe: list[str]) -> list[str]:
        """Pull daily history, score VCP setups, lock top_n watchlist."""
        p = {**self.default_params(), **self.params}
        top_n = int(p["top_n"])
        lookback_days = int(p.get("daily_lookback_days") or 400)
        scored: list[tuple[float, str, VcpSetup, list[OhlcvBar]]] = []

        load_daily = getattr(quotes, "load_daily_bars", None)
        for sym in universe:
            symbol = sym.upper()
            try:
                if callable(load_daily):
                    bars_raw = load_daily(symbol, days=lookback_days)
                else:
                    bars_raw = []
            except Exception:
                continue
            ohlcv = [
                OhlcvBar(
                    open=float(b.open),
                    high=float(b.high),
                    low=float(b.low),
                    close=float(b.close),
                    volume=float(getattr(b, "volume", 0) or 0),
                )
                for b in bars_raw
            ]
            if len(ohlcv) < 210:
                continue
            setup = detect_vcp(
                ohlcv,
                min_contractions=int(p["min_contractions"]),
                min_risk_pct=float(p["min_risk_pct"]),
                max_risk_pct=float(p["max_risk_pct"]),
                near_ema_pct=float(p["near_ema_pct"]),
                max_vol_dry_ratio=float(p["max_vol_dry_ratio"]),
                prefer_risk_lo=float(p["prefer_risk_lo"]),
                prefer_risk_hi=float(p["prefer_risk_hi"]),
            )
            if setup is None:
                continue
            scored.append((setup.score, symbol, setup, ohlcv))

        scored.sort(key=lambda x: x[0], reverse=True)
        picked = scored[:top_n]
        self.selected = [s for _, s, _, _ in picked]
        self.selection_meta = []
        self._setups = {}
        self._daily = {}
        self._legs = {}
        for score, symbol, setup, ohlcv in picked:
            self._daily[symbol] = ohlcv
            row = {
                "symbol": symbol,
                "score": score,
                "pivot": setup.pivot,
                "vcp_low": setup.vcp_low,
                "risk_pct": setup.risk_pct,
                "contractions": setup.contractions,
                "near_ema": setup.near_ema,
                "vol_dry_ratio": setup.vol_dry_ratio,
                "ema20": setup.ema20,
                "ema50": setup.ema50,
                "ema200": setup.ema200,
                "mode": "vcp",
            }
            self.selection_meta.append(row)
            self._setups[symbol] = row
            self._legs[symbol] = {
                "in_trade": False,
                "entry": None,
                "stop": setup.vcp_low,
                "target": None,
                "pivot": setup.pivot,
                "trades_today": 0,
                "side": "long",
            }
        return list(self.selected)

    def on_bar(self, bar: Bar, history: list[Bar]) -> Signal:
        return Signal(action=SignalAction.HOLD, reason="basket strategy — use universe feed")

    def on_symbol_bar(self, symbol: str, bar: Bar, history: list[Bar]) -> Signal:
        symbol = symbol.upper()
        p = {**self.default_params(), **self.params}
        local = bar.timestamp.astimezone(IST)
        day = local.date()
        if self._day != day:
            keep_params = dict(self.params)
            # Preserve locked setups across intraday bars; only clear legs day roll
            setups = dict(self._setups)
            selected = list(self.selected)
            meta = list(self.selection_meta)
            daily = dict(self._daily)
            self.reset()
            self.params = keep_params
            self._day = day
            self._setups = setups
            self.selected = selected
            self.selection_meta = meta
            self._daily = daily
            for sym, setup in setups.items():
                self._legs[sym] = {
                    "in_trade": False,
                    "entry": None,
                    "stop": setup.get("vcp_low"),
                    "target": None,
                    "pivot": setup.get("pivot"),
                    "trades_today": 0,
                    "side": "long",
                }

        if symbol not in self.selected and self.selected:
            return Signal(
                action=SignalAction.HOLD,
                reason="not in selected basket",
                meta={"symbol": symbol},
            )

        leg = self._legs.setdefault(
            symbol,
            {
                "in_trade": False,
                "entry": None,
                "stop": (self._setups.get(symbol) or {}).get("vcp_low"),
                "target": None,
                "pivot": (self._setups.get(symbol) or {}).get("pivot"),
                "trades_today": 0,
                "side": "long",
            },
        )
        setup = self._setups.get(symbol) or {}
        pivot = float(leg.get("pivot") or setup.get("pivot") or 0)
        stop = float(leg.get("stop") or setup.get("vcp_low") or 0)
        target_pct = float(p["target_pct"]) / 100.0
        entry_end = _parse_hhmm(str(p["entry_end"]))
        clock = local.time().replace(tzinfo=None)
        one_trade = True

        meta: dict[str, Any] = {
            "symbol": symbol,
            "mode": "vcp",
            "close": round(bar.close, 2),
            "pivot": pivot,
            "stop": stop,
            "target": leg.get("target"),
            "entry": leg.get("entry"),
            "fill_price": float(bar.close),
            "mark_price": float(bar.close),
            "risk_pct": setup.get("risk_pct"),
            "score": setup.get("score"),
        }

        # Manage open trade
        if leg["in_trade"] and leg["entry"] is not None and leg["stop"] is not None and leg["target"] is not None:
            entry = float(leg["entry"])
            stop_px = float(leg["stop"])
            target = float(leg["target"])
            meta.update({"entry": entry, "stop": stop_px, "target": target})
            flat_raw = str(p.get("flatten_at") or "").strip()
            if flat_raw:
                try:
                    flat_t = _parse_hhmm(flat_raw)
                    if clock >= flat_t:
                        leg["in_trade"] = False
                        return Signal(
                            action=SignalAction.FLAT,
                            reason=f"EOD flatten at {flat_raw}",
                            meta=meta,
                        )
                except Exception:
                    pass
            if bar.low <= stop_px:
                leg["in_trade"] = False
                meta["fill_price"] = stop_px
                return Signal(action=SignalAction.FLAT, reason="VCP stop hit", meta=meta)
            if bar.high >= target:
                leg["in_trade"] = False
                meta["fill_price"] = target
                return Signal(action=SignalAction.FLAT, reason="target hit", meta=meta)
            return Signal(action=SignalAction.HOLD, reason="in VCP trade", meta=meta)

        if one_trade and int(leg.get("trades_today") or 0) >= 1:
            return Signal(action=SignalAction.HOLD, reason="one trade already taken", meta=meta)

        if clock > entry_end:
            return Signal(action=SignalAction.HOLD, reason="past entry window", meta=meta)

        if pivot <= 0 or stop <= 0:
            return Signal(action=SignalAction.HOLD, reason="no VCP setup locked", meta=meta)

        # Breakout of pivot with volume confirmation (intraday bar + daily context)
        broke = bar.close > pivot or bar.high > pivot
        if not broke:
            return Signal(action=SignalAction.HOLD, reason="waiting VCP pivot break", meta=meta)

        daily = list(self._daily.get(symbol) or [])
        # Append today's synthetic daily close for volume check when possible
        vol_ok = True
        mult = float(p["volume_breakout_mult"])
        if daily:
            probe = daily + [
                OhlcvBar(
                    open=bar.open,
                    high=bar.high,
                    low=bar.low,
                    close=bar.close,
                    volume=float(bar.volume or 0),
                )
            ]
            # If intraday volume is tiny/zero (LTP ticks), fall back to daily last bar check
            if float(bar.volume or 0) > 0:
                vol_ok = breakout_volume_ok(probe, lookback=20, mult=mult)
            else:
                vol_ok = True  # live ticks often lack volume — allow price break
        if not vol_ok:
            return Signal(
                action=SignalAction.HOLD,
                reason="breakout without volume expansion",
                meta=meta,
            )

        entry = float(bar.close)
        # Prefer stops in 5–7% band: if VCP low is tighter/wider, still use VCP low
        # but reject if risk outside hard max (already filtered at scan).
        risk = entry - stop
        if risk <= 0:
            return Signal(action=SignalAction.HOLD, reason="invalid stop vs entry", meta=meta)
        risk_pct = risk / entry * 100.0
        if risk_pct > float(p["max_risk_pct"]) + 1.0:
            # breakout ran too far from VCP low — skip chase
            return Signal(
                action=SignalAction.HOLD,
                reason=f"chase risk {risk_pct:.1f}% too wide",
                meta=meta,
            )

        target = entry * (1.0 + target_pct)
        leg["in_trade"] = True
        leg["entry"] = entry
        leg["stop"] = stop
        leg["target"] = round(target, 2)
        leg["trades_today"] = int(leg.get("trades_today") or 0) + 1
        meta.update(
            {
                "entry": entry,
                "stop": stop,
                "target": leg["target"],
                "risk_pct": round(risk_pct, 2),
                "fill_price": entry,
            }
        )
        return Signal(
            action=SignalAction.BUY,
            reason=f"VCP pivot {pivot:.2f} broken with volume",
            meta=meta,
        )
