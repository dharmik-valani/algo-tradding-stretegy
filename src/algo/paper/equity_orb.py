from __future__ import annotations

"""NIFTY500 top-gainer / top-loser first-5m equity ORB strategies."""

from datetime import date, datetime, time, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo

from algo.paper.models import Bar, Signal, SignalAction
from algo.paper.strategy import Strategy

IST = ZoneInfo("Asia/Kolkata")
Mode = Literal["gainer", "loser"]


class _EquityOrbBase(Strategy):
    """Shared first-range breakout ORB for a scanned equity basket."""

    is_basket = True
    asset_kinds = ["stock"]
    mode: Mode = "gainer"
    # When True, BasketRunner sizes qty to fit cash and rolls back rejected fills.
    auto_size_cash: bool = False

    def default_params(self) -> dict[str, Any]:
        return {
            "session_open": "09:15",
            "scan_at": "09:16",
            "range_minutes": 5,
            "top_n": 5,
            "buffer_pct": 0.5,
            "risk_reward": 3.0,
            "open_eq_tol_pct": 0.05,
            "entry_end": "15:00",
            "flatten_at": "15:20",
            "scan_size": 80,
            "universe": "",
            "one_trade_per_symbol": True,
            "qty_per_symbol": 1,
        }

    def param_schema(self) -> list[dict[str, Any]]:
        long = self.mode == "gainer"
        return [
            {
                "key": "session_open",
                "label": "Session open",
                "type": "time",
                "help": "NSE open used to start the first-range candle (IST).",
                "example": "Example: 09:15 → first range is 09:15–09:20 when range=5.",
            },
            {
                "key": "scan_at",
                "label": "Scan time",
                "type": "time",
                "help": "Clock to REST-scan the universe once, then WS-subscribe only the selected names.",
                "example": "Example: 09:16 — after open, rank gainers/losers from day OHLC.",
            },
            {
                "key": "range_minutes",
                "label": "First range (min)",
                "type": "number",
                "min": 1,
                "max": 30,
                "help": "Opening range length. Break of range high (long) or low (short) triggers entry.",
                "example": "Example: 5 → use 09:15–09:20 high/low as the ORB box.",
            },
            {
                "key": "top_n",
                "label": "Stocks to trade",
                "type": "number",
                "min": 1,
                "max": 20,
                "help": f"How many {'top gainers' if long else 'top losers'} (after open=low/high filter) to run.",
                "example": "Example: 5 → trade the best 5 names that match the open filter.",
            },
            {
                "key": "buffer_pct",
                "label": "SL buffer %",
                "type": "number",
                "min": 0,
                "max": 5,
                "step": 0.1,
                "help": (
                    "Widens stop beyond the range extreme. "
                    + (
                        "Long SL = range low × (1 − buffer%)."
                        if long
                        else "Short SL = range high × (1 + buffer%)."
                    )
                ),
                "example": (
                    "Example: range low ₹100, buffer 0.5% → long SL ₹99.50."
                    if long
                    else "Example: range high ₹100, buffer 0.5% → short SL ₹100.50."
                ),
            },
            {
                "key": "risk_reward",
                "label": "Risk : Reward",
                "type": "number",
                "min": 1,
                "max": 10,
                "step": 0.5,
                "help": "Target distance = R:R × risk (entry vs stop). Default 1:3.",
                "example": (
                    "Example: entry 102, SL 100 → risk 2; R:R 3 → target 108."
                    if long
                    else "Example: entry 98, SL 100 → risk 2; R:R 3 → target 92."
                ),
            },
            {
                "key": "open_eq_tol_pct",
                "label": "Open≈extreme tol %",
                "type": "number",
                "min": 0,
                "max": 1,
                "step": 0.01,
                "help": (
                    "How close open must be to the day low (gainers) or day high (losers)."
                ),
                "example": (
                    "Example: 0.05% → open within 0.05% of low counts as open=low."
                    if long
                    else "Example: 0.05% → open within 0.05% of high counts as open=high."
                ),
            },
            {
                "key": "entry_end",
                "label": "No new entries after",
                "type": "time",
                "help": "Stop opening new breakouts after this IST time; still manage open trades.",
                "example": "Example: 15:00 — no fresh ORB entries in the last hour.",
            },
            {
                "key": "flatten_at",
                "label": "EOD flatten",
                "type": "time",
                "help": "Force-close any still-open paper legs at this IST clock (journal history kept).",
                "example": "Example: 15:20 — square off before close; desk PnL resets next morning.",
            },
            {
                "key": "scan_size",
                "label": "Universe scan size",
                "type": "number",
                "min": 10,
                "max": 500,
                "help": "How many liquid NIFTY500-style names to poll for the ranking (paper speed limit).",
                "example": "Example: 80 → scan first 80 liquid names, then pick top_n.",
            },
            {
                "key": "qty_per_symbol",
                "label": "Qty per stock",
                "type": "number",
                "min": 1,
                "max": 1000,
                "help": "Shares/lots per selected symbol (paper).",
                "example": "Example: 1 → one share unit per breakout fill in paper.",
            },
            {
                "key": "one_trade_per_symbol",
                "label": "One trade / symbol",
                "type": "select",
                "options": ["true", "false"],
                "help": "If true, each selected stock may enter only once per day.",
                "example": "Example: true → RELIANCE breaks out once; no re-entry same day.",
            },
        ]

    def reset(self) -> None:
        self.selected: list[str] = []
        self.selection_meta: list[dict[str, Any]] = []
        self._day: date | None = None
        self._legs: dict[str, dict[str, Any]] = {}

    def select_symbols(self, snapshots: list[dict[str, Any]]) -> list[str]:
        """Pick top_n names matching open=low (gainer) or open=high (loser)."""
        p = {**self.default_params(), **self.params}
        top_n = int(p["top_n"])
        tol = float(p["open_eq_tol_pct"]) / 100.0
        scored: list[tuple[float, dict[str, Any]]] = []
        for snap in snapshots:
            o = float(snap["open"])
            h = float(snap["high"])
            low = float(snap["low"])
            c = float(snap["close"])
            prev = float(snap.get("prev_close") or o)
            if o <= 0 or prev <= 0:
                continue
            if self.mode == "gainer":
                if abs(o - low) / o > tol:
                    continue
                pct = (c - prev) / prev * 100.0
            else:
                if abs(o - h) / o > tol:
                    continue
                pct = (c - prev) / prev * 100.0  # most negative for losers
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
            }
            for pct, s in picked
        ]
        for sym in self.selected:
            self._legs.setdefault(
                sym,
                {
                    "range_high": None,
                    "range_low": None,
                    "range_done": False,
                    "in_trade": False,
                    "entry": None,
                    "stop": None,
                    "target": None,
                    "trades_today": 0,
                    "side": "long" if self.mode == "gainer" else "short",
                },
            )
        return list(self.selected)

    def on_bar(self, bar: Bar, history: list[Bar]) -> Signal:
        # Basket strategies use on_symbol_bar; single-bar path is a no-op.
        return Signal(action=SignalAction.HOLD, reason="basket strategy — use universe feed")

    def on_symbol_bar(self, symbol: str, bar: Bar, history: list[Bar]) -> Signal:
        symbol = symbol.upper()
        p = {**self.default_params(), **self.params}
        local = bar.timestamp.astimezone(IST)
        day = local.date()
        if self._day != day:
            # New session day: clear legs/selection so basket re-scans
            keep_params = dict(self.params)
            self.reset()
            self.params = keep_params
            self._day = day

        if symbol not in self.selected and self.selected:
            return Signal(action=SignalAction.HOLD, reason="not in selected basket", meta={"symbol": symbol})

        leg = self._legs.setdefault(
            symbol,
            {
                "range_high": None,
                "range_low": None,
                "range_done": False,
                "in_trade": False,
                "entry": None,
                "stop": None,
                "target": None,
                "trades_today": 0,
                "side": "long" if self.mode == "gainer" else "short",
            },
        )

        open_t = _parse_hhmm(str(p["session_open"]))
        range_mins = int(p["range_minutes"])
        buffer = float(p["buffer_pct"]) / 100.0
        rr = float(p["risk_reward"])
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
            "entry": leg["entry"],
            "fill_price": float(bar.close),
            "mark_price": float(bar.close),
        }

        if clock < open_t:
            return Signal(action=SignalAction.HOLD, reason="pre-open", meta=meta)

        # Build first range
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

        # Manage open trade
        if leg["in_trade"] and leg["entry"] is not None and leg["stop"] is not None and leg["target"] is not None:
            entry = float(leg["entry"])
            stop = float(leg["stop"])
            target = float(leg["target"])
            meta.update({"entry": entry, "stop": stop, "target": target, "fill_price": float(bar.close)})
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
                if bar.high >= target or bar.close >= target:
                    leg["in_trade"] = False
                    return Signal(action=SignalAction.FLAT, reason=f"{symbol} long target ≥ {target:.2f}", meta=meta)
            else:
                if bar.high >= stop or bar.close >= stop:
                    leg["in_trade"] = False
                    return Signal(action=SignalAction.FLAT, reason=f"{symbol} short SL ≥ {stop:.2f}", meta=meta)
                if bar.low <= target or bar.close <= target:
                    leg["in_trade"] = False
                    return Signal(action=SignalAction.FLAT, reason=f"{symbol} short target ≤ {target:.2f}", meta=meta)
            return Signal(action=SignalAction.HOLD, reason=f"{symbol} in trade", meta=meta)

        if one_trade and int(leg["trades_today"]) >= 1:
            return Signal(action=SignalAction.HOLD, reason=f"{symbol} already traded today", meta=meta)

        if clock > entry_end:
            return Signal(action=SignalAction.HOLD, reason="past entry window", meta=meta)

        # Entries
        if self.mode == "gainer":
            if bar.close > rh or bar.high > rh:
                entry = float(bar.close)
                stop = rl * (1.0 - buffer)
                risk = max(entry - stop, 0.01)
                target = entry + rr * risk
                leg.update(
                    {
                        "in_trade": True,
                        "entry": entry,
                        "stop": stop,
                        "target": target,
                        "trades_today": int(leg["trades_today"]) + 1,
                    }
                )
                meta.update(
                    {
                        "entry": entry,
                        "stop": round(stop, 2),
                        "target": round(target, 2),
                        "fill_price": entry,
                        "risk": round(risk, 2),
                        "structure": "long_orb",
                    }
                )
                return Signal(
                    action=SignalAction.BUY,
                    reason=f"{symbol} broke first {range_mins}m high {rh:.2f}",
                    meta=meta,
                )
        else:
            if bar.close < rl or bar.low < rl:
                entry = float(bar.close)
                stop = rh * (1.0 + buffer)
                risk = max(stop - entry, 0.01)
                target = entry - rr * risk
                leg.update(
                    {
                        "in_trade": True,
                        "entry": entry,
                        "stop": stop,
                        "target": target,
                        "trades_today": int(leg["trades_today"]) + 1,
                    }
                )
                meta.update(
                    {
                        "entry": entry,
                        "stop": round(stop, 2),
                        "target": round(target, 2),
                        "fill_price": entry,
                        "risk": round(risk, 2),
                        "structure": "short_orb",
                    }
                )
                return Signal(
                    action=SignalAction.SELL,
                    reason=f"{symbol} broke first {range_mins}m low {rl:.2f}",
                    meta=meta,
                )

        return Signal(action=SignalAction.HOLD, reason=f"{symbol} waiting breakout", meta=meta)

    def rollback_entry(self, symbol: str, *, reason: str = "fill rejected") -> None:
        """Undo a signalled entry when the paper broker cannot fill (used by cash-autosize variants)."""
        symbol = symbol.upper()
        leg = self._legs.get(symbol)
        if not leg or not leg.get("in_trade"):
            return
        leg["in_trade"] = False
        leg["entry"] = None
        leg["stop"] = None
        leg["target"] = None
        leg["trades_today"] = max(0, int(leg.get("trades_today") or 0) - 1)
        leg["reject_reason"] = reason


class Nifty500GainerOrbStrategy(_EquityOrbBase):
    id = "nifty500_gainer_orb"
    name = "NIFTY500 Top Gainers ORB"
    description = (
        "Scan NIFTY500-style names: top gainers with open≈low; "
        "buy break of first 5m high; SL = 5m low − buffer%; target R:R 1:3."
    )
    mode: Mode = "gainer"


class Nifty500LoserOrbStrategy(_EquityOrbBase):
    id = "nifty500_loser_orb"
    name = "NIFTY500 Top Losers ORB"
    description = (
        "Scan NIFTY500-style names: top losers with open≈high; "
        "short break of first 5m low; SL = 5m high + buffer%; target R:R 1:3."
    )
    mode: Mode = "loser"


class Nifty500GainerOrbCashStrategy(Nifty500GainerOrbStrategy):
    """Same gainer ORB rules, but paper fills auto-size to available cash."""

    id = "nifty500_gainer_orb_cash"
    name = "NIFTY500 Gainers ORB (cash-sized)"
    description = (
        "Same as Top Gainers ORB, but paper qty is capped to fit cash per symbol "
        "and rejected fills roll back in_trade (no stuck phantom positions)."
    )
    auto_size_cash = True

    def default_params(self) -> dict[str, Any]:
        p = super().default_params()
        p["qty_per_symbol"] = 10  # max; runner sizes down
        return p

    def param_schema(self) -> list[dict[str, Any]]:
        schema = super().param_schema()
        for field in schema:
            if field.get("key") == "qty_per_symbol":
                field["label"] = "Max qty per stock"
                field["help"] = (
                    "Upper bound on shares. Desk auto-sizes down so cost ≤ cash "
                    "allocated to that symbol."
                )
                field["example"] = "Example: max 10, HAL @ ₹4900 with ₹20k → buys 4."
        return schema


class Nifty500LoserOrbCashStrategy(Nifty500LoserOrbStrategy):
    """Same loser ORB rules, but paper fills auto-size to available cash."""

    id = "nifty500_loser_orb_cash"
    name = "NIFTY500 Losers ORB (cash-sized)"
    description = (
        "Same as Top Losers ORB, but paper qty is capped to fit cash per symbol "
        "and rejected fills roll back in_trade (no stuck phantom positions)."
    )
    auto_size_cash = True

    def default_params(self) -> dict[str, Any]:
        p = super().default_params()
        p["qty_per_symbol"] = 10
        return p

    def param_schema(self) -> list[dict[str, Any]]:
        schema = super().param_schema()
        for field in schema:
            if field.get("key") == "qty_per_symbol":
                field["label"] = "Max qty per stock"
                field["help"] = (
                    "Upper bound on shares. Desk auto-sizes down so cost ≤ cash "
                    "allocated to that symbol."
                )
                field["example"] = "Example: max 10, MARUTI @ ₹12k with ₹20k → buys/shorts 1."
        return schema


def _parse_hhmm(value: str) -> time:
    parts = value.strip().split(":")
    return time(int(parts[0]), int(parts[1]) if len(parts) > 1 else 0)
