from __future__ import annotations

"""Multi-symbol paper runner for NIFTY500 gainer/loser ORB baskets."""

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from algo.config import Settings
from algo.paper.broker import PaperBroker, utcnow
from algo.paper.equity_orb import _EquityOrbBase
from algo.paper.journal import close_trade, open_trade, record_selections
from algo.paper.models import Bar, Position, Side, SignalAction, StrategyState
from algo.paper.quotes import LiveQuoteProvider
from algo.paper.universe import resolve_universe

IST = ZoneInfo("Asia/Kolkata")


class BasketRunner:
    def __init__(
        self,
        *,
        instance_id: str,
        strategy: _EquityOrbBase,
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
            legs.append(
                {
                    "symbol": sym,
                    "qty": pos.quantity,
                    "avg": pos.avg_price,
                    "last": px,
                    "realized": broker.realized_pnl,
                    "unrealized": broker.unrealized_pnl(px),
                    "stop": leg.get("stop"),
                    "target": leg.get("target"),
                    "in_trade": bool(leg.get("in_trade")),
                    "range_high": leg.get("range_high"),
                    "range_low": leg.get("range_low"),
                    "range_done": bool(leg.get("range_done")),
                    "trades_today": int(leg.get("trades_today") or 0),
                    "side": leg.get("side"),
                    "reject_reason": leg.get("reject_reason"),
                    "status": status,
                }
            )
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
            cash=cash,
            starting_cash=self.starting_cash,
            realized_pnl=realized,
            unrealized_pnl=unreal,
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
        return st

    def ensure_selection(self, quotes: LiveQuoteProvider) -> None:
        if self._scanned and self.strategy.selected:
            self.selected = list(self.strategy.selected)
            return
        universe = resolve_universe(self.strategy.params)
        snaps: list[dict[str, Any]] = []
        try:
            batch = quotes.equity_day_snapshots(universe)
            snaps = list(batch.values())
            self._log(
                f"Scan via {quotes.feed_status}: {len(snaps)}/{len(universe)} snapshots"
            )
        except Exception as exc:
            self._log(f"batch scan failed ({exc}); falling back per-symbol")
            for sym in universe:
                try:
                    snap = quotes.equity_day_snapshot(sym)
                    if snap:
                        snaps.append(snap)
                except Exception as skip_exc:
                    self._log(f"scan skip {sym}: {skip_exc}")
        picked = self.strategy.select_symbols(snaps)
        self.selected = list(picked)
        self._scanned = True
        for sym in self.selected:
            if sym not in self.brokers:
                b = PaperBroker(starting_cash=self.starting_cash / max(len(self.selected), 1))
                b.bind(self.strategy.id, f"NSE:EQ:{sym}", sym)
                self.brokers[sym] = b
                self.histories.setdefault(sym, [])
        if self.journal_session_id:
            record_selections(
                session_id=self.journal_session_id,
                strategy_id=self.strategy.id,
                instance_id=self.instance_id,
                rows=getattr(self.strategy, "selection_meta", []),
            )
        self._log(
            f"Selected {len(self.selected)}: {', '.join(self.selected) or 'none'} "
            f"(from {len(snaps)} scanned)"
        )

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

    def tick_live(self, quotes: LiveQuoteProvider) -> list[str]:
        self.ensure_selection(quotes)
        labels = []
        for sym in list(self.selected):
            try:
                price, ts, src = quotes.get_ltp(sym)
                bar = Bar(timestamp=ts, open=price, high=price, low=price, close=price, volume=0)
                self.on_symbol_bar(sym, bar)
                labels.append(f"{sym}={price:.2f}")
            except Exception as exc:
                self._log(f"LTP {sym}: {exc}")
        return labels

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
            order = broker.submit_market(
                strategy_id=self.strategy.id,
                instrument_id=f"NSE:EQ:{symbol}",
                symbol=symbol,
                side=side,
                quantity=abs(pos),
                last_price=fill_px,
                ts=ts,
            )
            if order.status.value == "FILLED":
                pnl_delta = broker.realized_pnl - before
                tid = self.open_trade_ids.pop(symbol, None)
                trade_side = "LONG" if pos > 0 else "SHORT"
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

    def _entry_qty(self, broker: PaperBroker, fill_px: float, signal) -> int:
        """Fixed qty for classic ORB; cash-fit qty for *_cash strategies."""
        wanted = max(int(self.qty), 1)
        if not getattr(self.strategy, "auto_size_cash", False):
            return wanted
        if fill_px <= 0:
            return 0
        affordable = int(broker.cash // (fill_px * 1.001))
        return max(0, min(wanted, affordable))

    def _log(self, message: str) -> None:
        self.logs.append(message)
