from __future__ import annotations

"""Zen-style NIFTY credit-spread overnight (paper).

Logic mirrors Stratzy's Zen Credit Spread Overnight on Dhan Algos:
https://algos.dhan.co/managers/stratzy/zen-credit-spread-overnight/68596cd26aa2cba24bbb67da

- Bullish (alpha & alpha2 > 0.8) → credit put spread: sell ATM PE / buy PE−400
- Bearish (both < 0.2) → credit call spread: sell ATM CE / buy CE+400
- Entries 10:15–14:15 IST; hold overnight; flatten next day ~10:15 or margin SL

Paper pricing uses Black–Scholes with weekly NIFTY expiry so overnight theta
is modeled (critical for this strategy). ATM PE/CE volumes for alpha2 remain
spot-derived proxies until rolling-option history is wired.
"""

import math
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from algo.paper.models import Bar, Signal, SignalAction
from algo.paper.strategy import Strategy

IST = ZoneInfo("Asia/Kolkata")


class ZenCreditSpreadOvernightStrategy(Strategy):
    id = "zen_credit_spread"
    name = "Zen Credit Spread Overnight"
    description = (
        "Stratzy-style NIFTY overnight credit spreads: bullish → put credit ATM/ATM−width; "
        "bearish → call credit ATM/ATM+width. Dual alpha ranks (800m / 300m); "
        "entries 10:15–14:15 IST; overnight hold with margin SL. "
        "Paper BS premiums (weekly expiry) — not live option-chain fills."
    )
    asset_kinds = ["index"]

    def default_params(self) -> dict[str, Any]:
        # Signal logic matches Stratzy About on Dhan Algos (credit put/call + dual alpha).
        return {
            "session_open": "09:15",
            "entry_start": "10:15",
            "entry_end": "14:15",
            # Stratzy spotlight: flatten next day ~15:00 if SL / TP not hit.
            "overnight_exit": "15:00",
            "bar_minutes": 5,
            "alpha_lookback_min": 800,
            "alpha2_lookback_min": 300,
            "alpha_long": 0.8,
            "alpha_short": 0.2,
            "strike_step": 50,
            "spread_width": 400,
            "roll_vol_bars": 20,
            "sl_margin_pct": 0.35,
            "lot_size": 65,
            "starting_margin": 100_000.0,
            "one_trade_at_a_time": True,
            "hold_overnight": True,
            "min_impulse_pts": 0,
            "risk_free_rate": 0.06,
            # Stratzy Past Trades calendar is Mon/Tue/Thu-heavy.
            "entry_weekdays": "0,1,3",
            # Require N consecutive extreme alpha bars before entry (avoids first-tick 10:25 fills).
            "confirm_bars": 1,
            # Early exit when this fraction of entry credit is captured (Stratzy "Hit").
            "tp_credit_frac": 0.70,
        }

    def param_schema(self) -> list[dict[str, Any]]:
        return [
            {
                "key": "entry_start",
                "label": "Entry start",
                "type": "time",
                "help": "Earliest IST time to open a new credit spread (avoids open volatility).",
                "example": "Example: 10:15 — no new Zen entries before mid-morning.",
            },
            {
                "key": "entry_end",
                "label": "Entry end",
                "type": "time",
                "help": "Latest IST time for new entries. Open spreads can still be managed after this.",
                "example": "Example: 14:15 — stop opening new spreads in the last 1h15m.",
            },
            {
                "key": "overnight_exit",
                "label": "Overnight exit",
                "type": "time",
                "help": "Next-day clock to flatten an overnight credit spread (if Hold overnight = true).",
                "example": "Example: Enter Mon 11:00, exit Tue 10:15 unless SL hit earlier.",
            },
            {
                "key": "alpha_lookback_min",
                "label": "Alpha lookback (min)",
                "type": "number",
                "min": 100,
                "max": 2000,
                "help": "Window (minutes) for ranking the 5m return÷open feature. Longer = slower, smoother signal.",
                "example": "Example: 800 min ≈ ~2 trading sessions of 5m bars for the percentile rank.",
            },
            {
                "key": "alpha2_lookback_min",
                "label": "Alpha2 lookback (min)",
                "type": "number",
                "min": 50,
                "max": 1000,
                "help": "Window for the volume/vol-scaled alpha2 rank. Both alphas must agree to trade.",
                "example": "Example: 300 min → alpha2 uses roughly half a day of 5m history.",
            },
            {
                "key": "alpha_long",
                "label": "Long threshold",
                "type": "number",
                "min": 0.5,
                "max": 1,
                "step": 0.05,
                "help": "Bullish when BOTH alpha and alpha2 exceed this (0–1 rank). Triggers credit put spread.",
                "example": "Example: 0.8 → only top ~20% strongest upside ranks count as long.",
            },
            {
                "key": "alpha_short",
                "label": "Short threshold",
                "type": "number",
                "min": 0,
                "max": 0.5,
                "step": 0.05,
                "help": "Bearish when BOTH alphas are below this. Triggers credit call spread.",
                "example": "Example: 0.2 → only weakest ~20% downside ranks count as short.",
            },
            {
                "key": "spread_width",
                "label": "Strike width (pts)",
                "type": "number",
                "min": 50,
                "max": 1000,
                "help": "Distance between short ATM leg and long hedge leg. Max loss ≈ width − credit.",
                "example": "Example: 400 with ATM 24,200 → put credit: sell 24200 PE, buy 23800 PE.",
            },
            {
                "key": "strike_step",
                "label": "Strike step",
                "type": "number",
                "min": 1,
                "max": 100,
                "help": "Strike interval used to snap ATM.",
                "example": "Example: NIFTY 50 → spot 24,187 snaps ATM to 24,200.",
            },
            {
                "key": "sl_margin_pct",
                "label": "SL as % of max loss",
                "type": "number",
                "min": 0.1,
                "max": 1,
                "step": 0.05,
                "help": "Exit when mark-to-market loss reaches this fraction of max loss (width − credit).",
                "example": "Example: Max loss 340 pts, SL 35% → cut if adverse ~119 pts on the spread mark.",
            },
            {
                "key": "min_impulse_pts",
                "label": "Min impulse (pts)",
                "type": "number",
                "min": 0,
                "max": 200,
                "help": "Ignore alpha signals if the last 5m spot move is smaller than this (filters chop).",
                "example": "Example: 25 → a 10-pt NIFTY wiggle won’t open a spread even if ranks look extreme.",
            },
            {
                "key": "lot_size",
                "label": "Lot size",
                "type": "number",
                "min": 1,
                "max": 500,
                "help": "NIFTY FO lot size (informational / sizing). Paper qty still uses the Qty field above.",
                "example": "Example: 65 — one NIFTY lot; PnL scale ≈ premium pts × 65 in live FO.",
            },
            {
                "key": "hold_overnight",
                "label": "Hold overnight",
                "type": "select",
                "options": ["true", "false"],
                "help": "true = keep spread past entry_end and exit next day at Overnight exit (or SL). false = flatten at entry_end.",
                "example": "Example: true → classic overnight credit; false → intraday-only spreads.",
            },
        ]

    def reset(self) -> None:
        self._in_trade = False
        self._structure: str | None = None  # put_credit | call_credit
        self._entry_credit: float | None = None
        self._entry_day: date | None = None
        self._entry_ts: datetime | None = None
        self._atm_at_entry: int | None = None
        self._short_strike: int | None = None
        self._long_strike: int | None = None
        self._max_loss_pts: float | None = None
        self._alpha_streak: int = 0
        self._alpha_dir: str | None = None
        # After flatten, block new entries until the next session day (Stratzy 1 overnight cycle).
        self._cooldown_until: date | None = None
        # Optional: epoch → rolling option marks (set by backtest harness)
        if not hasattr(self, "option_surface"):
            self.option_surface: dict[int, dict[str, float]] | None = None

    def _finish_trade(self, day: date) -> None:
        self._clear_trade()
        self._alpha_streak = 0
        self._alpha_dir = None
        self._cooldown_until = day  # no re-entry for the rest of this IST day

    def on_bar(self, bar: Bar, history: list[Bar]) -> Signal:
        p = {**self.default_params(), **self.params}
        local = bar.timestamp.astimezone(IST)
        clock = local.time().replace(tzinfo=None)
        day = local.date()
        r = float(p.get("risk_free_rate") or 0.06)
        weekdays = _parse_weekdays(p.get("entry_weekdays"))

        entry_start = _parse_hhmm(str(p["entry_start"]))
        entry_end = _parse_hhmm(str(p["entry_end"]))
        overnight_exit = _parse_hhmm(str(p["overnight_exit"]))
        bar_min = max(int(p["bar_minutes"]), 1)
        width = float(p["spread_width"])
        step = max(int(p["strike_step"]), 1)
        alpha_lb = max(int(p["alpha_lookback_min"]) // bar_min, 10)
        alpha2_lb = max(int(p["alpha2_lookback_min"]) // bar_min, 10)
        long_th = float(p["alpha_long"])
        short_th = float(p["alpha_short"])
        sl_pct = float(p["sl_margin_pct"])
        hold_onn = str(p.get("hold_overnight", True)).lower() in {"1", "true", "yes"}

        series = list(history) + [bar]
        spot = float(bar.close)
        atm = int(round(spot / step) * step)
        tte = _years_to_weekly_expiry(local)
        surface_row = _lookup_surface(getattr(self, "option_surface", None), bar.timestamp)

        alpha, alpha2, aux = _compute_alphas(
            series,
            alpha_lookback=alpha_lb,
            alpha2_lookback=alpha2_lb,
            roll_vol_bars=int(p["roll_vol_bars"]),
            strike_step=step,
            risk_free=r,
            option_surface=getattr(self, "option_surface", None),
        )
        iv = float(aux["iv_proxy"])
        if surface_row and surface_row.get("iv"):
            iv = float(surface_row["iv"])

        put_credit, call_credit = _spread_credits(spot, atm, width, iv, tte, r)
        # Prefer live/rolling credits when ATM + wing premiums are present
        if surface_row:
            pe_wing = surface_row.get("pe_wing_200") if int(width) <= 200 else surface_row.get("pe_wing")
            ce_wing = surface_row.get("ce_wing_200") if int(width) <= 200 else surface_row.get("ce_wing")
            if pe_wing is None:
                pe_wing = surface_row.get("pe_wing")
            if ce_wing is None:
                ce_wing = surface_row.get("ce_wing")
            if surface_row.get("pe_atm") and pe_wing is not None:
                put_credit = max(1.0, float(surface_row["pe_atm"]) - float(pe_wing))
            if surface_row.get("ce_atm") and ce_wing is not None:
                call_credit = max(1.0, float(surface_row["ce_atm"]) - float(ce_wing))
            meta_src = "rolling"
        else:
            meta_src = "bs"

        meta: dict[str, Any] = {
            "spot": round(spot, 2),
            "atm": atm,
            "alpha": None if alpha is None else round(alpha, 4),
            "alpha2": None if alpha2 is None else round(alpha2, 4),
            "put_credit": round(put_credit, 2),
            "call_credit": round(call_credit, 2),
            "structure": self._structure,
            "entry_credit": self._entry_credit,
            "tte_days": round(tte * 365.0, 2),
            "iv": round(iv, 4),
            "prem_src": meta_src,
        }

        # --- manage open overnight credit spread ---
        if self._in_trade and self._entry_credit is not None and self._structure:
            # Mark on FIXED entry strikes (Stratzy holds the same PE/CE contracts overnight).
            # Rolling ATM moneyness drifts with spot and mis-prices the open spread.
            mark = _mark_to_close(
                spot,
                structure=self._structure,
                short_strike=int(self._short_strike or atm),
                long_strike=int(self._long_strike or atm),
                iv_proxy=iv,
                tte=tte,
                rate=r,
            )
            meta["mark_price"] = round(mark, 2)
            meta["fill_price"] = round(mark, 2)
            meta["short_leg"] = (
                f"SELL {'PE' if self._structure == 'put_credit' else 'CE'} {self._short_strike}"
            )
            meta["long_leg"] = (
                f"BUY {'PE' if self._structure == 'put_credit' else 'CE'} {self._long_strike}"
            )
            loss_pts = mark - self._entry_credit
            max_loss = float(self._max_loss_pts or max(width - self._entry_credit, 1.0))
            meta["loss_pts"] = round(loss_pts, 2)
            meta["max_loss_pts"] = round(max_loss, 2)
            meta["margin_sl_level"] = round(max_loss * sl_pct, 2)
            meta["unrealized_pts"] = round(self._entry_credit - mark, 2)

            if loss_pts >= max_loss * sl_pct:
                self._finish_trade(day)
                return Signal(
                    action=SignalAction.FLAT,
                    reason=f"margin SL: loss {loss_pts:.1f} ≥ {sl_pct:.0%} of max {max_loss:.1f}",
                    meta=meta,
                )

            # Stratzy "Hit" early exits: take profit when most of the credit is captured.
            tp_frac = float(p.get("tp_credit_frac") or 0.0)
            if tp_frac > 0 and self._entry_credit > 0:
                captured = (self._entry_credit - mark) / self._entry_credit
                if captured >= tp_frac:
                    self._finish_trade(day)
                    return Signal(
                        action=SignalAction.FLAT,
                        reason=f"take-profit: captured {captured:.0%} of credit (target {tp_frac:.0%})",
                        meta=meta,
                    )

            if hold_onn and self._entry_day is not None and day > self._entry_day and clock >= overnight_exit:
                self._finish_trade(day)
                return Signal(
                    action=SignalAction.FLAT,
                    reason=f"overnight exit at {p['overnight_exit']}",
                    meta=meta,
                )

            if not hold_onn and clock >= entry_end:
                self._finish_trade(day)
                return Signal(
                    action=SignalAction.FLAT,
                    reason=f"session exit at {p['entry_end']}",
                    meta=meta,
                )

            return Signal(action=SignalAction.HOLD, reason="in credit spread", meta=meta)

        if alpha is None or alpha2 is None:
            self._alpha_streak = 0
            return Signal(
                action=SignalAction.HOLD,
                reason=f"warming alphas ({len(series)} bars)",
                meta=meta,
            )

        meta["alpha"] = round(alpha, 4)
        meta["alpha2"] = round(alpha2, 4)

        if clock < entry_start or clock > entry_end:
            self._alpha_streak = 0
            return Signal(action=SignalAction.HOLD, reason="outside entry window 10:15–14:15", meta=meta)

        if weekdays and local.weekday() not in weekdays:
            self._alpha_streak = 0
            return Signal(
                action=SignalAction.HOLD,
                reason=f"weekday {local.strftime('%a')} not in entry_weekdays",
                meta=meta,
            )

        # One overnight cycle at a time — no same-day re-entry after flatten (Stratzy desk).
        if self._cooldown_until is not None and day <= self._cooldown_until:
            return Signal(
                action=SignalAction.HOLD,
                reason=f"cooldown until after {self._cooldown_until}",
                meta=meta,
            )

        bullish = alpha > long_th and alpha2 > long_th
        bearish = alpha < short_th and alpha2 < short_th
        confirm = max(int(p.get("confirm_bars") or 1), 1)
        if bullish:
            self._alpha_streak = self._alpha_streak + 1 if self._alpha_dir == "bull" else 1
            self._alpha_dir = "bull"
        elif bearish:
            self._alpha_streak = self._alpha_streak + 1 if self._alpha_dir == "bear" else 1
            self._alpha_dir = "bear"
        else:
            self._alpha_streak = 0
            self._alpha_dir = None

        impulse = abs(spot - float(history[-1].close)) if history else 0.0
        min_impulse = float(p.get("min_impulse_pts") or 0)
        if min_impulse > 0 and impulse < min_impulse:
            return Signal(
                action=SignalAction.HOLD,
                reason=f"impulse {impulse:.1f} < min {min_impulse:.0f} pts",
                meta=meta,
            )

        if bullish and self._alpha_streak >= confirm:
            short_k, long_k = atm, atm - int(width)
            credit = put_credit
            self._open_trade("put_credit", day, local, atm, short_k, long_k, credit, width)
            self._alpha_streak = 0
            meta.update(
                {
                    "structure": "put_credit",
                    "short_leg": f"SELL PE {short_k}",
                    "long_leg": f"BUY PE {long_k}",
                    "fill_price": round(credit, 2),
                    "mark_price": round(credit, 2),
                    "entry_credit": round(credit, 2),
                    "max_loss_pts": round(max(width - credit, 1.0), 2),
                    "confirm_bars": confirm,
                }
            )
            return Signal(
                action=SignalAction.SELL,
                reason=f"bullish alpha={alpha:.2f}/alpha2={alpha2:.2f} → credit put ATM/{atm - int(width)}",
                meta=meta,
            )

        if bearish and self._alpha_streak >= confirm:
            short_k, long_k = atm, atm + int(width)
            credit = call_credit
            self._open_trade("call_credit", day, local, atm, short_k, long_k, credit, width)
            self._alpha_streak = 0
            meta.update(
                {
                    "structure": "call_credit",
                    "short_leg": f"SELL CE {short_k}",
                    "long_leg": f"BUY CE {long_k}",
                    "fill_price": round(credit, 2),
                    "mark_price": round(credit, 2),
                    "entry_credit": round(credit, 2),
                    "max_loss_pts": round(max(width - credit, 1.0), 2),
                    "confirm_bars": confirm,
                }
            )
            return Signal(
                action=SignalAction.SELL,
                reason=f"bearish alpha={alpha:.2f}/alpha2={alpha2:.2f} → credit call ATM/{atm + int(width)}",
                meta=meta,
            )

        if bullish or bearish:
            return Signal(
                action=SignalAction.HOLD,
                reason=f"alpha extreme streak {self._alpha_streak}/{confirm}",
                meta=meta,
            )

        return Signal(action=SignalAction.HOLD, reason="alphas not extreme", meta=meta)

    def _open_trade(
        self,
        structure: str,
        day: date,
        entry_ts: datetime,
        atm: int,
        short_k: int,
        long_k: int,
        credit: float,
        width: float,
    ) -> None:
        self._in_trade = True
        self._structure = structure
        self._entry_day = day
        self._entry_ts = entry_ts
        self._atm_at_entry = atm
        self._short_strike = short_k
        self._long_strike = long_k
        self._entry_credit = float(credit)
        self._max_loss_pts = max(width - credit, 1.0)

    def _clear_trade(self) -> None:
        self._in_trade = False
        self._structure = None
        self._entry_credit = None
        self._entry_day = None
        self._entry_ts = None
        self._atm_at_entry = None
        self._short_strike = None
        self._long_strike = None
        self._max_loss_pts = None


def _parse_hhmm(value: str) -> time:
    parts = value.strip().split(":")
    return time(int(parts[0]), int(parts[1]) if len(parts) > 1 else 0)


def _parse_weekdays(raw: Any) -> set[int] | None:
    """Parse '0,1,3' / [0,1,3] → weekday set. Empty/None → no filter."""
    if raw is None or raw == "":
        return None
    if isinstance(raw, (list, tuple, set)):
        vals = {int(x) for x in raw}
        return vals or None
    s = str(raw).strip()
    if not s or s.lower() in {"all", "*"}:
        return None
    vals = {int(p.strip()) for p in s.split(",") if p.strip() != ""}
    return vals or None


def _lookup_surface(
    surface: dict[int, dict[str, float]] | None,
    ts: datetime,
    *,
    tol_sec: int = 300,
) -> dict[str, float] | None:
    if not surface:
        return None
    epoch = int(ts.timestamp()) if ts.tzinfo else int(ts.replace(tzinfo=IST).timestamp())
    if epoch in surface:
        return surface[epoch]
    # nearest within tol
    best = None
    best_d = tol_sec + 1
    for k in surface:
        d = abs(k - epoch)
        if d < best_d:
            best_d = d
            best = k
    if best is not None and best_d <= tol_sec:
        return surface[best]
    return None


def _mark_from_surface_or_bs(
    spot: float,
    *,
    structure: str,
    short_strike: int,
    long_strike: int,
    iv_proxy: float,
    tte: float,
    rate: float,
    step: int,
    surface_row: dict[str, float] | None,
    width: float,
) -> float:
    """Prefer rolling ATM/wing credits; else BS on fixed strikes."""
    if surface_row:
        if structure == "put_credit" and surface_row.get("pe_atm") is not None:
            wing = surface_row.get("pe_wing_200") if int(width) <= 200 else surface_row.get("pe_wing")
            if wing is None:
                wing = surface_row.get("pe_wing")
            if wing is not None:
                return max(0.5, float(surface_row["pe_atm"]) - float(wing))
        if structure == "call_credit" and surface_row.get("ce_atm") is not None:
            wing = surface_row.get("ce_wing_200") if int(width) <= 200 else surface_row.get("ce_wing")
            if wing is None:
                wing = surface_row.get("ce_wing")
            if wing is not None:
                return max(0.5, float(surface_row["ce_atm"]) - float(wing))
    return _mark_to_close(
        spot,
        structure=structure,
        short_strike=short_strike,
        long_strike=long_strike,
        iv_proxy=iv_proxy,
        tte=tte,
        rate=rate,
    )


def _percentile_rank(values: list[float], current: float) -> float:
    """Fraction of window values ≤ current → [0, 1]."""
    if not values:
        return 0.5
    le = sum(1 for v in values if v <= current)
    return le / len(values)


def _day_open(series: list[Bar], idx: int) -> float:
    day = series[idx].timestamp.astimezone(IST).date()
    open_px = series[idx].open
    for j in range(idx, -1, -1):
        if series[j].timestamp.astimezone(IST).date() != day:
            break
        open_px = series[j].open
    return float(open_px) if open_px else float(series[idx].close)


def _next_weekly_expiry(day: date) -> date:
    """NIFTY weekly options expire on Tuesday (NSE). Same-day if already Tuesday before close."""
    # weekday: Mon=0 … Sun=6; Tuesday=1
    days_ahead = (1 - day.weekday()) % 7
    return day + timedelta(days=days_ahead)


def _years_to_weekly_expiry(ts: datetime) -> float:
    """Fractional years to next weekly expiry (min ~2 trading hours so premium ≠ intrinsic)."""
    local = ts.astimezone(IST)
    expiry = _next_weekly_expiry(local.date())
    # Expiry ~15:30 IST
    exp_dt = datetime.combine(expiry, time(15, 30), tzinfo=IST)
    if exp_dt <= local:
        exp_dt = datetime.combine(_next_weekly_expiry(local.date() + timedelta(days=1)), time(15, 30), tzinfo=IST)
    secs = max((exp_dt - local).total_seconds(), 2.0 * 3600)
    return secs / (365.0 * 24.0 * 3600.0)


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _bs_premium(
    spot: float,
    strike: float,
    *,
    opt: str,
    iv: float,
    tte: float,
    rate: float = 0.06,
) -> float:
    """Black–Scholes European option premium (paper proxy)."""
    s = max(float(spot), 1.0)
    k = max(float(strike), 1.0)
    sigma = max(float(iv), 0.05)
    t = max(float(tte), 1e-6)
    if opt == "CE":
        intrinsic = max(s - k, 0.0)
    else:
        intrinsic = max(k - s, 0.0)
    try:
        d1 = (math.log(s / k) + (rate + 0.5 * sigma * sigma) * t) / (sigma * math.sqrt(t))
        d2 = d1 - sigma * math.sqrt(t)
        if opt == "CE":
            px = s * _norm_cdf(d1) - k * math.exp(-rate * t) * _norm_cdf(d2)
        else:
            px = k * math.exp(-rate * t) * _norm_cdf(-d2) - s * _norm_cdf(-d1)
    except (ValueError, OverflowError, ZeroDivisionError):
        px = intrinsic
    return max(1.0, float(px), intrinsic)


# Back-compat alias used by older tests / call sites
def _seed_prem(spot: float, strike: int, opt: str, iv: float, tte: float = 3 / 365, rate: float = 0.06) -> float:
    return _bs_premium(spot, strike, opt=opt, iv=iv, tte=tte, rate=rate)


def _rolling_vol(prems: list[float], window: int) -> float:
    if len(prems) < 3:
        return 0.02
    w = prems[-window:] if len(prems) >= window else prems
    rets = []
    for i in range(1, len(w)):
        if w[i - 1] > 0:
            rets.append((w[i] - w[i - 1]) / w[i - 1])
    if len(rets) < 2:
        return 0.02
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return max(var**0.5, 1e-4)


def _compute_alphas(
    series: list[Bar],
    *,
    alpha_lookback: int,
    alpha2_lookback: int,
    roll_vol_bars: int,
    strike_step: int,
    risk_free: float = 0.06,
    option_surface: dict[int, dict[str, float]] | None = None,
) -> tuple[float | None, float | None, dict[str, Any]]:
    """Return (alpha, alpha2, aux). None while warming up."""
    need = max(alpha_lookback, alpha2_lookback) + 2
    if len(series) < need:
        return None, None, {"iv_proxy": 0.12}

    closes = [float(b.close) for b in series]
    n = len(closes)
    vol_n = min(20, n - 1)
    spot_rets = [
        (closes[i] - closes[i - 1]) / closes[i - 1]
        for i in range(n - vol_n, n)
        if closes[i - 1] > 0
    ]
    iv = 0.12
    if len(spot_rets) >= 5:
        m = sum(spot_rets) / len(spot_rets)
        var = sum((r - m) ** 2 for r in spot_rets) / max(len(spot_rets) - 1, 1)
        iv = max((var**0.5) * (252 * 75) ** 0.5, 0.08)
        iv = min(iv, 0.45)

    start_i = max(1, n - need - roll_vol_bars - 2)
    alpha_series: list[float] = []
    alpha2_series: list[float] = []
    ce_prems: list[float] = []
    pe_prems: list[float] = []

    for i in range(start_i, n):
        spot = closes[i]
        prev = closes[i - 1]
        day_open = _day_open(series, i)
        # Stratzy alpha: 5-minute close-to-close change normalized by day open, then ranked.
        # (Bar open→close alone is too noisy for sustained confirm streaks.)
        feat = (spot - prev) / day_open if day_open else 0.0

        atm = int(round(spot / strike_step) * strike_step)
        ts = series[i].timestamp
        tte = _years_to_weekly_expiry(ts if ts.tzinfo else ts.replace(tzinfo=IST))
        row = _lookup_surface(option_surface, ts)
        if row and row.get("ce_atm") and row.get("pe_atm"):
            ce = float(row["ce_atm"])
            pe = float(row["pe_atm"])
            vol_ce = max(float(row.get("ce_vol") or 1.0), 1.0)
            vol_pe = max(float(row.get("pe_vol") or 1.0), 1.0)
            if row.get("iv"):
                iv = float(row["iv"])
        else:
            ce = _bs_premium(spot, atm, opt="CE", iv=iv, tte=tte, rate=risk_free)
            pe = _bs_premium(spot, atm, opt="PE", iv=iv, tte=tte, rate=risk_free)
            vol_ce = max(float(series[i].volume), 1.0) * (1.0 + max(spot - prev, 0) / max(spot, 1) * 20)
            vol_pe = max(float(series[i].volume), 1.0) * (1.0 + max(prev - spot, 0) / max(spot, 1) * 20)
        ce_prems.append(ce)
        pe_prems.append(pe)

        avg_vol_ratio = (vol_ce + vol_pe) / 2.0
        roll_ce = _rolling_vol(ce_prems, roll_vol_bars)
        roll_pe = _rolling_vol(pe_prems, roll_vol_bars)
        atm_vol = roll_ce + roll_pe
        # Stratzy alpha2: price change × avg ATM PE/CE volume / ATM vol (CE+PE rolling)
        feat2 = ((spot - prev) * avg_vol_ratio) / max(atm_vol * max(spot, 1.0), 1e-6)

        alpha_series.append(feat)
        alpha2_series.append(feat2)

    a_win = alpha_series[-alpha_lookback:]
    a2_win = alpha2_series[-alpha2_lookback:]
    alpha = _percentile_rank(a_win, alpha_series[-1])
    alpha2 = _percentile_rank(a2_win, alpha2_series[-1])
    return alpha, alpha2, {"iv_proxy": iv, "atm_ce": ce_prems[-1], "atm_pe": pe_prems[-1]}


def _spread_credits(
    spot: float,
    atm: int,
    width: float,
    iv: float,
    tte: float,
    rate: float = 0.06,
) -> tuple[float, float]:
    """Net credit ≈ short ATM − long wing (BS)."""
    w = int(width)
    pe_atm = _bs_premium(spot, atm, opt="PE", iv=iv, tte=tte, rate=rate)
    pe_wing = _bs_premium(spot, atm - w, opt="PE", iv=iv, tte=tte, rate=rate)
    ce_atm = _bs_premium(spot, atm, opt="CE", iv=iv, tte=tte, rate=rate)
    ce_wing = _bs_premium(spot, atm + w, opt="CE", iv=iv, tte=tte, rate=rate)
    put_credit = max(1.0, pe_atm - pe_wing)
    call_credit = max(1.0, ce_atm - ce_wing)
    return put_credit, call_credit


def _mark_to_close(
    spot: float,
    *,
    structure: str,
    short_strike: int,
    long_strike: int,
    iv_proxy: float,
    tte: float = 3 / 365,
    rate: float = 0.06,
) -> float:
    """Debit to close the credit spread (short prem − long prem) with BS theta/delta."""
    if structure == "put_credit":
        short_p = _bs_premium(spot, short_strike, opt="PE", iv=iv_proxy, tte=tte, rate=rate)
        long_p = _bs_premium(spot, long_strike, opt="PE", iv=iv_proxy, tte=tte, rate=rate)
    else:
        short_p = _bs_premium(spot, short_strike, opt="CE", iv=iv_proxy, tte=tte, rate=rate)
        long_p = _bs_premium(spot, long_strike, opt="CE", iv=iv_proxy, tte=tte, rate=rate)
    return max(0.5, short_p - long_p)
