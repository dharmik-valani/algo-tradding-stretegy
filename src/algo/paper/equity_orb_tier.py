from __future__ import annotations

"""Brother's NIFTY500 top-10 gainer/loser 5m breakout with tiered qty + dual R:R."""

from datetime import date, datetime, time, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo

from algo.paper.models import Bar, Signal, SignalAction
from algo.paper.strategy import Strategy

IST = ZoneInfo("Asia/Kolkata")
Mode = Literal["gainer", "loser"]

# Default share tiers (price band → qty). Editable via params["qty_tiers"].
DEFAULT_QTY_TIERS: list[dict[str, float]] = [
    {"min": 80, "max": 200, "qty": 500},
    {"min": 200, "max": 500, "qty": 200},
    {"min": 500, "max": 600, "qty": 150},
    {"min": 600, "max": 900, "qty": 100},
    {"min": 900, "max": 1500, "qty": 50},
]


def parse_qty_tiers(raw: Any) -> list[dict[str, float]]:
    """Accept list[dict] or '80-200:500,200-500:200,...' string."""
    if isinstance(raw, list) and raw:
        out: list[dict[str, float]] = []
        for row in raw:
            try:
                out.append(
                    {
                        "min": float(row["min"]),
                        "max": float(row["max"]),
                        "qty": float(row["qty"]),
                    }
                )
            except Exception:
                continue
        return out or list(DEFAULT_QTY_TIERS)
    if isinstance(raw, str) and raw.strip():
        out = []
        for part in raw.split(","):
            part = part.strip()
            if not part or ":" not in part or "-" not in part:
                continue
            band, qty_s = part.split(":", 1)
            lo_s, hi_s = band.split("-", 1)
            try:
                out.append({"min": float(lo_s), "max": float(hi_s), "qty": float(qty_s)})
            except Exception:
                continue
        return out or list(DEFAULT_QTY_TIERS)
    return list(DEFAULT_QTY_TIERS)


def qty_for_price(price: float, *, tiers: list[dict[str, float]] | None = None, fallback: int = 50) -> int:
    px = float(price)
    for row in tiers or DEFAULT_QTY_TIERS:
        if float(row["min"]) <= px <= float(row["max"]):
            return max(1, int(row["qty"]))
    return max(1, int(fallback))


class _EquityOrbTierBase(Strategy):
    """Scan @ 09:18 top_n by %, first 5m breakout, wide-range SL branch, dual targets."""

    is_basket = True
    asset_kinds = ["stock"]
    mode: Mode = "gainer"
    auto_size_cash: bool = False
    use_price_tiers: bool = True

    def default_params(self) -> dict[str, Any]:
        return {
            "session_open": "09:15",
            "scan_at": "09:30",
            "range_minutes": 5,
            "top_n": 10,
            "buffer_pct": 0.2,
            "wide_range_pct": 1.0,
            "risk_reward_1": 2.0,
            "risk_reward_2": 3.0,
            "partial_at_r1_pct": 50.0,
            "entry_end": "15:00",
            "flatten_at": "15:20",
            "scan_size": 120,
            "universe": "",
            "max_price": 1500.0,
            "min_price": 80.0,
            "one_trade_per_symbol": True,
            "qty_per_symbol": 50,  # fallback when price outside tiers
            "qty_tiers": "80-200:500,200-500:200,500-600:150,600-900:100,900-1500:50",
        }

    def param_schema(self) -> list[dict[str, Any]]:
        long = self.mode == "gainer"
        return [
            {
                "key": "session_open",
                "label": "Session open",
                "type": "time",
                "help": "NSE open; first 5m candle starts here.",
                "example": "09:15",
            },
            {
                "key": "scan_at",
                "label": "Scan time",
                "type": "time",
                "help": "When to REST-scan NIFTY500 by % and lock top_n. Start live after this clock → scan on next tick.",
                "example": "09:30",
            },
            {
                "key": "range_minutes",
                "label": "First range (min)",
                "type": "number",
                "min": 1,
                "max": 30,
                "help": "Opening range length for breakout box.",
                "example": "5 → 09:15–09:20",
            },
            {
                "key": "top_n",
                "label": "Top stocks",
                "type": "number",
                "min": 1,
                "max": 20,
                "help": f"How many top {'gainers' if long else 'losers'} by % to trade.",
                "example": "10",
            },
            {
                "key": "buffer_pct",
                "label": "SL buffer %",
                "type": "number",
                "min": 0,
                "max": 5,
                "step": 0.1,
                "help": "Stop buffer beyond range / entry-candle extreme.",
                "example": "0.2",
            },
            {
                "key": "wide_range_pct",
                "label": "Wide 1st candle %",
                "type": "number",
                "min": 0.1,
                "max": 10,
                "step": 0.1,
                "help": (
                    "If first-range size exceeds this %, SL uses entry candle extreme "
                    "instead of the full opening-range extreme."
                ),
                "example": "1.0",
            },
            {
                "key": "risk_reward_1",
                "label": "R:R book 1",
                "type": "number",
                "min": 1,
                "max": 10,
                "step": 0.5,
                "help": "First profit target multiple (scale-out).",
                "example": "2 → book partial at 1:2",
            },
            {
                "key": "risk_reward_2",
                "label": "R:R book 2",
                "type": "number",
                "min": 1,
                "max": 10,
                "step": 0.5,
                "help": "Final profit target multiple.",
                "example": "3 → book rest at 1:3",
            },
            {
                "key": "partial_at_r1_pct",
                "label": "Partial % at R1",
                "type": "number",
                "min": 10,
                "max": 90,
                "help": "Percent of position to exit at first target.",
                "example": "50",
            },
            {
                "key": "max_price",
                "label": "Skip above price",
                "type": "number",
                "min": 100,
                "max": 10000,
                "help": "Ignore names trading above this (default 1500).",
                "example": "1500",
            },
            {
                "key": "min_price",
                "label": "Skip below price",
                "type": "number",
                "min": 1,
                "max": 500,
                "help": "Ignore names below this band.",
                "example": "80",
            },
            {
                "key": "qty_tiers",
                "label": "Qty tiers",
                "type": "text",
                "help": "Price bands → shares: min-max:qty,... (adjust anytime).",
                "example": "80-200:500,200-500:200,600-900:100",
            },
            {
                "key": "qty_per_symbol",
                "label": "Fallback qty",
                "type": "number",
                "min": 1,
                "max": 2000,
                "help": "Used when price is outside all tiers.",
                "example": "50",
            },
            {
                "key": "scan_size",
                "label": "Universe scan size",
                "type": "number",
                "min": 10,
                "max": 500,
                "help": "How many liquid names to poll for ranking.",
                "example": "120",
            },
            {
                "key": "entry_end",
                "label": "No new entries after",
                "type": "time",
                "help": "Stop new breakouts after this IST time.",
                "example": "15:00",
            },
            {
                "key": "flatten_at",
                "label": "EOD flatten",
                "type": "time",
                "help": "Force-close open legs at this IST clock (journal kept; desk resets next day).",
                "example": "15:20",
            },
            {
                "key": "one_trade_per_symbol",
                "label": "One trade / symbol",
                "type": "select",
                "options": ["true", "false"],
                "help": "Each selected stock may enter only once per day.",
                "example": "true",
            },
        ]

    def qty_for_price(self, price: float) -> int:
        p = {**self.default_params(), **self.params}
        tiers = parse_qty_tiers(p.get("qty_tiers"))
        return qty_for_price(price, tiers=tiers, fallback=int(p.get("qty_per_symbol") or 50))

    def reset(self) -> None:
        self.selected: list[str] = []
        self.selection_meta: list[dict[str, Any]] = []
        self._day: date | None = None
        self._legs: dict[str, dict[str, Any]] = {}

    def select_symbols(self, snapshots: list[dict[str, Any]]) -> list[str]:
        """Top_n by % change; skip outside min/max price. No open≈extreme filter."""
        p = {**self.default_params(), **self.params}
        top_n = int(p["top_n"])
        max_px = float(p.get("max_price") or 1500)
        min_px = float(p.get("min_price") or 0)
        scored: list[tuple[float, dict[str, Any]]] = []
        for snap in snapshots:
            o = float(snap["open"])
            c = float(snap["close"])
            prev = float(snap.get("prev_close") or o)
            if o <= 0 or prev <= 0 or c <= 0:
                continue
            if c > max_px or c < min_px:
                continue
            pct = (c - prev) / prev * 100.0
            scored.append((pct, snap))
        reverse = self.mode == "gainer"
        scored.sort(key=lambda x: x[0], reverse=reverse)
        picked = scored[:top_n]
        self.selected = [s["symbol"].upper() for _, s in picked]
        self.selection_meta = [
            {
                "symbol": s["symbol"].upper(),
                "pct_change": round(pct, 3),
                "open": s["open"],
                "high": s["high"],
                "low": s["low"],
                "close": s["close"],
                "prev_close": s.get("prev_close"),
                "mode": self.mode,
                "qty_hint": self.qty_for_price(float(s["close"])),
            }
            for pct, s in picked
        ]
        for sym, (_, s) in zip(self.selected, picked):
            self._legs.setdefault(
                sym,
                self._empty_leg(seed_price=float(s["close"])),
            )
        return list(self.selected)

    def _empty_leg(self, *, seed_price: float | None = None) -> dict[str, Any]:
        return {
            "range_high": None,
            "range_low": None,
            "range_done": False,
            "in_trade": False,
            "entry": None,
            "stop": None,
            "target": None,
            "target_r1": None,
            "target_r2": None,
            "scaled_r1": False,
            "trades_today": 0,
            "side": "long" if self.mode == "gainer" else "short",
            "seed_price": seed_price,
            "qty": None,
        }

    def seed_range(self, symbol: str, high: float, low: float) -> None:
        """Backfill opening-range extremes from historical 1m bars after late scan."""
        symbol = symbol.upper()
        leg = self._legs.setdefault(symbol, self._empty_leg())
        if high <= 0 or low <= 0:
            return
        rh = float(leg["range_high"]) if leg["range_high"] is not None else high
        rl = float(leg["range_low"]) if leg["range_low"] is not None else low
        leg["range_high"] = max(rh, high)
        leg["range_low"] = min(rl, low)

    def on_bar(self, bar: Bar, history: list[Bar]) -> Signal:
        return Signal(action=SignalAction.HOLD, reason="basket strategy — use universe feed")

    def on_symbol_bar(self, symbol: str, bar: Bar, history: list[Bar]) -> Signal:
        symbol = symbol.upper()
        p = {**self.default_params(), **self.params}
        local = bar.timestamp.astimezone(IST)
        day = local.date()
        if self._day != day:
            keep_params = dict(self.params)
            self.reset()
            self.params = keep_params
            self._day = day

        if symbol not in self.selected and self.selected:
            return Signal(action=SignalAction.HOLD, reason="not in selected basket", meta={"symbol": symbol})

        leg = self._legs.setdefault(symbol, self._empty_leg(seed_price=float(bar.close)))

        open_t = _parse_hhmm(str(p["session_open"]))
        range_mins = int(p["range_minutes"])
        buffer = float(p["buffer_pct"]) / 100.0
        wide_pct = float(p.get("wide_range_pct") or 1.0) / 100.0
        rr1 = float(p.get("risk_reward_1") or 2.0)
        rr2 = float(p.get("risk_reward_2") or 3.0)
        partial_pct = float(p.get("partial_at_r1_pct") or 50.0) / 100.0
        entry_end = _parse_hhmm(str(p["entry_end"]))
        one_trade = str(p.get("one_trade_per_symbol", True)).lower() in {"1", "true", "yes"}
        clock = local.time().replace(tzinfo=None)
        range_end = (
            datetime.combine(day, open_t, tzinfo=IST) + timedelta(minutes=range_mins)
        ).timetz().replace(tzinfo=None)

        meta: dict[str, Any] = {
            "symbol": symbol,
            "mode": self.mode,
            "close": round(bar.close, 2),
            "range_high": leg["range_high"],
            "range_low": leg["range_low"],
            "stop": leg["stop"],
            "target": leg["target"],
            "target_r1": leg.get("target_r1"),
            "target_r2": leg.get("target_r2"),
            "entry": leg["entry"],
            "fill_price": float(bar.close),
            "mark_price": float(bar.close),
            "qty": leg.get("qty"),
        }

        if clock < open_t:
            return Signal(action=SignalAction.HOLD, reason="pre-open", meta=meta)

        if clock < range_end:
            rh = bar.high if leg["range_high"] is None else max(float(leg["range_high"]), bar.high)
            rl = bar.low if leg["range_low"] is None else min(float(leg["range_low"]), bar.low)
            leg["range_high"] = rh
            leg["range_low"] = rl
            meta["range_high"] = rh
            meta["range_low"] = rl
            return Signal(action=SignalAction.HOLD, reason="building first range", meta=meta)

        if not leg["range_done"]:
            leg["range_done"] = True
            if leg["range_high"] is None:
                leg["range_high"] = bar.high
            if leg["range_low"] is None:
                leg["range_low"] = bar.low

        rh = float(leg["range_high"])
        rl = float(leg["range_low"])
        meta["range_high"] = rh
        meta["range_low"] = rl
        mid = max((rh + rl) / 2.0, 0.01)
        range_pct = (rh - rl) / mid

        # Manage open trade (SL / dual targets)
        if leg["in_trade"] and leg["entry"] is not None and leg["stop"] is not None:
            entry = float(leg["entry"])
            stop = float(leg["stop"])
            t1 = float(leg["target_r1"] or leg["target"] or entry)
            t2 = float(leg["target_r2"] or leg["target"] or entry)
            meta.update(
                {
                    "entry": entry,
                    "stop": stop,
                    "target": leg.get("target"),
                    "target_r1": t1,
                    "target_r2": t2,
                    "fill_price": float(bar.close),
                }
            )
            flat_raw = str(p.get("flatten_at") or "").strip()
            if flat_raw:
                try:
                    flat_t = _parse_hhmm(flat_raw)
                    if clock >= flat_t:
                        leg["in_trade"] = False
                        return Signal(
                            action=SignalAction.FLAT,
                            reason=f"{symbol} EOD flatten @ {flat_raw} IST",
                            meta=meta,
                        )
                except Exception:
                    pass
            if self.mode == "gainer":
                if bar.low <= stop or bar.close <= stop:
                    leg["in_trade"] = False
                    return Signal(action=SignalAction.FLAT, reason=f"{symbol} long SL ≤ {stop:.2f}", meta=meta)
                if not leg.get("scaled_r1") and (bar.high >= t1 or bar.close >= t1):
                    leg["scaled_r1"] = True
                    leg["target"] = t2
                    meta["close_frac"] = partial_pct
                    meta["target"] = t2
                    meta["structure"] = "long_orb_scale"
                    return Signal(
                        action=SignalAction.FLAT,
                        reason=f"{symbol} long book {partial_pct:.0%} @ 1:{rr1:g} ({t1:.2f})",
                        meta=meta,
                    )
                if leg.get("scaled_r1") and (bar.high >= t2 or bar.close >= t2):
                    leg["in_trade"] = False
                    return Signal(
                        action=SignalAction.FLAT,
                        reason=f"{symbol} long book rest @ 1:{rr2:g} ({t2:.2f})",
                        meta=meta,
                    )
            else:
                if bar.high >= stop or bar.close >= stop:
                    leg["in_trade"] = False
                    return Signal(action=SignalAction.FLAT, reason=f"{symbol} short SL ≥ {stop:.2f}", meta=meta)
                if not leg.get("scaled_r1") and (bar.low <= t1 or bar.close <= t1):
                    leg["scaled_r1"] = True
                    leg["target"] = t2
                    meta["close_frac"] = partial_pct
                    meta["target"] = t2
                    meta["structure"] = "short_orb_scale"
                    return Signal(
                        action=SignalAction.FLAT,
                        reason=f"{symbol} short book {partial_pct:.0%} @ 1:{rr1:g} ({t1:.2f})",
                        meta=meta,
                    )
                if leg.get("scaled_r1") and (bar.low <= t2 or bar.close <= t2):
                    leg["in_trade"] = False
                    return Signal(
                        action=SignalAction.FLAT,
                        reason=f"{symbol} short book rest @ 1:{rr2:g} ({t2:.2f})",
                        meta=meta,
                    )
            return Signal(action=SignalAction.HOLD, reason=f"{symbol} in trade", meta=meta)

        if one_trade and int(leg["trades_today"]) >= 1:
            return Signal(action=SignalAction.HOLD, reason=f"{symbol} already traded today", meta=meta)

        if clock > entry_end:
            return Signal(action=SignalAction.HOLD, reason="past entry window", meta=meta)

        # Entries on break of first 5m high/low
        if self.mode == "gainer":
            if bar.close > rh or bar.high > rh:
                entry = float(bar.close)
                # Wide first candle → SL on entry candle low; else first-range low
                if range_pct > wide_pct:
                    stop = float(bar.low) * (1.0 - buffer)
                    sl_src = "entry_candle_low"
                else:
                    stop = rl * (1.0 - buffer)
                    sl_src = "range_low"
                risk = max(entry - stop, 0.01)
                t1 = entry + rr1 * risk
                t2 = entry + rr2 * risk
                qty = self.qty_for_price(entry)
                leg.update(
                    {
                        "in_trade": True,
                        "entry": entry,
                        "stop": stop,
                        "target": t2,
                        "target_r1": t1,
                        "target_r2": t2,
                        "scaled_r1": False,
                        "trades_today": int(leg["trades_today"]) + 1,
                        "qty": qty,
                    }
                )
                meta.update(
                    {
                        "entry": entry,
                        "stop": round(stop, 2),
                        "target": round(t2, 2),
                        "target_r1": round(t1, 2),
                        "target_r2": round(t2, 2),
                        "fill_price": entry,
                        "risk": round(risk, 2),
                        "structure": "long_orb",
                        "sl_src": sl_src,
                        "range_pct": round(range_pct * 100, 3),
                        "qty": qty,
                    }
                )
                return Signal(
                    action=SignalAction.BUY,
                    reason=f"{symbol} broke first {range_mins}m high {rh:.2f} (SL {sl_src})",
                    meta=meta,
                )
        else:
            if bar.close < rl or bar.low < rl:
                entry = float(bar.close)
                if range_pct > wide_pct:
                    stop = float(bar.high) * (1.0 + buffer)
                    sl_src = "entry_candle_high"
                else:
                    stop = rh * (1.0 + buffer)
                    sl_src = "range_high"
                risk = max(stop - entry, 0.01)
                t1 = entry - rr1 * risk
                t2 = entry - rr2 * risk
                qty = self.qty_for_price(entry)
                leg.update(
                    {
                        "in_trade": True,
                        "entry": entry,
                        "stop": stop,
                        "target": t2,
                        "target_r1": t1,
                        "target_r2": t2,
                        "scaled_r1": False,
                        "trades_today": int(leg["trades_today"]) + 1,
                        "qty": qty,
                    }
                )
                meta.update(
                    {
                        "entry": entry,
                        "stop": round(stop, 2),
                        "target": round(t2, 2),
                        "target_r1": round(t1, 2),
                        "target_r2": round(t2, 2),
                        "fill_price": entry,
                        "risk": round(risk, 2),
                        "structure": "short_orb",
                        "sl_src": sl_src,
                        "range_pct": round(range_pct * 100, 3),
                        "qty": qty,
                    }
                )
                return Signal(
                    action=SignalAction.SELL,
                    reason=f"{symbol} broke first {range_mins}m low {rl:.2f} (SL {sl_src})",
                    meta=meta,
                )

        return Signal(action=SignalAction.HOLD, reason=f"{symbol} waiting breakout", meta=meta)

    def rollback_entry(self, symbol: str, *, reason: str = "fill rejected") -> None:
        symbol = symbol.upper()
        leg = self._legs.get(symbol)
        if not leg or not leg.get("in_trade"):
            return
        leg["in_trade"] = False
        leg["entry"] = None
        leg["stop"] = None
        leg["target"] = None
        leg["target_r1"] = None
        leg["target_r2"] = None
        leg["scaled_r1"] = False
        leg["qty"] = None
        leg["trades_today"] = max(0, int(leg.get("trades_today") or 0) - 1)
        leg["reject_reason"] = reason


class Nifty500TopGainerBrkStrategy(_EquityOrbTierBase):
    id = "nifty500_top_gainer_brk"
    name = "NIFTY500 Top10 Gainers 5m Brk"
    description = (
        "At 09:18 pick top 10 NIFTY500 gainers by % (skip >₹1500). "
        "Long when price breaks first 5m high; SL = 5m low−0.2% "
        "(or entry-candle low−0.2% if 1st 5m range >1%); book 1:2 then 1:3. "
        "Qty by price tier (80–200→500, 200–500→200, 600–900→100)."
    )
    mode: Mode = "gainer"


class Nifty500TopLoserBrkStrategy(_EquityOrbTierBase):
    id = "nifty500_top_loser_brk"
    name = "NIFTY500 Top10 Losers 5m Brk"
    description = (
        "At 09:18 pick top 10 NIFTY500 losers by % (skip >₹1500). "
        "Short when price breaks first 5m low; SL = 5m high+0.2% "
        "(or entry-candle high+0.2% if 1st 5m range >1%); book 1:2 then 1:3. "
        "Qty by price tier (adjustable)."
    )
    mode: Mode = "loser"


def _parse_hhmm(value: str) -> time:
    parts = value.strip().split(":")
    return time(int(parts[0]), int(parts[1]) if len(parts) > 1 else 0)
