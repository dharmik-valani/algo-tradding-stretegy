from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from algo.paper.models import Bar, Signal, SignalAction

IST = ZoneInfo("Asia/Kolkata")


class Strategy(ABC):
    """One strategy class = one dropdown option (see registry.py)."""

    id: str
    name: str
    description: str = ""
    # UI hints: which instrument kinds this strategy expects
    asset_kinds: list[str] = ["index", "stock"]

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        self.params = dict(params or {})
        self.reset()

    def reset(self) -> None:
        """Called when a paper session (re)starts."""

    @abstractmethod
    def on_bar(self, bar: Bar, history: list[Bar]) -> Signal:
        raise NotImplementedError

    def default_params(self) -> dict[str, Any]:
        return {}

    def param_schema(self) -> list[dict[str, Any]]:
        """Fields shown in the Paper Desk 'Add strategy' form."""
        return []


class HoldStrategy(Strategy):
    id = "hold"
    name = "Hold (no trades)"
    description = "Does nothing — useful to verify the paper loop is alive."
    asset_kinds = ["index", "stock", "option"]

    def on_bar(self, bar: Bar, history: list[Bar]) -> Signal:
        return Signal(action=SignalAction.HOLD, reason="hold strategy")


class EmaCrossStrategy(Strategy):
    id = "ema_cross"
    name = "EMA Cross"
    description = "Buy when fast EMA crosses above slow EMA; sell on cross below."
    asset_kinds = ["index", "stock"]

    def default_params(self) -> dict[str, Any]:
        return {"fast": 9, "slow": 21}

    def param_schema(self) -> list[dict[str, Any]]:
        return [
            {
                "key": "fast",
                "label": "Fast EMA",
                "type": "number",
                "min": 2,
                "max": 100,
                "help": "Shorter moving average — reacts quickly to price. Cross above Slow = buy signal.",
                "example": "Example: Fast 9 on NIFTY 5m means ~45 minutes of price weight.",
            },
            {
                "key": "slow",
                "label": "Slow EMA",
                "type": "number",
                "min": 3,
                "max": 300,
                "help": "Longer moving average — smoother trend. Must be greater than Fast EMA.",
                "example": "Example: Slow 21 with Fast 9 → classic short-term trend filter.",
            },
        ]

    def reset(self) -> None:
        self._prev_diff: float | None = None

    def on_bar(self, bar: Bar, history: list[Bar]) -> Signal:
        params = {**self.default_params(), **self.params}
        fast_n = int(params["fast"])
        slow_n = int(params["slow"])
        if slow_n <= fast_n:
            return Signal(action=SignalAction.HOLD, reason="invalid EMA params")
        closes = [b.close for b in history] + [bar.close]
        if len(closes) < slow_n + 1:
            return Signal(
                action=SignalAction.HOLD,
                reason=f"warming up {len(closes)}/{slow_n + 1}",
                meta={"fast": None, "slow": None},
            )
        fast = _ema(closes, fast_n)
        slow = _ema(closes, slow_n)
        diff = fast - slow
        prev = self._prev_diff
        self._prev_diff = diff
        meta = {"fast": round(fast, 2), "slow": round(slow, 2), "diff": round(diff, 4)}
        if prev is None:
            return Signal(action=SignalAction.HOLD, reason="first diff", meta=meta)
        if prev <= 0 < diff:
            return Signal(action=SignalAction.BUY, reason="EMA golden cross", meta=meta)
        if prev >= 0 > diff:
            return Signal(action=SignalAction.SELL, reason="EMA death cross", meta=meta)
        return Signal(action=SignalAction.HOLD, reason="no cross", meta=meta)


class SmaTrendStrategy(Strategy):
    id = "sma_trend"
    name = "SMA Trend"
    description = "Buy when close > SMA; go flat when close < SMA."
    asset_kinds = ["index", "stock"]

    def default_params(self) -> dict[str, Any]:
        return {"period": 20}

    def param_schema(self) -> list[dict[str, Any]]:
        return [
            {
                "key": "period",
                "label": "SMA period",
                "type": "number",
                "min": 2,
                "max": 300,
                "help": "Simple average of last N closes. Buy when price is above SMA; flatten when below.",
                "example": "Example: Period 20 on 5m ≈ last ~1.5 hours of average price.",
            },
        ]

    def on_bar(self, bar: Bar, history: list[Bar]) -> Signal:
        period = int({**self.default_params(), **self.params}["period"])
        closes = [b.close for b in history] + [bar.close]
        if len(closes) < period:
            return Signal(action=SignalAction.HOLD, reason=f"warming up {len(closes)}/{period}")
        sma = sum(closes[-period:]) / period
        meta = {"sma": round(sma, 2), "close": bar.close}
        if bar.close > sma:
            return Signal(action=SignalAction.BUY, reason="close above SMA", meta=meta)
        if bar.close < sma:
            return Signal(action=SignalAction.FLAT, reason="close below SMA", meta=meta)
        return Signal(action=SignalAction.HOLD, reason="at SMA", meta=meta)


class NiftyOptionOrbStrategy(Strategy):
    """NIFTY option: first N-minute premium high breakout, then SL/target / time exit.

    Paper note: without Dhan option chain, the feed is a synthetic ATM± premium
    derived from spot. Logic (range → entry → SL/target/time) is what we test.

    Prefer the Call / Put registry entries for running both sides at open.
    """

    id = "nifty_opt_orb"
    name = "NIFTY Opt ORB (15m)"
    description = (
        "After first-range high on option premium, buy breakout; "
        "exit on stop / 1:2 target / hold window. Prices are option premium ₹."
    )
    asset_kinds = ["option"]
    # Subclasses lock this; base allows CE|PE via params.
    locked_option_type: str | None = None

    def default_params(self) -> dict[str, Any]:
        opt = self.locked_option_type or "CE"
        return {
            "session_open": "09:15",
            "range_minutes": 15,
            "hold_minutes": 15,
            "stop_points": 17,
            "target_points": 34,  # ~1:2 of 17; override 25–40 as needed
            "risk_reward": 2.0,
            "option_type": opt,
            "strike_mode": "ATM",  # ATM | ATM+1 | ATM-1
            "strike_step": 50,
            "one_trade_per_day": True,
            "entry_start": "",  # optional HH:MM override (default = open + range)
            "entry_end": "15:00",
            "flatten_at": "15:00",
        }

    def param_schema(self) -> list[dict[str, Any]]:
        fields: list[dict[str, Any]] = [
            {
                "key": "session_open",
                "label": "Session open",
                "type": "time",
                "help": "Market open time used to start building the first-range high (IST).",
                "example": "Example: 09:15 — NSE cash/FO open. Range starts from this clock.",
            },
            {
                "key": "range_minutes",
                "label": "First range (min)",
                "type": "number",
                "min": 5,
                "max": 60,
                "help": "Minutes after open used to mark the premium high. Breakout of that high triggers entry.",
                "example": "Example: 15 → watch 09:15–09:30 premium high, then buy if price breaks it.",
            },
            {
                "key": "hold_minutes",
                "label": "Hold / exit window (min)",
                "type": "number",
                "min": 5,
                "max": 120,
                "help": "Max time to stay in the trade after entry. Exits on time even if SL/target not hit.",
                "example": "Example: 15 → enter 09:32, force exit by ~09:47 if still open.",
            },
            {
                "key": "stop_points",
                "label": "Stop (premium pts)",
                "type": "number",
                "min": 1,
                "max": 200,
                "help": "Stop-loss distance in option premium points from entry. Risk per lot ≈ stop × lot size.",
                "example": "Example: Stop 17 — buy CE at ₹120, SL ≈ ₹103 (120 − 17).",
            },
            {
                "key": "target_points",
                "label": "Target (premium pts)",
                "type": "number",
                "min": 1,
                "max": 400,
                "help": "Take-profit distance in premium points. Leave blank/0 to auto-set from Risk:Reward × Stop.",
                "example": "Example: Target 34 — entry ₹120, book near ₹154. Or clear it and use R:R instead.",
            },
            {
                "key": "risk_reward",
                "label": "Risk : Reward",
                "type": "number",
                "min": 1,
                "max": 5,
                "step": 0.5,
                "help": "Only used when Target is blank or 0. Target points = Stop × this ratio. “R:R 2” means risk ₹1 to aim ₹2.",
                "example": "Example: Stop 17, R:R 2 → Target auto = 34. Stop 17, R:R 1.5 → Target = 25.5.",
            },
        ]
        if self.locked_option_type is None:
            fields.append(
                {
                    "key": "option_type",
                    "label": "Option type",
                    "type": "select",
                    "options": ["CE", "PE"],
                    "help": "CE = Call (usually bullish premium). PE = Put (usually bearish premium).",
                    "example": "Example: CE for upside breakout; PE for downside / put ORB.",
                }
            )
        fields.extend(
            [
                {
                    "key": "strike_mode",
                    "label": "Strike",
                    "type": "select",
                    "options": ["ATM", "ATM+1", "ATM-1"],
                    "help": "Which strike vs spot. ATM = nearest; ATM+1 = one step OTM for CE / ITM for PE style bias.",
                    "example": "Example: NIFTY spot 24,187, step 50 → ATM 24,200; ATM+1 = 24,250; ATM−1 = 24,150.",
                },
                {
                    "key": "strike_step",
                    "label": "Strike step",
                    "type": "number",
                    "min": 1,
                    "max": 100,
                    "help": "Exchange strike interval for the underlying.",
                    "example": "Example: NIFTY = 50, BANKNIFTY = 100.",
                },
                {
                    "key": "entry_end",
                    "label": "No new entries after",
                    "type": "time",
                    "help": "Do not open a new ORB trade after this IST time (still manage open trades).",
                    "example": "Example: 15:00 — no fresh breakout buys after square-off time.",
                },
                {
                    "key": "flatten_at",
                    "label": "EOD flatten",
                    "type": "time",
                    "help": "Force-close an open premium trade at this IST clock if still held.",
                    "example": "Example: 15:00 — square off by 3:00 PM IST.",
                },
                {
                    "key": "one_trade_per_day",
                    "label": "One trade / day (locked)",
                    "type": "select",
                    "options": ["true"],
                    "help": "Locked on: after one entry (win or loss) this instance sits out until the next IST session day.",
                    "example": "Example: true → one CE ORB at 09:35, then flat for the rest of the day.",
                },
            ]
        )
        return fields

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        merged = dict(params or {})
        if self.locked_option_type:
            merged["option_type"] = self.locked_option_type
        super().__init__(params=merged)

    def reset(self) -> None:
        self._day: date | None = None
        self._range_high: float | None = None
        self._range_done = False
        self._in_trade = False
        self._entry_price: float | None = None
        self._entry_time: datetime | None = None
        self._trades_today = 0
        self._selected_strike: int | None = None

    def on_bar(self, bar: Bar, history: list[Bar]) -> Signal:
        p = {**self.default_params(), **self.params}
        local = bar.timestamp.astimezone(IST)
        day = local.date()
        if self._day != day:
            self.reset()
            self._day = day

        open_t = _parse_hhmm(str(p["session_open"]))
        range_mins = int(p["range_minutes"])
        hold_mins = int(p["hold_minutes"])
        stop_pts = float(p["stop_points"])
        target_pts = p.get("target_points")
        if target_pts in (None, "", 0, "0"):
            target_pts = stop_pts * float(p.get("risk_reward") or 2)
        else:
            target_pts = float(target_pts)
        entry_end = _parse_hhmm(str(p.get("entry_end") or "15:15"))
        # Always one paper entry per strategy instance per IST day.
        one_trade = True
        p["one_trade_per_day"] = True

        range_end = (datetime.combine(day, open_t, tzinfo=IST) + timedelta(minutes=range_mins)).timetz().replace(
            tzinfo=None
        )
        clock = local.time().replace(tzinfo=None)

        meta = {
            "premium": round(bar.close, 2),
            "range_high": self._range_high,
            "strike": self._selected_strike,
            "option_type": p["option_type"],
            "strike_mode": p["strike_mode"],
            "stop": stop_pts,
            "target": target_pts,
        }

        # Capture ATM strike once from opening area using spot if provided in bar meta volume hack:
        # volume unused; strike chosen externally via feed label. Keep placeholder.
        if self._selected_strike is None and history:
            # Prefer explicit strike from params override
            if p.get("strike"):
                self._selected_strike = int(float(p["strike"]))

        # Build first-range high (09:15 → 09:30 by default)
        if clock < open_t:
            return Signal(action=SignalAction.HOLD, reason="pre-open", meta=meta)

        if clock < range_end:
            high = bar.high
            self._range_high = high if self._range_high is None else max(self._range_high, high)
            meta["range_high"] = self._range_high
            return Signal(action=SignalAction.HOLD, reason="building first-range high", meta=meta)

        if not self._range_done:
            self._range_done = True
            if self._range_high is None:
                self._range_high = bar.high

        # Manage open trade: SL / target / time / EOD
        if self._in_trade and self._entry_price is not None and self._entry_time is not None:
            sl = self._entry_price - stop_pts
            tp = self._entry_price + target_pts
            meta.update({"entry": self._entry_price, "sl": round(sl, 2), "tp": round(tp, 2)})
            flat_raw = str(p.get("flatten_at") or "").strip()
            if flat_raw:
                try:
                    flat_t = _parse_hhmm(flat_raw)
                    if clock >= flat_t:
                        self._in_trade = False
                        return Signal(
                            action=SignalAction.FLAT,
                            reason=f"EOD flatten @ {flat_raw} IST",
                            meta=meta,
                        )
                except Exception:
                    pass
            if bar.low <= sl or bar.close <= sl:
                self._in_trade = False
                return Signal(action=SignalAction.FLAT, reason=f"stop hit ≤ {sl:.1f}", meta=meta)
            if bar.high >= tp or bar.close >= tp:
                self._in_trade = False
                return Signal(action=SignalAction.FLAT, reason=f"target hit ≥ {tp:.1f}", meta=meta)
            held = (local - self._entry_time.astimezone(IST)).total_seconds() / 60.0
            if held >= hold_mins:
                self._in_trade = False
                return Signal(
                    action=SignalAction.FLAT,
                    reason=f"time exit after {hold_mins}m",
                    meta=meta,
                )
            return Signal(action=SignalAction.HOLD, reason="in trade", meta=meta)

        if one_trade and self._trades_today >= 1:
            return Signal(action=SignalAction.HOLD, reason="one trade already taken today", meta=meta)

        if clock > entry_end:
            return Signal(action=SignalAction.HOLD, reason="past entry window", meta=meta)

        # Breakout of first-range high on premium
        assert self._range_high is not None
        if bar.close > self._range_high or bar.high > self._range_high:
            self._in_trade = True
            self._entry_price = float(bar.close)
            self._entry_time = local
            self._trades_today += 1
            meta.update(
                {
                    "entry": self._entry_price,
                    "sl": round(self._entry_price - stop_pts, 2),
                    "tp": round(self._entry_price + target_pts, 2),
                }
            )
            return Signal(
                action=SignalAction.BUY,
                reason=f"premium broke first {range_mins}m high {self._range_high:.1f}",
                meta=meta,
            )

        return Signal(action=SignalAction.HOLD, reason="waiting for breakout", meta=meta)


class NiftyOptionOrbCallStrategy(NiftyOptionOrbStrategy):
    """Same ORB rules, locked to Call (CE) — run beside Put at open."""

    id = "nifty_opt_orb_ce"
    name = "NIFTY Opt ORB Call (15m)"
    description = (
        "Call (CE) ORB: from 09:15, measure the CE premium high for 15 minutes. "
        "If premium breaks above that high, buy. "
        "Exit when premium hits stop (entry − stop pts), target (entry + target pts), or hold time ends. "
        "Paper uses synthetic ATM CE premium from NIFTY spot — not a live option-chain quote."
    )
    locked_option_type = "CE"


class NiftyOptionOrbPutStrategy(NiftyOptionOrbStrategy):
    """Same ORB rules, locked to Put (PE) — run beside Call at open."""

    id = "nifty_opt_orb_pe"
    name = "NIFTY Opt ORB Put (15m)"
    description = (
        "Put (PE) ORB: from 09:15, measure the PE premium high for 15 minutes. "
        "If premium breaks above that high, buy the put. "
        "Exit when premium hits stop (entry − stop pts), target (entry + target pts), or hold time ends. "
        "All prices on the desk are put premium ₹ (not NIFTY index). "
        "Paper uses synthetic ATM PE premium from NIFTY spot — not a live option-chain quote."
    )
    locked_option_type = "PE"


def _ema(values: list[float], period: int) -> float:
    k = 2 / (period + 1)
    ema = values[0]
    for v in values[1:]:
        ema = v * k + ema * (1 - k)
    return ema


def _parse_hhmm(value: str) -> time:
    parts = value.strip().split(":")
    return time(int(parts[0]), int(parts[1]) if len(parts) > 1 else 0)
