from __future__ import annotations

import threading
import time
from datetime import date, datetime, time as time_cls
from typing import Any
from zoneinfo import ZoneInfo

from algo.analysis.load import load_candles
from algo.config import Settings, get_settings
from algo.paper.basket import BasketRunner
from algo.paper.broker import PaperBroker, utcnow
from algo.paper.equity_orb import (
    Nifty500GainerOrbCashStrategy,
    Nifty500GainerOrbStrategy,
    Nifty500LoserOrbCashStrategy,
    Nifty500LoserOrbStrategy,
    _EquityOrbBase,
)
from algo.paper.equity_orb_tier import (
    Nifty500TopGainerBrkStrategy,
    Nifty500TopLoserBrkStrategy,
    _EquityOrbTierBase,
)
from algo.paper.journal import end_journal_session, list_open_trades, start_journal_session
from algo.paper.models import Bar, SessionSnapshot, Side, SignalAction, StrategyState
from algo.paper.quotes import LiveQuoteProvider, suggested_poll_seconds
from algo.paper.registry import create_strategy, list_strategies
from algo.paper.strategy import Strategy
from algo.storage.db import get_engine
from algo.storage.repositories import get_instrument_by_symbol
from algo.storage.db import session_scope

IST = ZoneInfo("Asia/Kolkata")

BASKET_IDS = {
    Nifty500GainerOrbStrategy.id,
    Nifty500LoserOrbStrategy.id,
    Nifty500GainerOrbCashStrategy.id,
    Nifty500LoserOrbCashStrategy.id,
    Nifty500TopGainerBrkStrategy.id,
    Nifty500TopLoserBrkStrategy.id,
}


def _normalize_strategy_params(params: dict[str, Any] | None) -> dict[str, Any]:
    """Keep intraday square-off at 15:00 IST (migrate older 15:20 desk saves)."""
    p = dict(params or {})
    flat = str(p.get("flatten_at") or "").strip()
    if flat in {"15:20", "15:25", "15:30"}:
        p["flatten_at"] = "15:00"
    if str(p.get("entry_end") or "").strip() == "15:15":
        p["entry_end"] = "15:00"
    return p


def _parse_iso(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    try:
        text = str(value).replace("Z", "+00:00")
        return datetime.fromisoformat(text)
    except Exception:
        return None


def _broker_snapshot(broker: PaperBroker) -> dict[str, Any]:
    return {
        "cash": broker.cash,
        "starting_cash": broker.starting_cash,
        "realized_pnl": broker.realized_pnl,
        "quantity": broker.position.quantity,
        "avg_price": broker.position.avg_price,
        "symbol": broker.position.symbol,
        "instrument_id": broker.position.instrument_id,
        "strategy_id": broker.position.strategy_id,
    }


def _apply_broker_snapshot(broker: PaperBroker, snap: dict[str, Any], *, strategy_id: str, symbol: str, instrument_id: str) -> None:
    broker.starting_cash = float(snap.get("starting_cash") or broker.starting_cash)
    broker.cash = float(snap.get("cash") if snap.get("cash") is not None else broker.starting_cash)
    broker.realized_pnl = float(snap.get("realized_pnl") or 0.0)
    broker.bind(strategy_id, instrument_id or f"NSE:EQ:{symbol}", symbol)
    broker.position.quantity = int(snap.get("quantity") or 0)
    broker.position.avg_price = float(snap.get("avg_price") or 0.0)


class StrategyRunner:
    def __init__(
        self,
        *,
        instance_id: str,
        strategy: Strategy,
        symbol: str,
        instrument_id: str,
        timeframe: str,
        quantity: int,
        starting_cash: float,
        asset_kind: str = "index",
        fee_bps: float = 1.0,
        slippage_bps: float = 2.0,
    ) -> None:
        self.instance_id = instance_id
        self.strategy = strategy
        self.symbol = symbol.upper()
        self.instrument_id = instrument_id
        self.timeframe = timeframe
        self.quantity = quantity
        self.asset_kind = asset_kind
        self.broker = PaperBroker(
            starting_cash=starting_cash,
            fee_bps=fee_bps,
            slippage_bps=slippage_bps,
        )
        self.broker.bind(strategy.id, instrument_id, self.symbol)
        self.history: list[Bar] = []
        self.logs: list[str] = []
        self.last_signal = "HOLD"
        self.last_bar_at: datetime | None = None
        self.enabled = True
        self.option_state: dict[str, float] = {}
        self.mark_price: float | None = None
        self._forming_bar: Bar | None = None
        self._desk_day = datetime.now(IST).date()

    def state(self) -> StrategyState:
        last = self.mark_price
        if last is None:
            last = self.history[-1].close if self.history else None
        st = StrategyState(
            strategy_id=self.strategy.id,
            instance_id=self.instance_id,
            name=self.strategy.name,
            enabled=self.enabled,
            instrument=self.symbol,
            asset_kind=self.asset_kind,
            timeframe=self.timeframe,
            quantity=self.quantity,
            params=self.strategy.params,
            last_signal=self.last_signal,
            last_bar_at=self.last_bar_at,
            logs=list(self.logs[-30:]),
        )
        st = self.broker.snapshot(st, last)
        st.note = self.last_signal or ""
        strat = self.strategy
        if getattr(strat, "_in_trade", False) and getattr(strat, "_entry_price", None) is not None:
            entry = float(strat._entry_price)
            p = {**strat.default_params(), **strat.params}
            stop_pts = float(p.get("stop_points") or 0)
            target_pts = p.get("target_points")
            if target_pts in (None, "", 0, "0"):
                target_pts = stop_pts * float(p.get("risk_reward") or 2)
            else:
                target_pts = float(target_pts)
            st.entry_price = entry
            st.stop_price = round(entry - stop_pts, 2)
            st.target_price = round(entry + target_pts, 2)
            st.legs_in_trade = 1
            st.note = f"{self.last_signal} · in trade"
        elif self.broker.position.quantity:
            st.entry_price = self.broker.position.avg_price or None
            st.legs_in_trade = 1
            p = {**strat.default_params(), **getattr(strat, "params", {})}
            if p.get("stop_points") is not None and st.entry_price:
                stop_pts = float(p["stop_points"])
                target_pts = p.get("target_points")
                if target_pts in (None, "", 0, "0"):
                    target_pts = stop_pts * float(p.get("risk_reward") or 2)
                else:
                    target_pts = float(target_pts)
                qty = self.broker.position.quantity
                if qty > 0:
                    st.stop_price = round(st.entry_price - stop_pts, 2)
                    st.target_price = round(st.entry_price + target_pts, 2)
                else:
                    st.stop_price = round(st.entry_price + stop_pts, 2)
                    st.target_price = round(st.entry_price - target_pts, 2)
        # Plain trade view for the desk / popup (option ORB + others).
        p_all = {**getattr(strat, "default_params", lambda: {})(), **getattr(strat, "params", {})}
        opt = self.option_state or {}
        closed = list(getattr(self.broker, "closed_trades", []) or [])
        last_closed = closed[-1] if closed else None
        in_trade = bool(st.legs_in_trade or (self.broker.position.quantity))
        st.trade_view = {
            "market": last if last is not None else st.last_price,
            "entry": st.entry_price,
            "stop": st.stop_price,
            "target": st.target_price,
            "in_trade": in_trade,
            "realized_pnl": float(st.realized_pnl or 0),
            "unrealized_pnl": float(st.unrealized_pnl or 0),
            "option_type": p_all.get("option_type") or getattr(strat, "locked_option_type", None),
            "strike": int(opt["strike"]) if opt.get("strike") else getattr(strat, "_selected_strike", None),
            "spot": opt.get("spot") or opt.get("prev_spot"),
            "premium": opt.get("premium"),
            "range_high": getattr(strat, "_range_high", None),
            "stop_points": p_all.get("stop_points"),
            "target_points": p_all.get("target_points"),
            "hold_minutes": p_all.get("hold_minutes"),
            "range_minutes": p_all.get("range_minutes"),
            "session_open": p_all.get("session_open"),
            "last_closed": last_closed,
            "closed_count": len(closed),
        }
        if not in_trade and last_closed:
            # Desk shows last closed levels so PnL isn't "mystery money".
            st.note = st.note or self.last_signal or "HOLD"
            if "flat" not in (st.note or "").lower() and "waiting" not in (st.note or "").lower():
                if abs(float(st.realized_pnl or 0)) > 1e-9:
                    st.note = f"{st.note} · flat (realized)"
                else:
                    st.note = f"{st.note} · flat (last closed)"
        day_pnl = float(st.realized_pnl or 0) + float(st.unrealized_pnl or 0)
        invested = float(st.starting_cash or 0)
        st.trade_view = {
            **(st.trade_view or {}),
            "invested": invested,
            "equity": invested + day_pnl,
            "day_pnl": day_pnl,
            "generated": invested + day_pnl,
            "desk_settled": False,
        }
        return st

    def on_bar(self, bar: Bar) -> None:
        if not self.enabled:
            return
        today = bar.timestamp.astimezone(IST).date()
        if self._desk_day != today:
            # New IST day — reset desk counters; journal history stays in DB.
            if self.broker.position.quantity == 0:
                self.strategy.reset()
                self.broker = PaperBroker(
                    starting_cash=self.broker.starting_cash,
                    fee_bps=self.broker.fee_bps,
                    slippage_bps=self.broker.slippage_bps,
                )
                self.broker.bind(self.strategy.id, self.instrument_id, self.symbol)
                self.history = []
                self.last_signal = "HOLD"
                self._log(f"New trading day {today.isoformat()} — desk PnL reset")
            self._desk_day = today
        # Same 5m bucket updates (live Zen): replace last bar instead of duplicating.
        if self.history and self.history[-1].timestamp == bar.timestamp:
            hist = self.history[:-1]
            signal = self.strategy.on_bar(bar, hist)
            self.history[-1] = bar
        else:
            signal = self.strategy.on_bar(bar, self.history)
            self.history.append(bar)
            if len(self.history) > 5000:
                self.history = self.history[-3000:]
        self.last_signal = signal.action.value
        self.last_bar_at = bar.timestamp
        if signal.meta.get("mark_price") is not None:
            self.mark_price = float(signal.meta["mark_price"])
        fill_px = float(signal.meta["fill_price"]) if signal.meta.get("fill_price") is not None else float(bar.close)
        self._log(
            f"{bar.timestamp.astimezone(IST).strftime('%H:%M:%S')} "
            f"{signal.action.value} @ {fill_px:.2f} — {signal.reason}"
        )
        pos_qty = self.broker.position.quantity
        if signal.action is SignalAction.BUY and pos_qty <= 0:
            qty = self.quantity + abs(min(pos_qty, 0))
            self.broker.submit_market(
                strategy_id=self.strategy.id,
                instrument_id=self.instrument_id,
                symbol=self.symbol,
                side=Side.BUY,
                quantity=qty,
                last_price=fill_px,
                ts=bar.timestamp,
            )
        elif signal.action is SignalAction.SELL and pos_qty >= 0:
            qty = self.quantity + max(pos_qty, 0)
            if qty > 0:
                self.broker.submit_market(
                    strategy_id=self.strategy.id,
                    instrument_id=self.instrument_id,
                    symbol=self.symbol,
                    side=Side.SELL,
                    quantity=qty if pos_qty > 0 else self.quantity,
                    last_price=fill_px,
                    ts=bar.timestamp,
                )
        elif signal.action is SignalAction.FLAT and pos_qty != 0:
            side = Side.SELL if pos_qty > 0 else Side.BUY
            self.broker.submit_market(
                strategy_id=self.strategy.id,
                instrument_id=self.instrument_id,
                symbol=self.symbol,
                side=side,
                quantity=abs(pos_qty),
                last_price=fill_px,
                ts=bar.timestamp,
            )

    def _log(self, message: str) -> None:
        self.logs.append(message)


class PaperSession:
    """In-memory multi-strategy paper session (singleton via get_session)."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.mode = "idle"
        self.running = False
        self.started_at: datetime | None = None
        self.updated_at: datetime | None = None
        self.message = "Ready. Add a strategy and press Start."
        self.runners: dict[str, StrategyRunner | BasketRunner] = {}
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self.poll_seconds = 15.0
        self._quotes: LiveQuoteProvider | None = None
        self.journal_session_id: str | None = None
        self._restoring = False
        self._resume_prefs: dict[str, Any] = {
            "mode": "live",
            "poll_seconds": 15.0,
            "was_running": False,
        }
        self._signals_installed = False
        self._runtime_tick = 0
        # REST→WS feed policy: seed once per selected set, re-seed if coverage drops.
        self._rest_seeded_day: date | None = None
        self._rest_seeded_syms: frozenset[str] = frozenset()
        self._feed_phase: str = "idle"  # idle | rest_ok | ws_live

    def available_strategies(self) -> list[dict]:
        return list_strategies()

    def add_strategy(
        self,
        strategy_id: str,
        *,
        symbol: str = "NIFTY",
        timeframe: str = "5m",
        quantity: int = 1,
        starting_cash: float = 100_000.0,
        params: dict[str, Any] | None = None,
        enabled: bool = True,
        asset_kind: str | None = None,
        instance_id: str | None = None,
    ) -> StrategyState:
        with self._lock:
            strategy = create_strategy(strategy_id, params=_normalize_strategy_params(params))
            params = dict(strategy.params)
            strategy.params = _normalize_strategy_params(params)
            params = dict(strategy.params)
            if strategy_id in BASKET_IDS:
                timeframe = "1m"
                key = instance_id or f"{strategy_id}:BASKET"
                if key in self.runners:
                    raise ValueError(f"Strategy instance already on desk: {key}")
                get_engine(self.settings)
                assert isinstance(strategy, (_EquityOrbBase, _EquityOrbTierBase))
                runner = BasketRunner(
                    instance_id=key,
                    strategy=strategy,
                    timeframe=timeframe,
                    starting_cash=starting_cash,
                    quantity=int(params.get("qty_per_symbol") or quantity),
                    settings=self.settings,
                    journal_session_id=self.journal_session_id,
                )
                runner.enabled = enabled
                self.runners[key] = runner
                self.message = f"Added {strategy.name} ({key})"
                self.persist_desk()
                return runner.state()

            kind = (asset_kind or ("option" if "option" in strategy.asset_kinds else "index")).lower()
            # Zen credit spread requires 5m NIFTY-style bars for alpha lookbacks.
            if strategy.id == "zen_credit_spread":
                timeframe = "5m"
                kind = "index"
                if "bar_minutes" not in params:
                    params["bar_minutes"] = 5
                    strategy.params = params
            opt = str(params.get("option_type", "CE")).upper()
            strike_mode = str(params.get("strike_mode", "ATM"))
            key = instance_id or f"{strategy_id}:{symbol.upper()}:{kind}:{opt}:{strike_mode}"
            if strategy.id == "zen_credit_spread":
                key = instance_id or f"{strategy_id}:{symbol.upper()}:credit"
            if key in self.runners:
                raise ValueError(f"Strategy instance already on desk: {key}")
            get_engine(self.settings)
            if kind == "stock":
                instrument_id = f"NSE:EQ:{symbol.upper()}"
            elif kind == "option":
                instrument_id = f"NSE:OPT:{symbol.upper()}:{opt}:{strike_mode}"
            else:
                instrument_id = f"NSE:INDEX:{symbol.upper()}"
            with session_scope(self.settings) as session:
                row = get_instrument_by_symbol(session, symbol)
                if row is not None and kind != "option":
                    instrument_id = row.id
            # Force strike_step default for BANKNIFTY
            if kind == "option" and "strike_step" not in params:
                params["strike_step"] = 100 if symbol.upper() == "BANKNIFTY" else 50
                strategy.params = params
            runner = StrategyRunner(
                instance_id=key,
                strategy=strategy,
                symbol=symbol,
                instrument_id=instrument_id,
                timeframe=timeframe,
                quantity=quantity,
                starting_cash=starting_cash,
                asset_kind=kind,
            )
            runner.enabled = enabled
            self.runners[key] = runner
            self.message = f"Added {strategy.name} ({key})"
            self.persist_desk()
            return runner.state()

    def remove_strategy(self, strategy_id: str) -> None:
        with self._lock:
            if strategy_id in self.runners:
                self.runners.pop(strategy_id, None)
                self.persist_desk()
                return
            matches = [k for k, r in self.runners.items() if r.strategy.id == strategy_id]
            if len(matches) == 1:
                self.runners.pop(matches[0], None)
                self.persist_desk()
                return
            raise ValueError(f"Unknown strategy instance {strategy_id}")

    def set_enabled(self, strategy_id: str, enabled: bool) -> None:
        with self._lock:
            runner = self.runners.get(strategy_id)
            if runner is None:
                matches = [r for r in self.runners.values() if r.strategy.id == strategy_id]
                if len(matches) == 1:
                    runner = matches[0]
            if runner is None:
                raise ValueError(f"Unknown strategy instance {strategy_id}")
            runner.enabled = enabled
            self.persist_desk()
            self.message = f"{'Started' if enabled else 'Paused'} {runner.strategy.name}"

    def desk_configs(self) -> list[dict[str, Any]]:
        """Serializable desk rows for disk persistence."""
        rows = []
        for runner in self.runners.values():
            if isinstance(runner, BasketRunner):
                rows.append(
                    {
                        "strategy_id": runner.strategy.id,
                        "instance_id": runner.instance_id,
                        "symbol": "BASKET",
                        "asset_kind": "stock",
                        "timeframe": runner.timeframe,
                        "quantity": runner.qty,
                        "starting_cash": runner.starting_cash,
                        "params": dict(runner.strategy.params),
                        "enabled": runner.enabled,
                    }
                )
            else:
                rows.append(
                    {
                        "strategy_id": runner.strategy.id,
                        "instance_id": runner.instance_id,
                        "symbol": runner.symbol,
                        "asset_kind": runner.asset_kind,
                        "timeframe": runner.timeframe,
                        "quantity": runner.quantity,
                        "starting_cash": runner.broker.starting_cash,
                        "params": dict(runner.strategy.params),
                        "enabled": runner.enabled,
                    }
                )
        return rows

    def persist_desk(self) -> None:
        if self._restoring:
            return
        from algo.paper.desk_store import save_strategies

        save_strategies(
            self.desk_configs(),
            prefs={
                "mode": self.mode if self.mode in {"live", "replay"} else "live",
                "poll_seconds": self.poll_seconds,
                "was_running": self.running,
            },
        )
        self.persist_runtime()

    def persist_runtime(self) -> None:
        """Checkpoint open positions + strategy internals so a crash can resume."""
        if self._restoring:
            return
        from algo.paper.desk_store import save_runtime

        runners: dict[str, Any] = {}
        for key, runner in self.runners.items():
            if isinstance(runner, BasketRunner):
                legs = getattr(runner.strategy, "_legs", {}) or {}
                runners[key] = {
                    "type": "basket",
                    "enabled": runner.enabled,
                    "last_signal": runner.last_signal,
                    "selected": list(runner.selected),
                    "scanned": bool(runner._scanned),
                    "cash_pool": runner.cash_pool,
                    "qty": runner.qty,
                    "legs": legs,
                    "selection_meta": list(getattr(runner.strategy, "selection_meta", []) or []),
                    "open_trade_ids": dict(runner.open_trade_ids),
                    "brokers": {
                        sym: _broker_snapshot(broker) for sym, broker in runner.brokers.items()
                    },
                }
            else:
                strat = runner.strategy
                runners[key] = {
                    "type": "single",
                    "enabled": runner.enabled,
                    "last_signal": runner.last_signal,
                    "mark_price": runner.mark_price,
                    "option_state": dict(runner.option_state or {}),
                    "broker": _broker_snapshot(runner.broker),
                    "orb": {
                        "day": str(getattr(strat, "_day", None) or ""),
                        "range_high": getattr(strat, "_range_high", None),
                        "range_done": bool(getattr(strat, "_range_done", False)),
                        "in_trade": bool(getattr(strat, "_in_trade", False)),
                        "entry_price": getattr(strat, "_entry_price", None),
                        "entry_time": getattr(strat, "_entry_time", None),
                        "trades_today": int(getattr(strat, "_trades_today", 0) or 0),
                        "selected_strike": getattr(strat, "_selected_strike", None),
                    },
                    "zen": {
                        "in_trade": bool(getattr(strat, "_in_trade", False)),
                        "structure": getattr(strat, "_structure", None),
                        "entry_credit": getattr(strat, "_entry_credit", None),
                        "entry_day": str(getattr(strat, "_entry_day", None) or ""),
                        "atm_at_entry": getattr(strat, "_atm_at_entry", None),
                        "short_strike": getattr(strat, "_short_strike", None),
                        "long_strike": getattr(strat, "_long_strike", None),
                        "max_loss_pts": getattr(strat, "_max_loss_pts", None),
                    },
                }
        save_runtime(
            {
                "saved_at": utcnow().isoformat(),
                "mode": self.mode,
                "running": self.running,
                "poll_seconds": self.poll_seconds,
                "runners": runners,
            }
        )

    def restore_desk(self) -> None:
        from algo.paper.desk_store import load_desk

        data = load_desk()
        prefs = data.get("prefs") or {}
        self.poll_seconds = float(prefs.get("poll_seconds") or 15)
        self._resume_prefs = {
            "mode": prefs.get("mode") or "live",
            "poll_seconds": self.poll_seconds,
            "was_running": bool(prefs.get("was_running")),
        }
        self._restoring = True
        try:
            for row in data.get("strategies") or []:
                try:
                    sid = row.get("strategy_id")
                    if not sid:
                        continue
                    key = row.get("instance_id")
                    if key and key in self.runners:
                        continue
                    self.add_strategy(
                        sid,
                        symbol=row.get("symbol") or "NIFTY",
                        timeframe=row.get("timeframe") or "5m",
                        quantity=int(row.get("quantity") or 1),
                        starting_cash=float(row.get("starting_cash") or 100_000),
                        params=_normalize_strategy_params(row.get("params") or {}),
                        enabled=bool(row.get("enabled", True)),
                        asset_kind=row.get("asset_kind"),
                        instance_id=key,
                    )
                except Exception as exc:
                    self.message = f"Desk restore skip: {exc}"
        finally:
            self._restoring = False
            # Keep prior was_running flag so maybe_auto_resume can restart live paper.
            from algo.paper.desk_store import save_strategies

            save_strategies(
                self.desk_configs(),
                prefs={
                    "mode": self._resume_prefs.get("mode") or "live",
                    "poll_seconds": self.poll_seconds,
                    "was_running": bool(self._resume_prefs.get("was_running")),
                },
            )
            self.persist_runtime()
        if self.runners:
            self.message = f"Restored {len(self.runners)} strateg{'y' if len(self.runners)==1 else 'ies'} from saved desk"

    def restore_runtime(self) -> None:
        """Rehydrate open positions from disk + journal after a crash/restart."""
        from algo.paper.desk_store import load_runtime

        data = load_runtime()
        snaps = data.get("runners") or {}
        restored = 0
        self._restoring = True
        try:
            for key, runner in self.runners.items():
                snap = snaps.get(key) or {}
                try:
                    if isinstance(runner, BasketRunner):
                        restored += self._restore_basket_runner(runner, snap)
                    else:
                        restored += self._restore_single_runner(runner, snap)
                except Exception as exc:
                    runner._log(f"runtime restore skip: {exc}")
        finally:
            self._restoring = False
        if restored:
            self.message = (
                f"Restored {len(self.runners)} strategies and {restored} open position"
                f"{'' if restored == 1 else 's'} from last checkpoint"
            )
            self.persist_runtime()

    def _restore_single_runner(self, runner: StrategyRunner, snap: dict[str, Any]) -> int:
        count = 0
        if snap.get("option_state"):
            runner.option_state = dict(snap["option_state"])
        if snap.get("mark_price") is not None:
            runner.mark_price = float(snap["mark_price"])
        if snap.get("last_signal"):
            runner.last_signal = str(snap["last_signal"])
        if snap.get("broker"):
            _apply_broker_snapshot(
                runner.broker,
                snap["broker"],
                strategy_id=runner.strategy.id,
                symbol=runner.symbol,
                instrument_id=runner.instrument_id,
            )
            if runner.broker.position.quantity:
                count += 1

        strat = runner.strategy
        if runner.strategy.id == "zen_credit_spread":
            zen = snap.get("zen") or {}
            if zen:
                from datetime import date as date_cls

                strat._in_trade = bool(zen.get("in_trade"))  # type: ignore[attr-defined]
                strat._structure = zen.get("structure")  # type: ignore[attr-defined]
                strat._entry_credit = (  # type: ignore[attr-defined]
                    float(zen["entry_credit"]) if zen.get("entry_credit") is not None else None
                )
                entry_day = str(zen.get("entry_day") or "")
                if entry_day:
                    try:
                        strat._entry_day = date_cls.fromisoformat(entry_day)  # type: ignore[attr-defined]
                    except Exception:
                        pass
                if zen.get("atm_at_entry") is not None:
                    strat._atm_at_entry = int(zen["atm_at_entry"])  # type: ignore[attr-defined]
                if zen.get("short_strike") is not None:
                    strat._short_strike = int(zen["short_strike"])  # type: ignore[attr-defined]
                if zen.get("long_strike") is not None:
                    strat._long_strike = int(zen["long_strike"])  # type: ignore[attr-defined]
                if zen.get("max_loss_pts") is not None:
                    strat._max_loss_pts = float(zen["max_loss_pts"])  # type: ignore[attr-defined]
                if strat._in_trade:  # type: ignore[attr-defined]
                    count = max(count, 1)
                    runner._log("Restored Zen open credit spread from checkpoint")
        else:
            orb = snap.get("orb") or {}
            if orb:
                day = str(orb.get("day") or "")
                if day:
                    from datetime import date as date_cls

                    try:
                        strat._day = date_cls.fromisoformat(day)  # type: ignore[attr-defined]
                    except Exception:
                        pass
                if orb.get("range_high") is not None:
                    strat._range_high = float(orb["range_high"])  # type: ignore[attr-defined]
                strat._range_done = bool(orb.get("range_done"))  # type: ignore[attr-defined]
                strat._in_trade = bool(orb.get("in_trade"))  # type: ignore[attr-defined]
                strat._entry_price = (  # type: ignore[attr-defined]
                    float(orb["entry_price"]) if orb.get("entry_price") is not None else None
                )
                strat._entry_time = _parse_iso(orb.get("entry_time"))  # type: ignore[attr-defined]
                strat._trades_today = int(orb.get("trades_today") or 0)  # type: ignore[attr-defined]
                if orb.get("selected_strike") is not None:
                    strat._selected_strike = int(orb["selected_strike"])  # type: ignore[attr-defined]
                if strat._in_trade and runner.broker.position.quantity == 0 and strat._entry_price:  # type: ignore[attr-defined]
                    # Ensure broker mirrors strategy open state
                    qty = runner.quantity or 1
                    runner.broker.position.quantity = qty
                    runner.broker.position.avg_price = float(strat._entry_price)  # type: ignore[attr-defined]
                    notional = qty * float(strat._entry_price)  # type: ignore[attr-defined]
                    runner.broker.cash = max(0.0, runner.broker.starting_cash - notional)
                    count = max(count, 1)
                if getattr(strat, "_in_trade", False):
                    runner._log("Restored open ORB position from checkpoint")
        return count

    def _restore_basket_runner(self, runner: BasketRunner, snap: dict[str, Any]) -> int:
        count = 0
        legs = dict(snap.get("legs") or {})
        selected = list(snap.get("selected") or [])
        brokers_snap = dict(snap.get("brokers") or {})
        open_ids = dict(snap.get("open_trade_ids") or {})

        # Journal is source of truth when runtime file is stale/missing.
        # Only resume same-day opens (IST) so old stuck rows don't resurrect.
        today = datetime.now(tz=IST).date()
        journal_opens = list_open_trades(instance_id=runner.instance_id, limit=200)
        for trade in journal_opens:
            entry_at = _parse_iso(trade.get("entry_at"))
            if entry_at is not None:
                try:
                    if entry_at.astimezone(IST).date() != today:
                        continue
                except Exception:
                    pass
            sym = str(trade.get("symbol") or "").upper()
            if not sym:
                continue
            if sym not in selected:
                selected.append(sym)
            open_ids.setdefault(sym, trade["id"])
            side = str(trade.get("side") or "LONG").upper()
            qty = int(trade.get("quantity") or runner.qty or 1)
            entry = float(trade.get("entry_price") or 0)
            signed_qty = qty if side == "LONG" else -qty
            brokers_snap.setdefault(
                sym,
                {
                    "cash": runner.starting_cash / max(len(selected) or 1, 1),
                    "starting_cash": runner.starting_cash / max(len(selected) or 1, 1),
                    "realized_pnl": 0.0,
                    "quantity": signed_qty,
                    "avg_price": entry,
                    "symbol": sym,
                },
            )
            brokers_snap[sym]["quantity"] = signed_qty
            brokers_snap[sym]["avg_price"] = entry
            meta = trade.get("meta") if isinstance(trade.get("meta"), dict) else {}
            leg = legs.setdefault(
                sym,
                {
                    "range_high": None,
                    "range_low": None,
                    "range_done": True,
                    "in_trade": True,
                    "entry": entry,
                    "stop": trade.get("stop_price"),
                    "target": trade.get("target_price"),
                    "trades_today": 1,
                    "side": "long" if side == "LONG" else "short",
                },
            )
            leg["in_trade"] = True
            leg["entry"] = entry
            if trade.get("stop_price") is not None:
                leg["stop"] = trade.get("stop_price")
            if trade.get("target_price") is not None:
                leg["target"] = trade.get("target_price")
            if meta.get("stop") is not None:
                leg["stop"] = meta.get("stop")
            if meta.get("target") is not None:
                leg["target"] = meta.get("target")
            leg["range_done"] = True

        if snap.get("last_signal"):
            runner.last_signal = str(snap["last_signal"])
        if snap.get("cash_pool") is not None:
            runner.cash_pool = float(snap["cash_pool"])
        if snap.get("qty") is not None:
            runner.qty = int(snap["qty"])

        runner.selected = selected
        runner.strategy.selected = list(selected)
        if snap.get("selection_meta"):
            runner.strategy.selection_meta = list(snap["selection_meta"])
        if legs:
            runner.strategy._legs = legs  # type: ignore[attr-defined]
        runner.open_trade_ids = {str(k).upper(): str(v) for k, v in open_ids.items()}
        runner._scanned = bool(selected) or bool(snap.get("scanned"))

        for sym, bsnap in brokers_snap.items():
            sym_u = str(sym).upper()
            broker = runner.brokers.get(sym_u)
            if broker is None:
                broker = PaperBroker(
                    starting_cash=float(bsnap.get("starting_cash") or (runner.starting_cash / max(len(selected) or 1, 1)))
                )
                runner.brokers[sym_u] = broker
            _apply_broker_snapshot(
                broker,
                bsnap,
                strategy_id=runner.strategy.id,
                symbol=sym_u,
                instrument_id=f"NSE:EQ:{sym_u}",
            )
            runner.histories.setdefault(sym_u, [])
            if broker.position.quantity:
                count += 1
                runner._log(
                    f"Restored open {sym_u} qty={broker.position.quantity} @ {broker.position.avg_price:.2f}"
                )
        if selected:
            runner._log(f"Basket resume symbols: {', '.join(selected)}")
        return count

    def maybe_auto_resume(self) -> None:
        prefs = getattr(self, "_resume_prefs", None) or {}
        if not prefs.get("was_running"):
            return
        if self.running or not self.runners:
            return
        mode = prefs.get("mode") or "live"
        poll = float(prefs.get("poll_seconds") or self.poll_seconds or 15)
        try:
            if mode == "live":
                self.start_live(poll_seconds=poll)
                self.message = (
                    f"Auto-resumed live paper after restart — {len(self.runners)} strategies · "
                    f"virtual fills only"
                )
            self.persist_desk()
        except Exception as exc:
            self.message = f"Auto-resume failed: {exc}. Press Start to continue."

    def install_signal_handlers(self) -> None:
        """Best-effort checkpoint on process exit (Ctrl+C / SIGTERM)."""
        import atexit
        import signal

        if getattr(self, "_signals_installed", False):
            return
        self._signals_installed = True

        def _checkpoint(*_args: Any) -> None:
            try:
                # Keep was_running=True on unexpected stop so the next boot resumes.
                if self.running:
                    from algo.paper.desk_store import save_strategies

                    save_strategies(
                        self.desk_configs(),
                        prefs={
                            "mode": self.mode if self.mode in {"live", "replay"} else "live",
                            "poll_seconds": self.poll_seconds,
                            "was_running": True,
                        },
                    )
                self.persist_runtime()
            except Exception:
                pass

        atexit.register(_checkpoint)

        def _handle(signum: int, _frame: Any) -> None:
            _checkpoint()
            signal.signal(signum, signal.SIG_DFL)
            signal.raise_signal(signum)

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, _handle)
            except Exception:
                pass

    def snapshot(self) -> SessionSnapshot:
        with self._lock:
            return SessionSnapshot(
                mode=self.mode,
                running=self.running,
                started_at=self.started_at,
                updated_at=self.updated_at,
                strategies=[r.state() for r in self.runners.values()],
                message=self.message,
            )

    def analytics(self) -> dict[str, Any]:
        from algo.paper.analytics import strategy_analytics_row, summarize_closed_trades

        with self._lock:
            rows = []
            all_trades: list[dict] = []
            for runner in self.runners.values():
                if isinstance(runner, BasketRunner):
                    trades: list[dict] = []
                    for b in runner.brokers.values():
                        trades.extend(b.closed_trades)
                    all_trades.extend(trades)
                    rows.append(
                        strategy_analytics_row(
                            instance_id=runner.instance_id,
                            strategy_id=runner.strategy.id,
                            name=runner.strategy.name,
                            enabled=runner.enabled,
                            instrument=",".join(runner.selected) or "BASKET",
                            asset_kind="stock",
                            realized_pnl=sum(b.realized_pnl for b in runner.brokers.values()),
                            unrealized_pnl=sum(
                                b.unrealized_pnl(
                                    runner.histories[s][-1].close if runner.histories.get(s) else None
                                )
                                for s, b in runner.brokers.items()
                            ),
                            closed_trades=trades,
                            params=runner.strategy.params,
                        )
                    )
                    continue
                trades = list(runner.broker.closed_trades)
                all_trades.extend(trades)
                last = runner.mark_price
                if last is None:
                    last = runner.history[-1].close if runner.history else None
                rows.append(
                    strategy_analytics_row(
                        instance_id=runner.instance_id,
                        strategy_id=runner.strategy.id,
                        name=runner.strategy.name,
                        enabled=runner.enabled,
                        instrument=runner.symbol,
                        asset_kind=runner.asset_kind,
                        realized_pnl=runner.broker.realized_pnl,
                        unrealized_pnl=runner.broker.unrealized_pnl(last),
                        closed_trades=trades,
                        params=runner.strategy.params,
                    )
                )
            overall = summarize_closed_trades(all_trades)
            return {
                "mode": self.mode,
                "running": self.running,
                "strategies": rows,
                "overall": overall,
                "message": self.message,
            }

    def start_replay(self, *, max_bars: int | None = None) -> None:
        if self.running:
            raise RuntimeError("Session already running")
        if not self.runners:
            raise RuntimeError("Add at least one strategy first")
        self.mode = "replay"
        self.running = True
        self.started_at = utcnow()
        self.journal_session_id = start_journal_session(mode="replay", message="Paper replay")
        for r in self.runners.values():
            if isinstance(r, BasketRunner):
                r.journal_session_id = self.journal_session_id
        self._stop.clear()
        self.message = "Replay running on historical candles…"
        self._thread = threading.Thread(target=self._run_replay, args=(max_bars,), daemon=True)
        self._thread.start()

    def start_live(self, *, poll_seconds: float = 15.0) -> None:
        if self.running:
            raise RuntimeError("Session already running")
        if not self.runners:
            raise RuntimeError("Add at least one strategy first")
        source = (self.settings.paper_price_source or "dhan-ws").lower()
        if source in {"dhan", "dhan-ws", "auto", "public"}:
            from algo.providers.dhan.auth import TokenRotator, current_token, ensure_fresh_token

            TokenRotator.instance().start()
            if source in {"dhan", "dhan-ws"}:
                if not self.settings.dhan_client_id or not current_token(self.settings):
                    raise RuntimeError(
                        "DHAN_CLIENT_ID / access token required — paste a fresh token in Settings"
                    )
            try:
                ensure_fresh_token(self.settings)
            except Exception as exc:
                if source in {"dhan", "dhan-ws"}:
                    raise RuntimeError(str(exc)) from exc
        self.mode = "live"
        self.running = True
        self.poll_seconds = poll_seconds
        self.started_at = utcnow()
        self.journal_session_id = start_journal_session(mode="live", message="Paper live")
        for r in self.runners.values():
            if isinstance(r, BasketRunner):
                r.journal_session_id = self.journal_session_id
        self._stop.clear()
        if self._quotes is not None:
            try:
                self._quotes.close()
            except Exception:
                pass
        self._quotes = LiveQuoteProvider(self.settings)
        self._rest_seeded_day = None
        self._rest_seeded_syms = frozenset()
        self._feed_phase = "idle"
        try:
            # Policy: REST verify/seed first, then soft-start WS for live stream.
            probe = ["NIFTY", "RELIANCE"]
            with self._lock:
                boot_syms: list[str] = []
                for r in self.runners.values():
                    if not r.enabled:
                        continue
                    if isinstance(r, BasketRunner):
                        boot_syms.extend(r.selected or [])
                    else:
                        boot_syms.append(getattr(r, "symbol", "") or "")
            seed_list = list(dict.fromkeys([*probe, *[s for s in boot_syms if s]]))
            seeded = self._quotes.seed_ltps_rest_first(seed_list, wait_ws_sec=2.0)
            self._feed_phase = "rest_ok" if seeded else "idle"
            if seeded:
                self._rest_seeded_day = datetime.now(IST).date()
                self._rest_seeded_syms = frozenset(seeded)
            # Soft WS wait — do not treat socket-up alone as success.
            self._quotes.wait_for_ws(timeout=5.0)
            if seed_list and self._quotes.mark_coverage(seed_list) >= 0.5:
                self._feed_phase = "ws_live"
        except Exception:
            pass
        feed = self._quotes.feed_status
        seeded_n = len(self._rest_seeded_syms)
        self.message = (
            f"Live paper REST→WS ({self._feed_phase}, seeded {seeded_n}) via {feed} · "
            f"poll {poll_seconds:.0f}s — virtual fills only (no Dhan orders)"
        )
        self._thread = threading.Thread(target=self._run_live, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        try:
            self.flatten_intraday_for_close()
        except Exception:
            pass
        self._stop.set()
        self.running = False
        end_journal_session(self.journal_session_id, message="Stopped")
        self.message = "Stopped"
        self.updated_at = utcnow()
        if self._quotes is not None:
            try:
                self._quotes.close()
            except Exception:
                pass
            self._quotes = None
        from algo.paper.desk_store import save_strategies

        save_strategies(
            self.desk_configs(),
            prefs={
                "mode": self.mode if self.mode in {"live", "replay"} else "live",
                "poll_seconds": self.poll_seconds,
                "was_running": False,
            },
        )
        self.persist_runtime()

    def flatten_intraday_for_close(self) -> None:
        """Square off open intraday paper legs before sleep (Zen overnight kept)."""
        quotes = self._quotes
        if quotes is None:
            return
        with self._lock:
            runners = list(self.runners.values())
        for runner in runners:
            if not runner.enabled:
                continue
            if isinstance(runner, BasketRunner):
                # Force EOD path even if flatten_at already passed earlier.
                runner._eod_done_day = None
                p = {**runner.strategy.default_params(), **runner.strategy.params}
                if not str(p.get("flatten_at") or "").strip():
                    runner.strategy.params = {**runner.strategy.params, "flatten_at": "15:00"}
                runner._maybe_eod_flatten(quotes)
                continue
            if getattr(runner.strategy, "id", "") == "zen_credit_spread":
                # Designed to hold overnight unless hold_overnight=false.
                continue
            pos_qty = runner.broker.position.quantity
            if pos_qty == 0:
                continue
            try:
                fill_px = None
                p = {**getattr(runner.strategy, "default_params", lambda: {})(), **(runner.strategy.params or {})}
                flat_raw = str(p.get("flatten_at") or "15:00").strip() or "15:00"
                try:
                    hh, mm = [int(x) for x in flat_raw.split(":")[:2]]
                except Exception:
                    hh, mm = 15, 0
                exit_ts = datetime.combine(datetime.now(IST).date(), time_cls(hh, mm), tzinfo=IST)
                if runner.asset_kind == "option":
                    bar, st = quotes.option_premium_bar(
                        runner.symbol,
                        option_type=str(p.get("option_type", "CE")),
                        strike_mode=str(p.get("strike_mode", "ATM")),
                        strike_step=int(p.get("strike_step", 50)),
                        state=runner.option_state,
                    )
                    runner.option_state = st
                    fill_px = float(bar.close)
                else:
                    fill_px, _ts, _ = quotes.get_ltp(
                        runner.symbol, allow_rest=True, wait_ws_sec=0.5, max_stale_sec=600
                    )
                if fill_px is None:
                    continue
                side = Side.SELL if pos_qty > 0 else Side.BUY
                runner.broker.submit_market(
                    strategy_id=runner.strategy.id,
                    instrument_id=runner.instrument_id,
                    symbol=runner.symbol,
                    side=side,
                    quantity=abs(pos_qty),
                    last_price=float(fill_px),
                    ts=exit_ts,
                )
                if hasattr(runner.strategy, "_in_trade"):
                    runner.strategy._in_trade = False  # type: ignore[attr-defined]
                runner.last_signal = "EOD_FLAT"
                runner._log(
                    f"EOD flatten @ session sleep ({flat_raw} IST) @ {float(fill_px):.2f}"
                )
            except Exception:
                continue

    def run_replay_sync(self, *, max_bars: int | None = None) -> SessionSnapshot:
        """Blocking replay for CLI."""
        self.mode = "replay"
        self.running = True
        self.started_at = utcnow()
        self.message = "Replay…"
        try:
            self._run_replay(max_bars)
        finally:
            self.running = False
            self.message = "Replay complete"
            self.updated_at = utcnow()
        return self.snapshot()

    def _bars_for_replay(self, runner: StrategyRunner, max_bars: int | None) -> list[Bar]:
        symbol = runner.symbol
        timeframe = runner.timeframe
        if runner.strategy.id == "zen_credit_spread":
            quotes = LiveQuoteProvider(self.settings, enable_dhan_feed=False)
            try:
                # Need ≥800 minutes of 5m history for alpha lookback.
                bars = quotes.load_public_bars(symbol, interval="5m", range_="60d")
                self.message = f"Replay Zen credit-spread on public {symbol} 5m bars…"
                return bars[-max_bars:] if max_bars else bars
            except Exception:
                bars = quotes.synthetic_bars(symbol, n=max(max_bars or 900, 900))
                self.message = f"Replay Zen using synthetic {symbol} 5m bars…"
                return bars

        if runner.asset_kind == "option":
            quotes = LiveQuoteProvider(self.settings, enable_dhan_feed=False)
            p = runner.strategy.params
            try:
                interval = "1m" if timeframe in {"1m", "1"} else "5m"
                bars = quotes.load_option_premium_bars(
                    symbol,
                    option_type=str(p.get("option_type", "CE")),
                    strike_mode=str(p.get("strike_mode", "ATM")),
                    strike_step=int(p.get("strike_step", 50)),
                    interval=interval,
                    range_="5d",
                )
                self.message = (
                    f"Replay synthetic {symbol} {p.get('option_type', 'CE')} "
                    f"{p.get('strike_mode', 'ATM')} premium (paper proxy)…"
                )
                return bars[-max_bars:] if max_bars else bars
            except Exception:
                bars = quotes.synthetic_bars(symbol, n=max_bars or 300)
                # reinterpret synthetic as premium-like levels
                return bars

        df = load_candles(symbol, timeframe)
        if not df.empty:
            bars = [
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
            return bars[-max_bars:] if max_bars else bars

        quotes = LiveQuoteProvider(self.settings, enable_dhan_feed=False)
        try:
            interval = "5m" if timeframe in {"5m", "5"} else "1m" if timeframe in {"1m", "1"} else "5m"
            bars = quotes.load_public_bars(symbol, interval=interval, range_="5d")
            self.message = f"Replay using public {symbol} bars (no Dhan Data API)…"
            return bars[-max_bars:] if max_bars else bars
        except Exception:
            bars = quotes.synthetic_bars(symbol, n=max_bars or 300)
            self.message = f"Replay using synthetic {symbol} bars (offline demo)…"
            return bars

    def _run_replay(self, max_bars: int | None) -> None:
        try:
            with self._lock:
                runners = list(self.runners.values())
            quotes = LiveQuoteProvider(self.settings, enable_dhan_feed=False)
            for runner in runners:
                if isinstance(runner, BasketRunner):
                    runner.journal_session_id = self.journal_session_id
                    self.message = f"Replay basket {runner.strategy.name}…"
                    runner.run_replay(quotes, max_bars=max_bars or 400)
                    self.updated_at = utcnow()
                    continue
                bars = self._bars_for_replay(runner, max_bars)
                if not bars:
                    self.message = f"No bars available for {runner.symbol} {runner.timeframe}"
                    continue
                if runner.asset_kind == "option" and bars and bars[0].volume:
                    runner.strategy.params["strike"] = int(bars[0].volume)
                    runner.strategy._selected_strike = int(bars[0].volume)  # type: ignore[attr-defined]
                for bar in bars:
                    if self._stop.is_set():
                        break
                    runner.on_bar(bar)
                    self.updated_at = utcnow()
            if not self.message.startswith("Replay using") and not self.message.startswith("Replay synthetic"):
                self.message = "Replay complete"
            else:
                self.message = self.message.replace("…", "") + " — complete"
            end_journal_session(self.journal_session_id, message=self.message)
        except Exception as exc:
            self.message = f"Replay error: {exc}"
            end_journal_session(self.journal_session_id, message=self.message)
        finally:
            self.running = False
            self.updated_at = utcnow()

    def _collect_want_syms(self, runners: list[Any]) -> list[str]:
        want_syms: list[str] = []
        for runner in runners:
            if not runner.enabled:
                continue
            if isinstance(runner, BasketRunner):
                want_syms.extend(runner.selected or [])
                for sym, broker in runner.brokers.items():
                    if broker.position.quantity:
                        want_syms.append(sym)
            else:
                want_syms.append(getattr(runner, "symbol", "") or "")
        return [s for s in dict.fromkeys(want_syms) if s]

    def _run_live(self) -> None:
        try:
            while not self._stop.is_set():
                quotes = self._quotes
                if quotes is None:
                    break
                with self._lock:
                    runners = list(self.runners.values())

                # 1) REST scan / selection first (may pick new basket names).
                for runner in runners:
                    if not runner.enabled or not isinstance(runner, BasketRunner):
                        continue
                    try:
                        runner.journal_session_id = self.journal_session_id
                        runner._maybe_roll_trading_day()
                        runner.ensure_selection(quotes)
                    except Exception:
                        pass

                # 2) Rebuild want-set from selected + open legs only.
                want_syms = self._collect_want_syms(runners)
                today = datetime.now(IST).date()
                want_key = frozenset(want_syms)
                coverage = quotes.mark_coverage(want_syms) if want_syms else 1.0
                ws_down = bool(
                    quotes._ws is not None
                    and (quotes._ws.rate_limited() or not quotes._ws.connected)
                )
                need_rest_seed = bool(want_syms) and (
                    self._rest_seeded_day != today
                    or want_key != self._rest_seeded_syms
                    or (
                        (coverage < 0.75 or ws_down)
                        and (self._runtime_tick == 0 or self._runtime_tick % 3 == 0)
                    )
                )

                try:
                    if need_rest_seed and want_syms:
                        # 3) REST LTP first (verify + seed), then subscribe WS.
                        seeded = quotes.seed_ltps_rest_first(
                            want_syms, wait_ws_sec=1.0 if not ws_down else 0.0
                        )
                        if seeded:
                            self._rest_seeded_day = today
                            self._rest_seeded_syms = frozenset(want_syms)
                            self._feed_phase = "rest_ok"
                            coverage = quotes.mark_coverage(want_syms)
                    elif want_syms:
                        # Already seeded — WS-only warm (no REST unless coverage falls).
                        quotes.set_subscriptions(want_syms)
                        quotes.prefetch_ltps(
                            want_syms, wait_ws_sec=0.8, allow_rest=False
                        )
                    if want_syms and coverage >= 0.75 and not ws_down:
                        self._feed_phase = "ws_live"
                except Exception:
                    pass

                # Adaptive cadence: more active symbols → slower poll (Dhan-safe).
                sleep_for = suggested_poll_seconds(
                    len(want_syms), base=float(self.poll_seconds or 15.0)
                )
                src = f"{quotes.feed_status}|{self._feed_phase}"
                labels: list[str] = []
                errors: list[str] = []
                for runner in runners:
                    if not runner.enabled:
                        continue
                    try:
                        if isinstance(runner, BasketRunner):
                            runner.journal_session_id = self.journal_session_id
                            # Selection already done above; tick marks only.
                            labels.extend(runner.tick_live(quotes, skip_selection=True))
                            src = f"{quotes.feed_status}|{self._feed_phase}"
                        elif runner.strategy.id == "zen_credit_spread":
                            bar = self._live_zen_bar(runner)
                            runner.on_bar(bar)
                            a = getattr(runner.strategy, "_structure", None) or "flat"
                            mark = runner.mark_price
                            labels.append(
                                f"ZEN {runner.symbol} {a}"
                                + (f" mark={mark:.1f}" if mark is not None else f" spot={bar.close:.1f}")
                            )
                            src = f"{quotes.feed_status}|{self._feed_phase}"
                        elif runner.asset_kind == "option":
                            p = runner.strategy.params
                            bar, st = quotes.option_premium_bar(
                                runner.symbol,
                                option_type=str(p.get("option_type", "CE")),
                                strike_mode=str(p.get("strike_mode", "ATM")),
                                strike_step=int(p.get("strike_step", 50)),
                                state=runner.option_state,
                            )
                            runner.option_state = st
                            runner.strategy.params["strike"] = int(st.get("strike", 0))
                            runner.strategy._selected_strike = int(st.get("strike", 0))  # type: ignore[attr-defined]
                            runner.on_bar(bar)
                            labels.append(
                                f"{runner.symbol}{p.get('option_type', 'CE')}@{int(st.get('strike', 0))}={bar.close:.1f}"
                            )
                            src = f"{quotes.feed_status}|{self._feed_phase}"
                        else:
                            price, ts, used = quotes.get_ltp(
                                runner.symbol, allow_rest=False, wait_ws_sec=0.5, max_stale_sec=180
                            )
                            bar = Bar(
                                timestamp=ts, open=price, high=price, low=price, close=price, volume=0
                            )
                            runner.on_bar(bar)
                            labels.append(f"{runner.symbol}={price:.2f}")
                            src = used
                    except Exception as exc:
                        name = getattr(runner, "symbol", runner.strategy.id)
                        err = str(exc)
                        if "429" in err or "too many" in err.lower() or "rate limited" in err.lower():
                            errors.append(f"{name}: Dhan 429 — waiting for websocket")
                        else:
                            errors.append(f"{name}: {err}")
                self.updated_at = utcnow()
                if labels:
                    self.message = (
                        f"Live paper ({src}) @ {datetime.now(tz=IST).strftime('%H:%M:%S')} IST — "
                        + ", ".join(labels[:12])
                        + ("…" if len(labels) > 12 else "")
                        + " · virtual fills only"
                    )
                    if errors:
                        self.message += f" · warn: {errors[0]}"
                elif errors:
                    # Soft degrade — keep last marks; short WS cool must not freeze the desk.
                    self.message = (
                        f"Live paper holding last marks ({quotes.feed_status}|{self._feed_phase}) — {errors[0]}"
                    )
                self._runtime_tick += 1
                # Checkpoint often so a crash/laptop sleep can resume open trades.
                if self._runtime_tick == 1 or self._runtime_tick % 3 == 0:
                    try:
                        self.persist_runtime()
                        from algo.paper.desk_store import save_strategies

                        save_strategies(
                            self.desk_configs(),
                            prefs={
                                "mode": "live",
                                "poll_seconds": self.poll_seconds,
                                "was_running": True,
                            },
                        )
                    except Exception:
                        pass
                time.sleep(sleep_for)
        except Exception as exc:
            self.message = f"Live paper error: {exc}"
        finally:
            # Unexpected exit keeps was_running=True via last checkpoint.
            # Clean stop() sets was_running=False explicitly.
            try:
                self.persist_runtime()
            except Exception:
                pass
            self.running = False
            self.updated_at = utcnow()
            end_journal_session(self.journal_session_id, message=self.message)
    def _live_zen_bar(self, runner: StrategyRunner) -> Bar:
        """Warm 5m history once, then emit bucketed live 5m bars for Zen alphas."""
        assert self._quotes is not None
        if not runner.history:
            try:
                if self._quotes._rest_cooling():
                    raise RuntimeError("rest cooling — defer zen chart warmup")
                hist = self._quotes.load_public_bars(runner.symbol, interval="5m", range_="60d")
                # Keep enough for 800m lookback (+ cushion)
                runner.history = hist[-500:] if len(hist) > 500 else hist
                self.message = f"Zen warmed {len(runner.history)} × 5m bars for {runner.symbol}"
            except Exception as exc:
                self.message = f"Zen warmup deferred: {exc}"
                # Do not synthetic-spam forever — try again next ticks once feed is healthy.
                if not runner.history:
                    runner.history = []

        price, ts, _ = self._quotes.get_ltp(
            runner.symbol, allow_rest=False, wait_ws_sec=0.8, max_stale_sec=180
        )
        if not runner.history:
            # Still no history — emit a single forming bar so UI is not stuck on LTP error.
            runner.history = [
                Bar(timestamp=ts, open=price, high=price, low=price, close=price, volume=0)
            ]
        local = ts.astimezone(IST)
        bucket = local.replace(minute=(local.minute // 5) * 5, second=0, microsecond=0)
        forming = runner._forming_bar
        if forming is None or forming.timestamp.astimezone(IST) != bucket:
            # Close previous forming bar was already pushed via on_bar; start new bucket.
            runner._forming_bar = Bar(
                timestamp=bucket,
                open=price,
                high=price,
                low=price,
                close=price,
                volume=float(runner.option_state.get("vol", 0)) + 1,
            )
        else:
            runner._forming_bar = Bar(
                timestamp=bucket,
                open=forming.open,
                high=max(forming.high, price),
                low=min(forming.low, price),
                close=price,
                volume=forming.volume + 1,
            )
        return runner._forming_bar


_SESSION: PaperSession | None = None
_SESSION_LOCK = threading.Lock()
_SESSION_READY = threading.Event()


def get_session() -> PaperSession:
    """Return the singleton paper session.

    Heavy restore / auto-resume must NOT hold ``_SESSION_LOCK`` — otherwise
    every HTTP handler (including ``/api/health``) deadlocks while Dhan
    network calls run inside ``start_live``.
    """
    global _SESSION
    starter = False
    with _SESSION_LOCK:
        if _SESSION is None:
            _SESSION = PaperSession()
            _SESSION.install_signal_handlers()
            starter = True
        sess = _SESSION

    if starter:
        try:
            sess.restore_desk()
            sess.restore_runtime()
            sess.maybe_auto_resume()
        finally:
            _SESSION_READY.set()
    else:
        # Another thread may still be restoring — wait briefly so callers see a
        # consistent desk, but never block forever (health / UI must stay up).
        _SESSION_READY.wait(timeout=90.0)
    return sess


def reset_session() -> PaperSession:
    global _SESSION
    with _SESSION_LOCK:
        if _SESSION and _SESSION.running:
            _SESSION.stop()
        from algo.paper.desk_store import clear_runtime, save_strategies

        # Keep saved desk strategies; only clear live runtime state
        saved = _SESSION.desk_configs() if _SESSION else []
        prefs = {"mode": "live", "poll_seconds": 15, "was_running": False}
        clear_runtime()
        _SESSION_READY.clear()
        _SESSION = PaperSession()
        _SESSION.install_signal_handlers()
        _SESSION._restoring = True
        try:
            for row in saved:
                try:
                    _SESSION.add_strategy(
                        row["strategy_id"],
                        symbol=row.get("symbol") or "NIFTY",
                        timeframe=row.get("timeframe") or "5m",
                        quantity=int(row.get("quantity") or 1),
                        starting_cash=float(row.get("starting_cash") or 100_000),
                        params=_normalize_strategy_params(row.get("params") or {}),
                        enabled=bool(row.get("enabled", True)),
                        asset_kind=row.get("asset_kind"),
                        instance_id=row.get("instance_id"),
                    )
                except Exception:
                    pass
        finally:
            _SESSION._restoring = False
            save_strategies(saved, prefs=prefs)
            _SESSION_READY.set()
        _SESSION.message = "Session runtime reset — desk strategies kept"
        return _SESSION


def reload_session_from_disk() -> PaperSession:
    """Stop live, re-open DB engine, restore desk + runtime from Postgres (or files)."""
    global _SESSION
    with _SESSION_LOCK:
        if _SESSION and _SESSION.running:
            _SESSION.stop()
        from algo.storage.db import get_engine, reset_engine

        reset_engine()
        get_engine()
        _SESSION_READY.clear()
        _SESSION = PaperSession()
        _SESSION.install_signal_handlers()
        sess = _SESSION
    try:
        sess.restore_desk()
        sess.restore_runtime()
        sess.message = f"Restored from database — {len(sess.runners)} strategies on desk"
    finally:
        _SESSION_READY.set()
    return sess
