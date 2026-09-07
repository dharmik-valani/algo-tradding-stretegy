from __future__ import annotations

"""Zen-style NIFTY credit-spread overnight (paper).

Logic mirrors the public About text for Stratzy's Zen Credit Spread Overnight on
Dhan Algos (credit put spread when bullish, credit call spread when bearish;
alpha / alpha2 ranks; 10:15–14:15 IST window). See:
https://algos.dhan.co/managers/stratzy/zen-credit-spread-overnight/68596cd26aa2cba24bbb67da?tab=about-algo

Paper note: ATM CE/PE volumes & vols are synthetic proxies from spot until a
real option-chain Data API is available. Virtual fills only.
"""

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
        "NIFTY credit spreads: bullish → sell ATM PE / buy PE−400; "
        "bearish → sell ATM CE / buy CE+400. Dual alpha ranks (800m / 300m); "
        "entries 10:15–14:15 IST; overnight hold with margin-based SL. "
        "Paper proxy of Stratzy Zen Credit Spread Overnight (Dhan Algos)."
    )
    asset_kinds = ["index"]

    def default_params(self) -> dict[str, Any]:
        return {
            "session_open": "09:15",
            "entry_start": "10:15",
            "entry_end": "14:15",
            "overnight_exit": "10:15",  # flatten next day at this clock (before new entries)
            "bar_minutes": 5,
            "alpha_lookback_min": 800,
            "alpha2_lookback_min": 300,
            "alpha_long": 0.8,
            "alpha_short": 0.2,
            "strike_step": 50,
            "spread_width": 400,
            "roll_vol_bars": 20,
            "sl_margin_pct": 0.35,  # stop when loss ≥ this fraction of max loss (width)
            "lot_size": 65,  # NIFTY lot (informational / margin estimate)
            "starting_margin": 100_000.0,
            "one_trade_at_a_time": True,
            "hold_overnight": True,
            "min_impulse_pts": 25,  # ignore alpha fires on tiny chop
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
        self._atm_at_entry: int | None = None
        self._short_strike: int | None = None
        self._long_strike: int | None = None
        self._max_loss_pts: float | None = None

    def on_bar(self, bar: Bar, history: list[Bar]) -> Signal:
        p = {**self.default_params(), **self.params}
        local = bar.timestamp.astimezone(IST)
        clock = local.time().replace(tzinfo=None)
        day = local.date()

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

        alpha, alpha2, aux = _compute_alphas(
            series,
            alpha_lookback=alpha_lb,
            alpha2_lookback=alpha2_lb,
            roll_vol_bars=int(p["roll_vol_bars"]),
            strike_step=step,
        )

        # Spread marks for management / entry pricing
        put_credit, call_credit = _spread_credits(spot, atm, width, aux["iv_proxy"])

        meta: dict[str, Any] = {
            "spot": round(spot, 2),
            "atm": atm,
            "alpha": None if alpha is None else round(alpha, 4),
            "alpha2": None if alpha2 is None else round(alpha2, 4),
            "put_credit": round(put_credit, 2),
            "call_credit": round(call_credit, 2),
            "structure": self._structure,
            "entry_credit": self._entry_credit,
        }

        # --- manage open overnight credit spread ---
        if self._in_trade and self._entry_credit is not None and self._structure:
            mark = _mark_to_close(
                spot,
                structure=self._structure,
                short_strike=int(self._short_strike or atm),
                long_strike=int(self._long_strike or atm),
                iv_proxy=aux["iv_proxy"],
            )
            meta["mark_price"] = round(mark, 2)
            meta["fill_price"] = round(mark, 2)
            loss_pts = mark - self._entry_credit  # adverse if mark rises vs credit
            max_loss = float(self._max_loss_pts or max(width - self._entry_credit, 1.0))
            meta["loss_pts"] = round(loss_pts, 2)
            meta["max_loss_pts"] = round(max_loss, 2)
            meta["margin_sl_level"] = round(max_loss * sl_pct, 2)

            # Margin-based stop: loss vs fraction of max loss (width-based)
            if loss_pts >= max_loss * sl_pct:
                self._clear_trade()
                return Signal(
                    action=SignalAction.FLAT,
                    reason=f"margin SL: loss {loss_pts:.1f} ≥ {sl_pct:.0%} of max {max_loss:.1f}",
                    meta=meta,
                )

            # Overnight exit next day at configured clock
            if hold_onn and self._entry_day is not None and day > self._entry_day and clock >= overnight_exit:
                self._clear_trade()
                return Signal(
                    action=SignalAction.FLAT,
                    reason=f"overnight exit at {p['overnight_exit']}",
                    meta=meta,
                )

            # If not holding overnight, flatten at entry_end
            if not hold_onn and clock >= entry_end:
                self._clear_trade()
                return Signal(
                    action=SignalAction.FLAT,
                    reason=f"session exit at {p['entry_end']}",
                    meta=meta,
                )

            return Signal(action=SignalAction.HOLD, reason="in credit spread", meta=meta)

        # Warmup / session gates
        if alpha is None or alpha2 is None:
            return Signal(
                action=SignalAction.HOLD,
                reason=f"warming alphas ({len(series)} bars)",
                meta=meta,
            )

        meta["alpha"] = round(alpha, 4)
        meta["alpha2"] = round(alpha2, 4)

        if clock < entry_start or clock > entry_end:
            return Signal(action=SignalAction.HOLD, reason="outside entry window 10:15–14:15", meta=meta)

        # Both alphas must agree (per published About text)
        bullish = alpha > long_th and alpha2 > long_th
        bearish = alpha < short_th and alpha2 < short_th
        impulse = abs(spot - float(history[-1].close)) if history else 0.0
        min_impulse = float(p.get("min_impulse_pts") or 0)
        if impulse < min_impulse:
            return Signal(
                action=SignalAction.HOLD,
                reason=f"impulse {impulse:.1f} < min {min_impulse:.0f} pts",
                meta=meta,
            )

        if bullish:
            # Credit put spread: sell ATM PE, buy PE at ATM - width
            short_k, long_k = atm, atm - int(width)
            credit = put_credit
            self._open_trade("put_credit", day, atm, short_k, long_k, credit, width)
            meta.update(
                {
                    "structure": "put_credit",
                    "short_leg": f"SELL PE {short_k}",
                    "long_leg": f"BUY PE {long_k}",
                    "fill_price": round(credit, 2),
                    "mark_price": round(credit, 2),
                    "entry_credit": round(credit, 2),
                    "max_loss_pts": round(max(width - credit, 1.0), 2),
                }
            )
            return Signal(
                action=SignalAction.SELL,
                reason=f"bullish alpha={alpha:.2f}/alpha2={alpha2:.2f} → credit put spread ATM/{atm - int(width)}",
                meta=meta,
            )

        if bearish:
            # Credit call spread: sell ATM CE, buy CE at ATM + width
            short_k, long_k = atm, atm + int(width)
            credit = call_credit
            self._open_trade("call_credit", day, atm, short_k, long_k, credit, width)
            meta.update(
                {
                    "structure": "call_credit",
                    "short_leg": f"SELL CE {short_k}",
                    "long_leg": f"BUY CE {long_k}",
                    "fill_price": round(credit, 2),
                    "mark_price": round(credit, 2),
                    "entry_credit": round(credit, 2),
                    "max_loss_pts": round(max(width - credit, 1.0), 2),
                }
            )
            return Signal(
                action=SignalAction.SELL,
                reason=f"bearish alpha={alpha:.2f}/alpha2={alpha2:.2f} → credit call spread ATM/{atm + int(width)}",
                meta=meta,
            )

        return Signal(action=SignalAction.HOLD, reason="alphas not extreme", meta=meta)

    def _open_trade(
        self,
        structure: str,
        day: date,
        atm: int,
        short_k: int,
        long_k: int,
        credit: float,
        width: float,
    ) -> None:
        self._in_trade = True
        self._structure = structure
        self._entry_day = day
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
        self._atm_at_entry = None
        self._short_strike = None
        self._long_strike = None
        self._max_loss_pts = None


def _parse_hhmm(value: str) -> time:
    parts = value.strip().split(":")
    return time(int(parts[0]), int(parts[1]) if len(parts) > 1 else 0)


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


def _seed_prem(spot: float, strike: int, opt: str, iv: float) -> float:
    if opt == "CE":
        intrinsic = max(spot - strike, 0.0)
    else:
        intrinsic = max(strike - spot, 0.0)
    # Extrinsic ~ iv * spot * scale (paper proxy)
    extrinsic = max(5.0, iv * spot * 0.15)
    # Decay OTM distance
    dist = abs(spot - strike)
    extrinsic *= max(0.15, 1.0 - dist / (spot * 0.08 + 1))
    return max(1.0, intrinsic + extrinsic)


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
) -> tuple[float | None, float | None, dict[str, Any]]:
    """Return (alpha, alpha2, aux). None while warming up."""
    need = max(alpha_lookback, alpha2_lookback) + 2
    if len(series) < need:
        return None, None, {"iv_proxy": 0.12}

    # Spot realized vol → IV proxy for synthetic premiums
    closes = [float(b.close) for b in series]
    spot_rets = [
        (closes[i] - closes[i - 1]) / closes[i - 1]
        for i in range(1, len(closes))
        if closes[i - 1] > 0
    ]
    iv = 0.12
    if len(spot_rets) >= 5:
        m = sum(spot_rets[-20:]) / min(20, len(spot_rets))
        var = sum((r - m) ** 2 for r in spot_rets[-20:]) / max(min(20, len(spot_rets)) - 1, 1)
        iv = max((var**0.5) * (252 * 75) ** 0.5, 0.08)  # rough annualization from 5m
        iv = min(iv, 0.45)

    alpha_series: list[float] = []
    alpha2_series: list[float] = []
    ce_prems: list[float] = []
    pe_prems: list[float] = []

    for i in range(1, len(series)):
        spot = closes[i]
        prev = closes[i - 1]
        day_open = _day_open(series, i)
        # alpha feature: 5-minute price change normalized by opening price
        feat = (spot - prev) / day_open if day_open else 0.0

        atm = int(round(spot / strike_step) * strike_step)
        ce = _seed_prem(spot, atm, "CE", iv)
        pe = _seed_prem(spot, atm, "PE", iv)
        ce_prems.append(ce)
        pe_prems.append(pe)

        vol_ce = max(float(series[i].volume), 1.0) * (1.0 + max(spot - prev, 0) / max(spot, 1) * 20)
        vol_pe = max(float(series[i].volume), 1.0) * (1.0 + max(prev - spot, 0) / max(spot, 1) * 20)
        avg_vol_ratio = (vol_ce + vol_pe) / 2.0

        roll_ce = _rolling_vol(ce_prems, roll_vol_bars)
        roll_pe = _rolling_vol(pe_prems, roll_vol_bars)
        atm_vol = roll_ce + roll_pe
        # alpha2 feature: price change × avg ATM PE/CE volume, scaled by ATM vol
        # Use signed change so bullish thrust ranks high with call-heavy volume.
        feat2 = ((spot - prev) * avg_vol_ratio) / max(atm_vol * max(spot, 1.0), 1e-6)

        alpha_series.append(feat)
        alpha2_series.append(feat2)

    # Ranks use trailing windows ending at current bar
    a_win = alpha_series[-alpha_lookback:]
    a2_win = alpha2_series[-alpha2_lookback:]
    alpha = _percentile_rank(a_win, alpha_series[-1])
    alpha2 = _percentile_rank(a2_win, alpha2_series[-1])
    return alpha, alpha2, {"iv_proxy": iv, "atm_ce": ce_prems[-1], "atm_pe": pe_prems[-1]}


def _spread_credits(spot: float, atm: int, width: float, iv: float) -> tuple[float, float]:
    """Net credit ≈ short ATM − long wing."""
    w = int(width)
    pe_atm = _seed_prem(spot, atm, "PE", iv)
    pe_itm = _seed_prem(spot, atm - w, "PE", iv)  # ITM put for bull put spread long leg
    ce_atm = _seed_prem(spot, atm, "CE", iv)
    ce_otm = _seed_prem(spot, atm + w, "CE", iv)
    put_credit = max(1.0, pe_atm - pe_itm)
    call_credit = max(1.0, ce_atm - ce_otm)
    return put_credit, call_credit


def _mark_to_close(
    spot: float,
    *,
    structure: str,
    short_strike: int,
    long_strike: int,
    iv_proxy: float,
) -> float:
    """Debit to close the credit spread (short prem − long prem)."""
    if structure == "put_credit":
        short_p = _seed_prem(spot, short_strike, "PE", iv_proxy)
        long_p = _seed_prem(spot, long_strike, "PE", iv_proxy)
    else:
        short_p = _seed_prem(spot, short_strike, "CE", iv_proxy)
        long_p = _seed_prem(spot, long_strike, "CE", iv_proxy)
    return max(0.5, short_p - long_p)
