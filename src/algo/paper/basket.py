from __future__ import annotations

"""Multi-symbol paper runner for NIFTY500 gainer/loser ORB baskets."""

from datetime import datetime, timedelta, time as time_cls
from typing import Any
from zoneinfo import ZoneInfo
import time

from algo.config import Settings
from algo.paper.broker import PaperBroker, utcnow
from algo.paper.equity_orb import _EquityOrbBase
from algo.paper.equity_orb_tier import _EquityOrbTierBase
from algo.paper.journal import close_trade, list_trades, open_trade, record_selections
from algo.paper.models import Bar, Position, Side, SignalAction, StrategyState
from algo.paper.quotes import LiveQuoteProvider
from algo.paper.universe import resolve_universe

IST = ZoneInfo("Asia/Kolkata")
_BasketStrategy = _EquityOrbBase | _EquityOrbTierBase


class BasketRunner:
    def __init__(
        self,
        *,
        instance_id: str,
        strategy: _BasketStrategy,
        timeframe: str = "1m",
        starting_cash: float = 100_000.0,
        quantity: int | None = None,
        settings: Settings | None = None,
        journal_session_id: str | None = None,
    ) -> None:
        self.instance_id = instance_id
        self.strategy = strategy
        self.timeframe = timeframe
        self.starting_cash = starting_cash
        self.settings = settings
        self.journal_session_id = journal_session_id
        self.enabled = True
        self.logs: list[str] = []
        self.last_signal = "HOLD"
        self.last_bar_at: datetime | None = None
        self.selected: list[str] = []
        self.histories: dict[str, list[Bar]] = {}
        self.brokers: dict[str, PaperBroker] = {}
        self.open_trade_ids: dict[str, str] = {}
        self._scanned = False
        self._desk_day = datetime.now(IST).date()
        self._eod_done_day = None
        self._desk_settled = False
        self._day_pnl = 0.0
        p = {**strategy.default_params(), **strategy.params}
        self.qty = int(quantity if quantity is not None else p.get("qty_per_symbol") or 1)
        self.cash_pool = starting_cash

    def _leg_status(self, symbol: str, leg: dict[str, Any], last: float | None) -> str:
        """Human-readable why a selected name is / isn't in a trade."""
        if leg.get("in_trade"):
            return "in trade"
        reject = leg.get("reject_reason")
        if reject:
            return f"rejected · {reject}"
        if int(leg.get("trades_today") or 0) >= 1:
            return "already traded today"
        if not leg.get("range_done"):
            return "building first range"
        p = {**self.strategy.default_params(), **self.strategy.params}
        try:
            now = datetime.now(IST).time().replace(tzinfo=None)
            end_s = str(p.get("entry_end") or "15:00")
            hh, mm = [int(x) for x in end_s.split(":")[:2]]
            if now.hour > hh or (now.hour == hh and now.minute >= mm):
                return "past entry window"
        except Exception:
            pass
        rh = leg.get("range_high")
        rl = leg.get("range_low")
        mode = getattr(self.strategy, "mode", "loser")
        if rh is not None and rl is not None:
            trigger = float(rl) if mode == "loser" else float(rh)
            if last is not None:
                gap = (float(last) - trigger) if mode == "loser" else (trigger - float(last))
                side = "below low" if mode == "loser" else "above high"
                return f"waiting break {side} {trigger:.2f} · last {float(last):.2f} (Δ {gap:+.2f})"
            return f"waiting break {('below ' + f'{float(rl):.2f}') if mode == 'loser' else ('above ' + f'{float(rh):.2f}')}"
        return "waiting breakout"

    def state(self) -> StrategyState:
        realized = sum(b.realized_pnl for b in self.brokers.values())
        unreal = 0.0
        last_px = None
        fills = []
        orders = []
        legs = []
        cash = self.cash_pool
        n_sel = max(len(self.selected) or len(self.brokers) or 1, 1)
        per_leg_cash = float(self.starting_cash) / n_sel
        for sym, broker in self.brokers.items():
            hist = self.histories.get(sym) or []
            px = hist[-1].close if hist else None
            if px is not None:
                last_px = px
            unreal += broker.unrealized_pnl(px)
            cash = min(cash, broker.cash) if self.brokers else self.cash_pool
            fills.extend(broker.fills[-20:])
            orders.extend(broker.orders[-20:])
            pos = broker.position
            leg = (getattr(self.strategy, "_legs", {}) or {}).get(sym, {})
            status = self._leg_status(sym, leg, px)
            closed = list(getattr(broker, "closed_trades", []) or [])
            last_closed = closed[-1] if closed else None
            in_trade = bool(leg.get("in_trade")) and not self._desk_settled
            # Hide marks only after EOD settle; while WAITING show live LTP so the desk is readable.
            show_px = None if self._desk_settled else px
            filled_qty = 0 if self._desk_settled else int(pos.quantity or 0)
            last_qty = int((last_closed or {}).get("quantity") or 0) if last_closed else 0
            planned_qty = int(leg.get("qty") or 0) if not self._desk_settled else 0
            # Backfill stop/target onto last_closed for legs already exited this session.
            if last_closed is not None and not in_trade:
                if last_closed.get("stop") is None and leg.get("stop") is not None:
                    last_closed["stop"] = leg.get("stop")
                if last_closed.get("target") is None and leg.get("target") is not None:
                    last_closed["target"] = leg.get("target")
                if not last_closed.get("quantity") and leg.get("qty"):
                    last_closed["quantity"] = int(leg.get("qty") or 0)
                # Never allow same-day re-entry once a closed fill exists.
                leg["trades_today"] = max(int(leg.get("trades_today") or 0), 1)
                if leg.get("stop") is None and last_closed.get("stop") is not None:
                    leg["stop"] = last_closed.get("stop")
                if leg.get("target") is None and last_closed.get("target") is not None:
                    leg["target"] = last_closed.get("target")
                if not leg.get("qty") and last_closed.get("quantity"):
                    leg["qty"] = int(last_closed.get("quantity") or 0)
            # Prefer live position qty; else last closed fill; else planned tier qty.
            show_qty = abs(filled_qty) if filled_qty else (last_qty if not in_trade and last_qty else planned_qty)
            # Keep SL/TP visible after exit (review) — only blank after EOD desk settle.
            show_stop = None if self._desk_settled else (
                leg.get("stop")
                if leg.get("stop") is not None
                else (last_closed or {}).get("stop")
            )
            show_target = None if self._desk_settled else (
                leg.get("target")
                if leg.get("target") is not None
                else (last_closed or {}).get("target")
            )
            legs.append(
                {
                    "symbol": sym,
                    "qty": show_qty,
                    "filled_qty": abs(filled_qty),
                    "planned_qty": planned_qty,
                    "avg": pos.avg_price if in_trade else None,
                    "last": show_px,
                    "realized": 0.0 if self._desk_settled else broker.realized_pnl,
                    "unrealized": 0.0 if self._desk_settled else broker.unrealized_pnl(px),
                    "stop": show_stop,
                    "target": show_target,
                    "in_trade": in_trade,
                    "range_high": leg.get("range_high"),
                    "range_low": leg.get("range_low"),
                    "range_done": bool(leg.get("range_done")),
                    "trades_today": int(leg.get("trades_today") or 0),
                    "side": leg.get("side"),
                    "reject_reason": leg.get("reject_reason"),
                    "status": "settled" if self._desk_settled else status,
                    "capital": round(per_leg_cash, 2),
                    "entry": (pos.avg_price if in_trade else (last_closed or {}).get("entry")),
                    "exit": None if in_trade else (last_closed or {}).get("exit"),
                    "leg_pnl": (
                        float(broker.unrealized_pnl(px))
                        if in_trade
                        else (0.0 if self._desk_settled else float((last_closed or {}).get("pnl") or broker.realized_pnl or 0))
                    ),
                    "last_closed": last_closed,
                }
            )
        if self._desk_settled:
            last_px = None
        # Aggregate cash = starting − deployed notionals approx: sum of broker cashes / n
        if self.brokers:
            cash = sum(b.cash for b in self.brokers.values())
        pos0 = Position(
            strategy_id=self.strategy.id,
            instrument_id="BASKET",
            symbol=",".join(self.selected) or "BASKET",
            quantity=sum(abs(b.position.quantity) for b in self.brokers.values()),
            avg_price=0.0,
        )
        st = StrategyState(
            strategy_id=self.strategy.id,
            instance_id=self.instance_id,
            name=self.strategy.name,
            enabled=self.enabled,
            instrument=",".join(self.selected) or "BASKET",
            asset_kind="stock",
            timeframe=self.timeframe,
            quantity=self.qty,
            cash=cash if not self._desk_settled else self.starting_cash + float(self._day_pnl or 0),
            starting_cash=self.starting_cash,
            realized_pnl=0.0 if self._desk_settled else realized,
            unrealized_pnl=0.0 if self._desk_settled else unreal,
            last_price=last_px,
            last_signal=self.last_signal,
            last_bar_at=self.last_bar_at,
            params={
                **self.strategy.params,
                "selected": self.selected,
                "selection": getattr(self.strategy, "selection_meta", []),
                "legs": legs,
            },
            position=pos0,
            fills=fills[-50:],
            orders=orders[-50:],
            logs=list(self.logs[-40:]),
            basket=legs,
        )
        active = [x for x in legs if x.get("in_trade")]
        st.legs_selected = len(self.selected) or len(legs)
        st.legs_in_trade = len(active)
        if len(active) == 1:
            a0 = active[0]
            st.last_price = a0.get("last")
            st.entry_price = a0.get("avg")
            st.stop_price = a0.get("stop")
            st.target_price = a0.get("target")
            st.note = f"{a0.get('symbol')} · {a0.get('status') or 'in trade'}"
        elif len(active) > 1:
            st.note = f"{len(active)} open · {', '.join(x.get('symbol') or '' for x in active[:4])}"
            # Representative levels: leave blank — multi-leg; UI shows "multi"
        elif legs:
            waiting = sum(1 for x in legs if not x.get("in_trade"))
            st.note = f"{waiting} waiting breakout" if waiting else (self.last_signal or "HOLD")
        else:
            st.note = self.last_signal or "HOLD"
        if self._desk_settled:
            st.note = f"settled · day PnL ₹{float(self._day_pnl or 0):,.0f}"
            st.legs_in_trade = 0
            st.last_price = None
            st.entry_price = None
            st.stop_price = None
            st.target_price = None
        closed_all: list[dict] = []
        for b in self.brokers.values():
            closed_all.extend(getattr(b, "closed_trades", []) or [])
        last_closed = closed_all[-1] if closed_all else None
        live_day = float(self._day_pnl or 0) if self._desk_settled else float(realized + unreal)
        st.trade_view = {
            "market": st.last_price,
            "entry": st.entry_price,
            "stop": st.stop_price,
            "target": st.target_price,
            "in_trade": bool(active) and not self._desk_settled,
            "realized_pnl": 0.0 if self._desk_settled else float(realized or 0),
            "unrealized_pnl": 0.0 if self._desk_settled else float(unreal or 0),
            "last_closed": last_closed,
            "closed_count": len(closed_all),
            "open_count": 0 if self._desk_settled else len(active),
            "invested": float(self.starting_cash),
            "equity": float(self.starting_cash) + live_day,
            "day_pnl": live_day,
            "desk_settled": bool(self._desk_settled),
            "generated": float(self.starting_cash) + live_day,
        }
        if (
            not active
            and not self._desk_settled
            and abs(float(realized or 0)) > 1e-9
            and "realized" not in (st.note or "").lower()
        ):
            st.note = f"{st.note} · flat (realized)" if st.note else "flat (realized)"
        return st

    def restore_today_from_journal(self) -> int:
        """Rebuild same-day locks + open/closed levels from journal after process restart.

        Prevents re-entry on symbols already traded today and keeps qty/stop/target visible.
        """
        today = datetime.now(IST).date()

        def _as_day(raw: Any) -> Any:
            if raw is None:
                return None
            if isinstance(raw, datetime):
                return raw.astimezone(IST).date()
            try:
                return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).astimezone(IST).date()
            except Exception:
                return None

        try:
            rows = list_trades(instance_id=self.instance_id, limit=500)
        except Exception as exc:
            self._log(f"journal restore skipped: {exc}")
            return 0

        by_sym: dict[str, list[dict[str, Any]]] = {}
        for t in rows:
            day = _as_day(t.get("exit_at")) or _as_day(t.get("entry_at"))
            if day != today:
                continue
            sym = str(t.get("symbol") or "").upper()
            if not sym:
                continue
            by_sym.setdefault(sym, []).append(t)

        if not by_sym:
            return 0

        # Ensure selection includes journal symbols.
        for sym in by_sym:
            if sym not in self.selected:
                self.selected.append(sym)
        self.strategy.selected = list(self.selected)
        self._scanned = bool(self.selected)
        n_sel = max(len(self.selected) or 1, 1)
        restored = 0

        for sym, trades in by_sym.items():
            closed = [t for t in trades if str(t.get("status") or "") == "closed"]
            opens = [t for t in trades if str(t.get("status") or "") == "open"]
            closed.sort(key=lambda x: str(x.get("exit_at") or x.get("entry_at") or ""), reverse=True)
            opens.sort(key=lambda x: str(x.get("entry_at") or ""), reverse=True)

            broker = self.brokers.setdefault(
                sym,
                PaperBroker(starting_cash=self.starting_cash / n_sel),
            )
            if not broker.position.symbol:
                broker.bind(self.strategy.id, f"NSE:EQ:{sym}", sym)
            self.histories.setdefault(sym, [])
            legs_map = getattr(self.strategy, "_legs", None)
            if not isinstance(legs_map, dict):
                self.strategy._legs = {}  # type: ignore[attr-defined]
                legs_map = self.strategy._legs  # type: ignore[attr-defined]
            empty_fn = getattr(self.strategy, "_empty_leg", None)
            if sym not in legs_map:
                legs_map[sym] = empty_fn() if callable(empty_fn) else {
                    "in_trade": False,
                    "trades_today": 0,
                    "stop": None,
                    "target": None,
                    "qty": 0,
                    "side": None,
                    "entry": None,
                }
            leg = legs_map[sym]

            if closed:
                # Already finished today — lock 1/day and keep levels for review.
                t = closed[0]
                qty = int(t.get("quantity") or (t.get("meta") or {}).get("qty") or 0)
                stop = t.get("stop_price")
                target = t.get("target_price")
                meta = t.get("meta") or {}
                if stop is None:
                    stop = meta.get("stop")
                if target is None:
                    target = meta.get("target")
                leg["in_trade"] = False
                leg["trades_today"] = max(int(leg.get("trades_today") or 0), 1)
                leg["qty"] = qty or leg.get("qty")
                leg["stop"] = float(stop) if stop is not None else leg.get("stop")
                leg["target"] = float(target) if target is not None else leg.get("target")
                leg["side"] = "short" if str(t.get("side") or "").upper() == "SHORT" else "long"
                leg["entry"] = t.get("entry_price")
                broker.closed_trades = [
                    {
                        "pnl": float(t.get("realized_pnl") or 0),
                        "quantity": qty,
                        "entry": float(t.get("entry_price") or 0),
                        "exit": float(t.get("exit_price") or 0),
                        "side": "short" if str(t.get("side") or "").upper() == "SHORT" else "long",
                        "stop": float(stop) if stop is not None else None,
                        "target": float(target) if target is not None else None,
                    }
                ]
                # Drop orphan open journal ids so we don't manage ghost positions.
                self.open_trade_ids.pop(sym, None)
                restored += 1
                continue

            if opens:
                t = opens[0]
                qty = int(t.get("quantity") or (t.get("meta") or {}).get("qty") or 0)
                stop = t.get("stop_price")
                target = t.get("target_price")
                meta = t.get("meta") or {}
                if stop is None:
                    stop = meta.get("stop")
                if target is None:
                    target = meta.get("target")
                entry = float(t.get("entry_price") or 0)
                side = str(t.get("side") or "").upper()
                signed = -abs(qty) if side == "SHORT" else abs(qty)
                broker.position.quantity = signed
                broker.position.avg_price = entry
                broker.position.symbol = sym
                leg["in_trade"] = True
                leg["trades_today"] = max(int(leg.get("trades_today") or 0), 1)
                leg["qty"] = abs(qty)
                leg["stop"] = float(stop) if stop is not None else None
                leg["target"] = float(target) if target is not None else None
                leg["side"] = "short" if side == "SHORT" else "long"
                leg["entry"] = entry
                if t.get("id"):
                    self.open_trade_ids[sym] = str(t["id"])
                restored += 1

        if restored:
            self._log(
                f"Restored {restored} same-day journal leg(s) — qty/stop/target kept; "
                f"1 trade/symbol/day enforced"
            )
        return restored

    def ensure_selection(self, quotes: LiveQuoteProvider) -> None:
        if self._scanned and self.strategy.selected:
            self.selected = list(self.strategy.selected)
            return
        # Back off after a rate-limit — do not re-hammer the universe every poll.
        cool_until = float(getattr(self, "_scan_cool_until", 0.0) or 0.0)
        if cool_until and time.time() < cool_until:
            wait = int(cool_until - time.time())
            self._log(f"Scan cooling {wait}s after Dhan 429")
            return
        p = {**self.strategy.default_params(), **self.strategy.params}
        scan_at_raw = str(p.get("scan_at") or "").strip()
        if scan_at_raw:
            try:
                hh, mm = [int(x) for x in scan_at_raw.split(":")[:2]]
                now = datetime.now(IST).time().replace(tzinfo=None)
                if now < time_cls(hh, mm):
                    self._log(f"Waiting scan_at {scan_at_raw} IST (now {now.strftime('%H:%M:%S')})")
                    return
            except Exception:
                pass
        universe = resolve_universe(self.strategy.params)
        snaps: list[dict[str, Any]] = []
        try:
            # Colleague / Kite pattern: timed REST OHLC for the universe once,
            # then WS only the selected names (session.set_subscriptions).
            batch = quotes.equity_day_snapshots(universe, prefer_rest=True)
            snaps = list(batch.values())
            src = "rest" if any(s.get("source") == "dhan" for s in snaps) else quotes.feed_status
            self._log(
                f"Scan via {src} (REST-first): {len(snaps)}/{len(universe)} snapshots"
            )
        except Exception as exc:
            err = str(exc)
            if "429" in err or "too many" in err.lower():
                self._scan_cool_until = time.time() + 300.0
                self._log(f"batch scan rate-limited — cooling 300s ({exc})")
                return
            self._log(f"batch scan failed ({exc}) — will retry next poll (no per-symbol REST)")
            return
        if len(snaps) < max(5, len(universe) // 10):
            # Too thin to trust rankings — cool briefly, then retry REST.
            self._scan_cool_until = time.time() + 60.0
            self._log(f"Scan incomplete ({len(snaps)} snaps) — waiting before re-scan")
            return
        picked = self.strategy.select_symbols(snaps)
        self.selected = list(picked)
        self._scanned = True
        for sym in self.selected:
            if sym not in self.brokers:
                b = PaperBroker(starting_cash=self.starting_cash / max(len(self.selected), 1))
                b.bind(self.strategy.id, f"NSE:EQ:{sym}", sym)
                self.brokers[sym] = b
                self.histories.setdefault(sym, [])
        self._seed_opening_ranges(quotes, p)
        if self.journal_session_id:
            record_selections(
                session_id=self.journal_session_id,
                strategy_id=self.strategy.id,
                instance_id=self.instance_id,
                rows=getattr(self.strategy, "selection_meta", []),
            )
        self._log(
            f"Selected {len(self.selected)}: {', '.join(self.selected) or 'none'} "
            f"(from {len(snaps)} scanned) — REST seed then WS selected only"
        )
        # Immediately REST-seed selected LTPs so the same poll can trade / show marks.
        try:
            seeded = quotes.seed_ltps_rest_first(self.selected, wait_ws_sec=1.0)
            self._log(f"REST-seeded {len(seeded)}/{len(self.selected)} selected LTPs")
        except Exception as exc:
            self._log(f"REST seed after scan failed: {exc}")
        # Re-apply same-day journal locks after select_symbols rebuilds empty legs.
        try:
            self.restore_today_from_journal()
        except Exception as exc:
            self._log(f"post-scan journal restore failed: {exc}")

    def _seed_opening_ranges(self, quotes: LiveQuoteProvider, params: dict[str, Any]) -> None:
        """Backfill 09:15→now range highs/lows so a 09:18 scan still gets a full first candle."""
        seed_fn = getattr(self.strategy, "seed_range", None)
        if not callable(seed_fn):
            return
        open_s = str(params.get("session_open") or "09:15")
        range_mins = int(params.get("range_minutes") or 5)
        try:
            oh, om = [int(x) for x in open_s.split(":")[:2]]
        except Exception:
            return
        now = datetime.now(IST)
        day = now.date()
        open_dt = datetime.combine(day, time_cls(oh, om), tzinfo=IST)
        range_end = open_dt + timedelta(minutes=range_mins)
        end_dt = min(now, range_end)
        for sym in self.selected:
            try:
                bars = quotes.load_public_bars(sym, interval="1m", range_="1d")
            except Exception as exc:
                self._log(f"range seed skip {sym}: {exc}")
                continue
            highs: list[float] = []
            lows: list[float] = []
            for bar in bars:
                ts = bar.timestamp.astimezone(IST)
                if ts.date() != day:
                    continue
                if open_dt <= ts <= end_dt:
                    highs.append(float(bar.high))
                    lows.append(float(bar.low))
            if highs and lows:
                seed_fn(sym, max(highs), min(lows))
                self._log(f"Seeded {sym} range H={max(highs):.2f} L={min(lows):.2f}")

    def on_symbol_bar(self, symbol: str, bar: Bar) -> None:
        if not self.enabled:
            return
        symbol = symbol.upper()
        if symbol not in self.selected:
            return
        broker = self.brokers.setdefault(
            symbol,
            PaperBroker(starting_cash=self.starting_cash / max(len(self.selected) or 1, 1)),
        )
        if not broker.position.symbol:
            broker.bind(self.strategy.id, f"NSE:EQ:{symbol}", symbol)
        hist = self.histories.setdefault(symbol, [])
        # Same-timestamp replace
        if hist and hist[-1].timestamp == bar.timestamp:
            signal = self.strategy.on_symbol_bar(symbol, bar, hist[:-1])
            hist[-1] = bar
        else:
            signal = self.strategy.on_symbol_bar(symbol, bar, hist)
            hist.append(bar)
            if len(hist) > 2000:
                self.histories[symbol] = hist[-1200:]
        self.last_signal = signal.action.value
        self.last_bar_at = bar.timestamp
        fill_px = float(signal.meta.get("fill_price") or bar.close)
        self._log(
            f"{bar.timestamp.astimezone(IST).strftime('%H:%M:%S')} {symbol} "
            f"{signal.action.value} @ {fill_px:.2f} — {signal.reason}"
        )
        self._apply(symbol, broker, signal, fill_px, bar.timestamp)

    def tick_live(
        self, quotes: LiveQuoteProvider, *, skip_selection: bool = False
    ) -> list[str]:
        # Migrate older saved flatten_at=15:20 → 15:00 IST
        flat = str(self.strategy.params.get("flatten_at") or "").strip()
        if flat in {"15:20", "15:25", "15:30"}:
            self.strategy.params["flatten_at"] = "15:00"
        if not skip_selection:
            self._maybe_roll_trading_day()
            self.ensure_selection(quotes)
        if not getattr(self, "_journal_restored", False):
            try:
                self.restore_today_from_journal()
            except Exception:
                pass
            self._journal_restored = True
        self._maybe_eod_flatten(quotes)
        labels = []
        # Marks should already be REST-seeded + WS-subscribed by session.
        for sym in list(self.selected):
            try:
                price, ts, src = quotes.get_ltp(
                    sym, allow_rest=False, wait_ws_sec=0.2, max_stale_sec=300
                )
                bar = Bar(timestamp=ts, open=price, high=price, low=price, close=price, volume=0)
                self.on_symbol_bar(sym, bar)
                labels.append(f"{sym}={price:.2f}")
            except Exception as exc:
                err = str(exc)
                if "429" in err or "too many" in err.lower() or "rate limited" in err.lower():
                    self._log(f"LTP {sym}: waiting for dhan-ws (429 cool-down)")
                else:
                    self._log(f"LTP {sym}: {exc}")
        return labels

    def _maybe_roll_trading_day(self) -> None:
        """New IST day → clear selection + desk PnL counters (journal history kept)."""
        today = datetime.now(IST).date()
        if self._desk_day == today:
            return
        self._log(
            f"New trading day {today.isoformat()} — resetting desk selection/PnL "
            f"(prior day journal kept)"
        )
        keep_params = dict(self.strategy.params)
        self.strategy.reset()
        self.strategy.params = keep_params
        self.selected = []
        self.brokers = {}
        self.histories = {}
        self.open_trade_ids = {}
        self._scanned = False
        self._eod_done_day = None
        self._desk_settled = False
        self._day_pnl = 0.0
        self._desk_day = today

    def _settle_desk_display(self) -> None:
        """After EOD: lock day PnL, zero Execute row PnL (journal kept)."""
        if self._desk_settled:
            return
        realized = sum(b.realized_pnl for b in self.brokers.values())
        self._day_pnl = float(realized)
        n = max(len(self.selected) or 1, 1)
        fresh: dict[str, PaperBroker] = {}
        for sym in self.selected:
            b = PaperBroker(starting_cash=self.starting_cash / n)
            b.bind(self.strategy.id, f"NSE:EQ:{sym}", sym)
            fresh[sym] = b
        self.brokers = fresh
        self.open_trade_ids = {}
        self._desk_settled = True
        self._log(
            f"Desk settled — day PnL ₹{self._day_pnl:,.2f} shown as Generated; "
            f"Execute row PnL cleared to 0 (journal kept)"
        )

    def _eod_exit_ts(self, flat_raw: str) -> datetime:
        """Use flatten clock (IST) for journal exit_at — not wall clock if we ran late."""
        now = datetime.now(IST)
        try:
            hh, mm = [int(x) for x in str(flat_raw).split(":")[:2]]
        except Exception:
            hh, mm = 15, 0
        return datetime.combine(now.date(), time_cls(hh, mm), tzinfo=IST)

    def _maybe_eod_flatten(self, quotes: LiveQuoteProvider) -> None:
        """Force-close open intraday legs at flatten_at (default 15:00 IST)."""
        p = {**self.strategy.default_params(), **self.strategy.params}
        flat_raw = str(p.get("flatten_at") or "").strip()
        if not flat_raw:
            return
        today = datetime.now(IST).date()
        if self._eod_done_day == today and self._desk_settled:
            return
        try:
            hh, mm = [int(x) for x in flat_raw.split(":")[:2]]
            now = datetime.now(IST).time().replace(tzinfo=None)
            if now < time_cls(hh, mm):
                return
        except Exception:
            return
        open_syms = [
            sym
            for sym, broker in self.brokers.items()
            if broker.position.quantity
        ]
        # Late wake/sleep must not stamp exits at 17:xx — journal uses flatten clock.
        exit_ts = self._eod_exit_ts(flat_raw)
        if open_syms:
            self._log(
                f"EOD flatten @ {flat_raw} IST — closing {len(open_syms)} open leg(s) "
                f"(exit_at={exit_ts.strftime('%H:%M:%S')} IST)"
            )
            from algo.paper.models import Signal

            for sym in open_syms:
                try:
                    price, _ts, _src = quotes.get_ltp(
                        sym, allow_rest=True, wait_ws_sec=0.5, max_stale_sec=600
                    )
                except Exception:
                    continue
                broker = self.brokers[sym]
                sig = Signal(
                    action=SignalAction.FLAT,
                    reason=f"EOD flatten @ {flat_raw} IST",
                    meta={
                        "structure": "eod",
                        "fill_price": float(price),
                        "symbol": sym,
                        "exit_at_policy": exit_ts.isoformat(),
                    },
                )
                self._apply(sym, broker, sig, float(price), exit_ts)
                leg = (getattr(self.strategy, "_legs", {}) or {}).get(sym)
                if isinstance(leg, dict):
                    leg["in_trade"] = False
                self._preserve_exit_levels(
                    sym,
                    broker,
                    {
                        "stop": (leg or {}).get("stop") if isinstance(leg, dict) else None,
                        "target": (leg or {}).get("target") if isinstance(leg, dict) else None,
                        "qty": (leg or {}).get("qty") if isinstance(leg, dict) else None,
                    },
                )
            self.last_signal = "EOD_FLAT"
        self._eod_done_day = today
        self._settle_desk_display()

    def run_replay(self, quotes: LiveQuoteProvider, max_bars: int | None = 500) -> None:
        self.ensure_selection(quotes)
        # Align bars by timestamp across symbols
        series: dict[str, list[Bar]] = {}
        for sym in self.selected:
            try:
                bars = quotes.load_public_bars(sym, interval="1m", range_="5d")
                series[sym] = bars[-max_bars:] if max_bars else bars
            except Exception:
                series[sym] = quotes.synthetic_bars(sym, n=max_bars or 300)
        # Replay chronologically per symbol (simple)
        for sym, bars in series.items():
            for bar in bars:
                self.on_symbol_bar(sym, bar)

    def _apply(self, symbol: str, broker: PaperBroker, signal, fill_px: float, ts: datetime) -> None:
        pos = broker.position.quantity
        qty = self._entry_qty(broker, fill_px, signal)
        meta = signal.meta or {}
        autosize = bool(getattr(self.strategy, "auto_size_cash", False))
        if signal.action is SignalAction.BUY and pos <= 0:
            if qty <= 0:
                if autosize and meta.get("structure") == "long_orb":
                    self.strategy.rollback_entry(symbol, reason="insufficient cash for 1 share")
                    self._log(f"{symbol} BUY skipped — insufficient cash @ {fill_px:.2f}")
                return
            need = qty + abs(min(pos, 0))
            before = broker.realized_pnl
            order = broker.submit_market(
                strategy_id=self.strategy.id,
                instrument_id=f"NSE:EQ:{symbol}",
                symbol=symbol,
                side=Side.BUY,
                quantity=need if pos < 0 else qty,
                last_price=fill_px,
                ts=ts,
            )
            if order.status.value == "REJECTED":
                if autosize and meta.get("structure") == "long_orb":
                    self.strategy.rollback_entry(
                        symbol, reason=order.reject_reason or "buy rejected"
                    )
                    self._log(f"{symbol} BUY rejected — {order.reject_reason}; rolled back")
                return
            if order.status.value == "FILLED" and pos <= 0 and meta.get("structure") == "long_orb":
                if autosize:
                    self._log(f"{symbol} BUY sized qty={qty} @ {order.fill_price or fill_px:.2f}")
                if self.journal_session_id:
                    tid = open_trade(
                        session_id=self.journal_session_id,
                        strategy_id=self.strategy.id,
                        instance_id=self.instance_id,
                        symbol=symbol,
                        side="LONG",
                        quantity=qty,
                        entry_price=float(order.fill_price or fill_px),
                        stop_price=meta.get("stop"),
                        target_price=meta.get("target"),
                        entry_at=ts,
                        reason=signal.reason,
                        meta={**meta, "qty": qty},
                        fee=float(broker.fills[-1].fee if broker.fills else 0),
                    )
                    self.open_trade_ids[symbol] = tid
            elif order.status.value == "FILLED" and pos < 0:
                pnl_delta = broker.realized_pnl - before
                self._preserve_exit_levels(symbol, broker, meta)
                tid = self.open_trade_ids.pop(symbol, None)
                if self.journal_session_id and tid:
                    close_trade(
                        trade_id=tid,
                        session_id=self.journal_session_id,
                        strategy_id=self.strategy.id,
                        instance_id=self.instance_id,
                        symbol=symbol,
                        side="SHORT",
                        quantity=qty,
                        exit_price=float(order.fill_price or fill_px),
                        exit_at=ts,
                        realized_pnl=pnl_delta,
                        reason=signal.reason,
                        meta=meta,
                    )
        elif signal.action is SignalAction.SELL and pos >= 0:
            if qty <= 0 and pos == 0:
                if autosize and meta.get("structure") == "short_orb":
                    self.strategy.rollback_entry(symbol, reason="insufficient cash for 1 share")
                    self._log(f"{symbol} SELL skipped — insufficient cash @ {fill_px:.2f}")
                return
            sell_qty = qty if pos == 0 else pos
            before = broker.realized_pnl
            order = broker.submit_market(
                strategy_id=self.strategy.id,
                instrument_id=f"NSE:EQ:{symbol}",
                symbol=symbol,
                side=Side.SELL,
                quantity=sell_qty,
                last_price=fill_px,
                ts=ts,
            )
            if order.status.value == "REJECTED":
                if autosize and meta.get("structure") == "short_orb" and pos == 0:
                    self.strategy.rollback_entry(
                        symbol, reason=order.reject_reason or "sell rejected"
                    )
                    self._log(f"{symbol} SELL rejected — {order.reject_reason}; rolled back")
                return
            if order.status.value == "FILLED" and pos == 0 and meta.get("structure") == "short_orb":
                if autosize:
                    self._log(f"{symbol} SHORT sized qty={qty} @ {order.fill_price or fill_px:.2f}")
                if self.journal_session_id:
                    tid = open_trade(
                        session_id=self.journal_session_id,
                        strategy_id=self.strategy.id,
                        instance_id=self.instance_id,
                        symbol=symbol,
                        side="SHORT",
                        quantity=qty,
                        entry_price=float(order.fill_price or fill_px),
                        stop_price=meta.get("stop"),
                        target_price=meta.get("target"),
                        entry_at=ts,
                        reason=signal.reason,
                        meta={**meta, "qty": qty},
                        fee=float(broker.fills[-1].fee if broker.fills else 0),
                    )
                    self.open_trade_ids[symbol] = tid
            elif order.status.value == "FILLED" and pos > 0:
                pnl_delta = broker.realized_pnl - before
                self._preserve_exit_levels(symbol, broker, meta)
                tid = self.open_trade_ids.pop(symbol, None)
                if self.journal_session_id and tid:
                    close_trade(
                        trade_id=tid,
                        session_id=self.journal_session_id,
                        strategy_id=self.strategy.id,
                        instance_id=self.instance_id,
                        symbol=symbol,
                        side="LONG",
                        quantity=abs(pos),
                        exit_price=float(order.fill_price or fill_px),
                        exit_at=ts,
                        realized_pnl=pnl_delta,
                        reason=signal.reason,
                        meta=meta,
                    )
        elif signal.action is SignalAction.FLAT and pos != 0:
            before = broker.realized_pnl
            side = Side.SELL if pos > 0 else Side.BUY
            close_frac = meta.get("close_frac")
            if close_frac is not None and 0 < float(close_frac) < 1:
                qty_close = max(1, int(round(abs(pos) * float(close_frac))))
                qty_close = min(qty_close, abs(pos) - 1) if abs(pos) > 1 else abs(pos)
            else:
                qty_close = abs(pos)
            order = broker.submit_market(
                strategy_id=self.strategy.id,
                instrument_id=f"NSE:EQ:{symbol}",
                symbol=symbol,
                side=side,
                quantity=qty_close,
                last_price=fill_px,
                ts=ts,
            )
            if order.status.value == "FILLED":
                pnl_delta = broker.realized_pnl - before
                trade_side = "LONG" if pos > 0 else "SHORT"
                still_open = broker.position.quantity != 0
                if still_open:
                    # Partial scale-out at R1 — keep journal trade open until final exit.
                    self._log(
                        f"{symbol} scaled out qty={qty_close} @ {order.fill_price or fill_px:.2f} "
                        f"(left {broker.position.quantity})"
                    )
                    return
                # Full exit (incl. tiny positions where partial collapsed to 100%)
                leg = (getattr(self.strategy, "_legs", {}) or {}).get(symbol)
                if isinstance(leg, dict):
                    leg["in_trade"] = False
                self._preserve_exit_levels(symbol, broker, meta, quantity=abs(pos))
                tid = self.open_trade_ids.pop(symbol, None)
                if self.journal_session_id and tid:
                    close_trade(
                        trade_id=tid,
                        session_id=self.journal_session_id,
                        strategy_id=self.strategy.id,
                        instance_id=self.instance_id,
                        symbol=symbol,
                        side=trade_side,
                        quantity=abs(pos),
                        exit_price=float(order.fill_price or fill_px),
                        exit_at=ts,
                        realized_pnl=pnl_delta,
                        reason=signal.reason,
                        meta=meta,
                    )

    def _preserve_exit_levels(
        self,
        symbol: str,
        broker: PaperBroker,
        meta: dict[str, Any],
        *,
        quantity: int | None = None,
    ) -> None:
        """Keep qty/stop/target on the closed trade + leg for desk review (no same-day re-entry)."""
        leg = (getattr(self.strategy, "_legs", {}) or {}).get(symbol) or {}
        stop = meta.get("stop")
        if stop is None:
            stop = leg.get("stop")
        target = meta.get("target")
        if target is None:
            target = leg.get("target")
        qty = quantity
        if qty is None:
            qty = meta.get("qty") or leg.get("qty")
        broker.annotate_last_closed(
            stop=float(stop) if stop is not None else None,
            target=float(target) if target is not None else None,
            quantity=int(qty) if qty is not None else None,
        )
        # Keep levels on the leg so Execute can show them after DONE (1/day lock still applies).
        if isinstance(leg, dict):
            if stop is not None:
                leg["stop"] = float(stop)
            if target is not None:
                leg["target"] = float(target)
            if qty is not None:
                leg["qty"] = int(qty)

    def _entry_qty(self, broker: PaperBroker, fill_px: float, signal) -> int:
        """Tiered qty (brother strategies), fixed qty, or cash-fit for *_cash."""
        meta = signal.meta or {}
        if meta.get("qty"):
            wanted = max(int(meta["qty"]), 1)
        elif getattr(self.strategy, "use_price_tiers", False) and hasattr(self.strategy, "qty_for_price"):
            wanted = max(int(self.strategy.qty_for_price(fill_px)), 1)
        else:
            wanted = max(int(self.qty), 1)
        if not getattr(self.strategy, "auto_size_cash", False):
            return wanted
        if fill_px <= 0:
            return 0
        affordable = int(broker.cash // (fill_px * 1.001))
        return max(0, min(wanted, affordable))

    def _log(self, message: str) -> None:
        self.logs.append(message)
