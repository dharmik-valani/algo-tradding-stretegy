from __future__ import annotations

from datetime import date, datetime, timedelta
from collections.abc import Callable
from typing import Any
from zoneinfo import ZoneInfo
import time

from algo.config import Settings
from algo.ingest.rate_limiter import RateLimiter
from algo.paper.dhan_equity_ids import nse_eq_security_id
from algo.paper.models import Bar
from algo.providers.base import ProviderError
from algo.providers.dhan.client import DhanClient
from algo.providers.dhan.live_feed import DhanLiveFeed
from algo.providers.dhan.parser import parse_columnar_candles

IST = ZoneInfo("Asia/Kolkata")

INDEX_LTP_KEYS = {
    "NIFTY": ("IDX_I", "13"),
    "BANKNIFTY": ("IDX_I", "25"),
    "FINNIFTY": ("IDX_I", "27"),
    "MIDCPNIFTY": ("IDX_I", "442"),
    "SENSEX": ("IDX_I", "51"),
}

# Dhan marketfeed allows up to 1000 ids / request.
_DHAN_BATCH = 900

# Accept legacy env values; all map to Dhan-only behaviour.
_DHAN_SOURCES = {"dhan", "dhan-ws", "auto", "public"}


def suggested_poll_seconds(want_count: int, *, base: float = 15.0) -> float:
    """Fallback poll interval when WebSocket is down (REST-safe cadence).

    Live trading is WS-tick driven; this only applies while the socket is
    disconnected / rate-limited so we do not hammer Quote REST.
    """
    n = max(0, int(want_count))
    if n <= 20:
        target = 15.0
    elif n <= 50:
        target = 25.0
    elif n <= 100:
        target = 40.0
    else:
        target = 60.0
    return max(float(base), target)


class LiveQuoteProvider:
    """Paper price feed — REST-first verify/seed, then WebSocket for live marks."""

    def __init__(
        self,
        settings: Settings,
        client: DhanClient | None = None,
        *,
        enable_dhan_feed: bool = True,
    ) -> None:
        self.settings = settings
        raw = (getattr(settings, "paper_price_source", None) or "dhan-ws").lower()
        self.source = "dhan-ws" if raw in _DHAN_SOURCES else raw
        live_url = settings.yaml_config.get("dhan", {}).get("base_url", "https://api.dhan.co/v2")
        self._dhan: DhanClient | None = None
        self._ws: DhanLiveFeed | None = None
        self._dhan_feed_ok: bool | None = None  # None=unknown
        self._last_dhan_error: str | None = None
        self._token: str = ""
        self._rest_cooldown_until = 0.0
        # Short-lived REST LTP memo so one poll doesn't fire N identical calls.
        self._rest_ltp_memo: dict[str, tuple[float, float]] = {}
        # Last-known marks for risk checks when the socket briefly drops (keep trading).
        # symbol -> (price, monotonic_ts, source)
        self._mark_cache: dict[str, tuple[float, float, str]] = {}
        # (segment, security_id) -> symbol for WS tick fan-out
        self._sid_to_sym: dict[tuple[str, str], str] = {}
        self._on_mark: Callable[[str, float], None] | None = None
        self._enable_dhan_feed = enable_dhan_feed
        self._429_strikes = 0
        # Diagnostics for rate-limit monitoring (ring buffer).
        self._rl_events: list[dict[str, Any]] = []
        self._rest_calls: dict[str, int] = {"quote": 0, "data": 0, "other": 0}
        self._rest_ok: dict[str, int] = {"quote": 0, "data": 0, "other": 0}
        self._last_want_syms: list[str] = []


        if self._enable_dhan_feed:
            try:
                from algo.providers.dhan.auth import TokenRotator, current_token, ensure_fresh_token

                try:
                    self._token = ensure_fresh_token(settings)
                except Exception as exc:
                    self._last_dhan_error = str(exc)
                    self._token = current_token(settings)
                if self._token and settings.dhan_client_id:
                    self._dhan = client or DhanClient(
                        client_id=settings.dhan_client_id,
                        access_token=self._token,
                        base_url=live_url,
                        timeout=settings.timeout_seconds,
                        # Official DhanHQ: Quote=1/s, Data=5/s — stay under both.
                        quote_limiter=RateLimiter(settings.quote_requests_per_second, settings.requests_per_day),
                        data_limiter=RateLimiter(settings.requests_per_second, settings.requests_per_day),
                    )
                    self._ws = DhanLiveFeed(
                        client_id=settings.dhan_client_id,
                        token_provider=self._live_token,
                        prefer_quote=True,
                        on_tick=self._on_ws_tick,
                    )
                    # Lazy-start on first subscribe.
                    rotator = TokenRotator.instance()
                    rotator.on_renew(self._on_token_renewed)
                    rotator.start()
            except Exception as exc:
                self._last_dhan_error = str(exc)

    def _on_token_renewed(self, token: str) -> None:
        self._token = token
        if self._dhan is not None:
            self._dhan.access_token = token
        if self._ws is not None:
            self._ws.force_reconnect()

    def _live_token(self) -> str:
        from algo.providers.dhan.auth import current_token, ensure_fresh_token

        try:
            self._token = ensure_fresh_token(self.settings)
            if self._dhan is not None:
                self._dhan.access_token = self._token
        except Exception:
            self._token = current_token(self.settings) or self._token
        return self._token

    def _rest_cooling(self) -> bool:
        # Self-heal legacy long cools (old builds used 300–1200s).
        left = self._rest_cooldown_until - time.time()
        if left > 120:
            self._rest_cooldown_until = time.time() + 30.0
        return time.time() < self._rest_cooldown_until

    def _mark_rate_limited(
        self,
        exc: Exception | str,
        *,
        seconds: float = 300.0,
        kind: str = "quote",
        context: str = "",
    ) -> None:
        """Pause REST only after 429. WebSocket keep running so live marks continue."""
        msg = str(exc)
        self._last_dhan_error = msg
        if "429" in msg or "too many" in msg.lower() or "805" in msg:
            self._429_strikes += 1
            # REST-only cool — WS marks keep trading. Keep this short (not 5–20 min).
            cool = min(45.0 * min(self._429_strikes, 3), 120.0)
            self._rest_cooldown_until = max(self._rest_cooldown_until, time.time() + cool)
            event = {
                "ts": datetime.now(tz=IST).isoformat(),
                "kind": kind,
                "context": context or "unknown",
                "message": msg[:240],
                "strikes": self._429_strikes,
                "cool_seconds": int(cool),
                "want_symbols": len(self._last_want_syms),
                "mark_cache": len(self._mark_cache),
                "ws_status": self._ws.status if self._ws is not None else "no-ws",
            }
            self._rl_events.append(event)
            self._rl_events = self._rl_events[-40:]
            import logging

            logging.getLogger(__name__).warning(
                "dhan-rate-limit kind=%s ctx=%s cool=%ss strikes=%s want=%s msg=%s",
                kind,
                context,
                int(cool),
                self._429_strikes,
                len(self._last_want_syms),
                msg[:160],
            )
        else:
            self._dhan_feed_ok = False

    def diagnostics(self) -> dict[str, Any]:
        """Snapshot for /api/feed — used to diagnose rate limits without guessing."""
        ws = self._ws
        return {
            "feed_status": self.feed_status,
            "source": self.source,
            "want_symbols": list(self._last_want_syms),
            "want_count": len(self._last_want_syms),
            "mark_cache_count": len(self._mark_cache),
            "rest_cooling": self._rest_cooling(),
            "rest_cool_seconds_left": int(max(0, self._rest_cooldown_until - time.time()))
            if self._rest_cooling()
            else 0,
            "strikes_429": self._429_strikes,
            "rest_calls": dict(self._rest_calls),
            "rest_ok": dict(self._rest_ok),
            "ws": {
                "connected": bool(ws and ws.connected),
                "rate_limited": bool(ws and ws.rate_limited()),
                "status": ws.status if ws else None,
                "cache_ticks": len(ws._cache) if ws else 0,
                "wanted": len(ws._wanted) if ws else 0,
                "last_error": ws.last_error if ws else None,
                "cool_seconds_left": int(max(0, (ws._rate_limited_until - time.time())))
                if ws and ws.rate_limited()
                else 0,
            },
            "recent_rate_limits": list(self._rl_events[-10:]),
            "suggested_poll_seconds": suggested_poll_seconds(len(self._last_want_syms)),
            "drive_mode": "ws_tick",
            "fallback_poll_when": "ws_down_or_429",
            "adaptive_poll_table": {
                "<=20": 15,
                "21-50": 25,
                "51-100": 40,
                ">100": 60,
            },
        }

    def set_on_mark(self, callback: Callable[[str, float], None] | None) -> None:
        """Session hook: called on each WS LTP for a subscribed symbol."""
        self._on_mark = callback

    def _register_sid(self, symbol: str, segment: str, security_id: str) -> None:
        self._sid_to_sym[(str(segment), str(security_id))] = symbol.upper()

    def _on_ws_tick(self, segment: str, security_id: str, fields: dict[str, Any]) -> None:
        sym = self._sid_to_sym.get((str(segment), str(security_id)))
        if not sym:
            return
        ltp = fields.get("ltp")
        if ltp is None:
            return
        px = float(ltp)
        if px <= 0:
            return
        self._remember_mark(sym, px, "dhan-ws")
        self._dhan_feed_ok = True
        cb = self._on_mark
        if cb is not None:
            try:
                cb(sym, px)
            except Exception:
                pass

    def _clear_rate_limit_on_success(self) -> None:
        if not self._rest_cooling():
            self._429_strikes = 0

    def _remember_mark(self, symbol: str, price: float, source: str) -> None:
        self._mark_cache[symbol.upper()] = (float(price), time.monotonic(), source)

    def _cached_mark(
        self, symbol: str, *, max_stale_sec: float
    ) -> tuple[float, datetime, str] | None:
        row = self._mark_cache.get(symbol.upper())
        if not row:
            return None
        price, mono, source = row
        age = time.monotonic() - mono
        if age > max_stale_sec:
            return None
        tag = source if age < 2.0 else f"{source}-stale:{int(age)}s"
        return price, datetime.now(tz=IST), tag

    def close(self) -> None:
        self._on_mark = None
        try:
            from algo.providers.dhan.auth import TokenRotator

            TokenRotator.instance().off_renew(self._on_token_renewed)
        except Exception:
            pass
        if self._ws is not None:
            try:
                self._ws.set_on_tick(None)
            except Exception:
                pass
            self._ws.stop()
            self._ws = None
        if self._dhan is not None:
            self._dhan.close()
            self._dhan = None

    def wait_for_ws(self, timeout: float = 8.0) -> bool:
        """Start the feed and wait briefly for the socket to come up."""
        if self._ws is None:
            return False
        if self._ws.rate_limited():
            return False
        if not self._ws._thread or not self._ws._thread.is_alive():
            self._ws.start()
        deadline = time.time() + max(timeout, 0.5)
        while time.time() < deadline:
            if self._ws.connected:
                return True
            if self._ws.rate_limited():
                return False
            time.sleep(0.2)
        return bool(self._ws.connected)

    def ensure_subscribed(self, symbols: list[str]) -> None:
        """Add WebSocket instruments (additive). Prefer set_subscriptions after scan."""
        if self._ws is None:
            return
        cleaned = [s.upper() for s in symbols if s]
        # Additive want-list for diagnostics; set_subscriptions owns replace semantics.
        self._last_want_syms = list(dict.fromkeys([*self._last_want_syms, *cleaned]))
        # Only skip while the *socket* itself is rate-limited (reconnect cool).
        if self._ws.rate_limited():
            return
        if not self._ws._thread or not self._ws._thread.is_alive():
            self._ws.start()
        instruments: list[tuple[str, str]] = []
        for sym in cleaned:
            try:
                seg, sid = self._resolve_dhan_key(sym)
                instruments.append((seg, sid))
                self._register_sid(sym, seg, sid)
            except Exception:
                continue
        if instruments:
            self._ws.subscribe(instruments)

    def set_subscriptions(self, symbols: list[str]) -> None:
        """Replace WS want-set with exactly these symbols (colleague Kite pattern).

        After a timed REST/OHLC scan, call this with selected + open legs only —
        not the full NIFTY500 scan universe.
        """
        if self._ws is None:
            return
        cleaned = [s.upper() for s in symbols if s]
        self._last_want_syms = list(dict.fromkeys(cleaned))
        if self._ws.rate_limited():
            return
        if not self._ws._thread or not self._ws._thread.is_alive():
            self._ws.start()
        instruments: list[tuple[str, str]] = []
        sid_map: dict[tuple[str, str], str] = {}
        for sym in self._last_want_syms:
            try:
                seg, sid = self._resolve_dhan_key(sym)
                key = (seg, sid)
                instruments.append(key)
                sid_map[key] = sym
            except Exception:
                continue
        # Replace reverse map for the active want-set (drop unsubscribed names).
        self._sid_to_sym = sid_map
        self._ws.set_wanted(instruments)

    def _ws_quote(self, symbol: str) -> dict | None:
        if self._ws is None:
            return None
        try:
            segment, sid = self._resolve_dhan_key(symbol.upper())
        except Exception:
            return None
        return self._ws.get(segment, sid)

    def _wait_ws_ltp(self, symbol: str, *, wait_sec: float = 2.5) -> float | None:
        """Poll the WS cache briefly after subscribe — avoids needless REST."""
        deadline = time.time() + max(wait_sec, 0.0)
        while True:
            q = self._ws_quote(symbol)
            if q and q.get("ltp") is not None:
                return float(q["ltp"])
            if time.time() >= deadline:
                return None
            if self._ws is not None and self._ws.rate_limited() and not self._ws.connected:
                return None
            time.sleep(0.12)

    def equity_day_snapshot(self, symbol: str) -> dict[str, float | str] | None:
        """Today's OHLCV + prev close for scanner filters (gainers/losers)."""
        snaps = self.equity_day_snapshots([symbol])
        return snaps.get(symbol.upper())

    def equity_day_snapshots(
        self,
        symbols: list[str],
        *,
        prefer_rest: bool = False,
    ) -> dict[str, dict[str, float | str]]:
        """Batch day snapshots for scanners.

        prefer_rest=True (colleague / Kite-style pre-open scan):
          one Quote OHLC REST for the universe — does NOT WS-subscribe the scan list.
        Default: WS cache first, then soft REST fill when coverage is thin.
        """
        wanted = [s.upper() for s in symbols if s]
        out: dict[str, dict[str, float | str]] = {}
        if not wanted:
            return out

        if prefer_rest and self._dhan is not None and not self._rest_cooling():
            try:
                out.update(self._dhan_equity_snapshots(wanted))
                if out:
                    self._dhan_feed_ok = True
                    self._clear_rate_limit_on_success()
                return out
            except Exception:
                # Fall through to WS path if REST fails / 429.
                pass

        self.ensure_subscribed(wanted)
        # Give quote packets a moment to land before REST.
        time.sleep(0.6)
        for sym in wanted:
            q = self._ws_quote(sym)
            if not q or q.get("ltp") is None:
                continue
            o = q.get("open")
            h = q.get("high")
            low = q.get("low")
            c = q.get("ltp")
            prev = q.get("prev_close") or q.get("close") or c
            if o is None or h is None or low is None:
                continue
            out[sym] = {
                "symbol": sym,
                "open": float(o),
                "high": float(h),
                "low": float(low),
                "close": float(c),
                "prev_close": float(prev),
                "source": "dhan-ws",
            }

        missing = [s for s in wanted if s not in out]
        if (
            missing
            and self._dhan is not None
            and not self._rest_cooling()
            and (prefer_rest or len(out) < max(5, len(wanted) // 5))
        ):
            try:
                out.update(self._dhan_equity_snapshots(missing))
                if out:
                    self._dhan_feed_ok = True
                    self._clear_rate_limit_on_success()
            except Exception:
                if not out:
                    raise
        return out

    def mark_coverage(self, symbols: list[str], *, max_stale_sec: float = 180.0) -> float:
        """Fraction of symbols that already have a usable mark (WS cache or remembered)."""
        wanted = [s.upper() for s in symbols if s]
        if not wanted:
            return 1.0
        ok = 0
        for sym in wanted:
            if self._cached_mark(sym, max_stale_sec=max_stale_sec) is not None:
                ok += 1
                continue
            q = self._ws_quote(sym)
            if q and q.get("ltp") is not None:
                ok += 1
        return ok / float(len(wanted))

    def seed_ltps_rest_first(
        self,
        symbols: list[str],
        *,
        wait_ws_sec: float = 1.0,
    ) -> dict[str, float]:
        """Market policy: REST LTP batch first (verify + seed), then WS-subscribe for live.

        Pre-market / open / post-scan should call this before relying on websocket ticks.
        """
        wanted = list(dict.fromkeys(s.upper() for s in symbols if s))
        out: dict[str, float] = {}
        if not wanted:
            return out

        if self._dhan is not None and not self._rest_cooling():
            try:
                prices = self._dhan_ltp_batch(wanted)
                now = time.monotonic()
                for sym, price in prices.items():
                    self._rest_ltp_memo[sym] = (price, now)
                    self._remember_mark(sym, float(price), "dhan-rest")
                    out[sym] = float(price)
                self._dhan_feed_ok = True
                self._clear_rate_limit_on_success()
            except Exception:
                pass

        # After REST seed, lock WS want-set to these symbols only.
        self.set_subscriptions(wanted)
        if wait_ws_sec <= 0:
            return out

        deadline = time.time() + wait_ws_sec
        while time.time() < deadline:
            pending = False
            for sym in wanted:
                q = self._ws_quote(sym)
                if q and q.get("ltp") is not None:
                    px = float(q["ltp"])
                    self._remember_mark(sym, px, "dhan-ws")
                    out[sym] = px
                elif sym not in out:
                    pending = True
            if not pending:
                break
            time.sleep(0.12)
        return out

    def prefetch_ltps(
        self,
        symbols: list[str],
        *,
        wait_ws_sec: float = 2.5,
        allow_rest: bool = False,
        prefer_rest: bool = False,
    ) -> None:
        """Warm marks for active symbols.

        prefer_rest=True → REST batch first, then short WS wait (open / re-seed).
        Default → WS wait first; REST only fills missing when allow_rest=True.
        """
        wanted = [s.upper() for s in symbols if s]
        if not wanted:
            return
        if prefer_rest:
            self.seed_ltps_rest_first(wanted, wait_ws_sec=wait_ws_sec)
            return
        self.ensure_subscribed(wanted)
        deadline = time.time() + max(wait_ws_sec, 0.0)
        missing = list(wanted)
        while missing and time.time() < deadline:
            still: list[str] = []
            for sym in missing:
                q = self._ws_quote(sym)
                if q and q.get("ltp") is not None:
                    self._remember_mark(sym, float(q["ltp"]), "dhan-ws")
                    continue
                still.append(sym)
            missing = still
            if missing:
                time.sleep(0.12)
        if not missing or not allow_rest or self._dhan is None or self._rest_cooling():
            return
        try:
            prices = self._dhan_ltp_batch(missing)
            now = time.monotonic()
            for sym, price in prices.items():
                self._rest_ltp_memo[sym] = (price, now)
                self._remember_mark(sym, price, "dhan-rest")
            self._dhan_feed_ok = True
            self._clear_rate_limit_on_success()
        except Exception:
            return

    def _stale_budget(self, max_stale_sec: float) -> float:
        """While WS is reconnecting / 429-cooling, keep last marks longer for SL/TP."""
        if self._ws is not None and (self._ws.rate_limited() or not self._ws.connected):
            return max(float(max_stale_sec), 600.0)
        return float(max_stale_sec)

    def get_ltp(
        self,
        symbol: str,
        *,
        allow_rest: bool = False,
        wait_ws_sec: float = 1.5,
        max_stale_sec: float = 180.0,
    ) -> tuple[float, datetime, str]:
        """Live marks: WebSocket first, then last-known mark (keeps SL/TP alive)."""
        symbol = symbol.upper()
        if self._dhan is None and self._ws is None:
            raise ProviderError(
                self._last_dhan_error or "Dhan feed not configured (token / client id)",
                code="NO_DHAN",
            )

        self.ensure_subscribed([symbol])
        stale_budget = self._stale_budget(max_stale_sec)
        # Don't block the live loop while WS is cooling — use cache immediately.
        if self._ws is not None and self._ws.rate_limited():
            wait_ws_sec = min(wait_ws_sec, 0.05)

        q = self._ws_quote(symbol)
        if q and q.get("ltp") is not None:
            px = float(q["ltp"])
            self._remember_mark(symbol, px, "dhan-ws")
            return px, datetime.now(tz=IST), "dhan-ws"

        price = self._wait_ws_ltp(symbol, wait_sec=wait_ws_sec)
        if price is not None:
            self._remember_mark(symbol, price, "dhan-ws")
            return price, datetime.now(tz=IST), "dhan-ws"

        cached = self._cached_mark(symbol, max_stale_sec=stale_budget)
        if cached is not None:
            return cached

        # REST seed when WS is silent (connected-but-no-equity-ticks is common on Dhan).
        # Always for indexes; for equities when allow_rest or never marked this session.
        never_marked = symbol not in self._mark_cache
        if (
            self._dhan is not None
            and not self._rest_cooling()
            and (allow_rest or never_marked or symbol in INDEX_LTP_KEYS)
        ):
            try:
                px, ts = self._dhan_ltp(symbol)
                self._rest_ltp_memo[symbol] = (px, time.monotonic())
                self._remember_mark(symbol, px, "dhan-rest")
                self._dhan_feed_ok = True
                self._clear_rate_limit_on_success()
                return px, ts, "dhan-rest"
            except Exception:
                pass

        raise ProviderError(
            "No Dhan mark yet — REST/WS both empty; waiting for next poll",
            code="NO_TICK",
        )

    @property
    def feed_status(self) -> str:
        parts: list[str] = []
        if self._ws is not None and self._ws.connected:
            parts.append(self._ws.status)
        elif self._ws is not None and self._ws.rate_limited():
            wait = int(max(0, self._ws._rate_limited_until - time.time()))
            parts.append(f"dhan-ws reconnect in {wait}s")
        elif self._ws is not None:
            parts.append(self._ws.status)
        if self._rest_cooling():
            wait = int(max(0, self._rest_cooldown_until - time.time()))
            parts.append(f"REST paused {wait}s (WS marks continue)")
        return " · ".join(parts) if parts else "dhan (probing)"

    def as_bar(self, symbol: str) -> Bar:
        price, ts, _src = self.get_ltp(symbol)
        return Bar(timestamp=ts, open=price, high=price, low=price, close=price, volume=0)

    def option_premium_bar(
        self,
        underlying: str,
        *,
        option_type: str = "CE",
        strike_mode: str = "ATM",
        strike_step: int = 50,
        state: dict[str, float] | None = None,
    ) -> tuple[Bar, dict[str, float]]:
        """Synthetic option premium from spot — for paper without option-chain Data API."""
        st = state or {}
        try:
            spot, ts, _ = self.get_ltp(
                underlying, allow_rest=False, wait_ws_sec=0.4, max_stale_sec=180
            )
        except Exception:
            # Keep option SL/TP alive on last known spot while WS reconnects.
            if st.get("spot") or st.get("prev_spot"):
                spot = float(st.get("spot") or st.get("prev_spot") or 0)
                ts = datetime.now(tz=IST)
            else:
                raise
        step = max(int(strike_step), 1)
        atm = int(round(spot / step) * step)
        if strike_mode == "ATM+1":
            strike = atm + step
        elif strike_mode == "ATM-1":
            strike = atm - step
        else:
            strike = atm
        if st.get("strike"):
            strike = int(st["strike"])
        prev_spot = float(st.get("prev_spot", spot))
        premium = float(st.get("premium", _seed_premium(spot, strike, option_type)))
        move = spot - prev_spot
        delta = 0.55 if option_type.upper() == "CE" else -0.45
        premium = max(5.0, premium + delta * move)
        high = premium + abs(move) * 0.15
        low = max(1.0, premium - abs(move) * 0.15)
        bar = Bar(timestamp=ts, open=premium, high=max(high, premium), low=min(low, premium), close=premium, volume=float(strike))
        new_state = {"prev_spot": spot, "premium": premium, "strike": float(strike), "spot": spot}
        return bar, new_state

    def load_option_premium_bars(
        self,
        underlying: str,
        *,
        option_type: str = "CE",
        strike_mode: str = "ATM",
        strike_step: int = 50,
        interval: str = "1m",
        range_: str = "5d",
    ) -> list[Bar]:
        spot_bars = self.load_public_bars(underlying, interval=interval, range_=range_)
        if not spot_bars:
            return []
        step = max(int(strike_step), 1)
        first = spot_bars[0].close
        atm = int(round(first / step) * step)
        if strike_mode == "ATM+1":
            strike = atm + step
        elif strike_mode == "ATM-1":
            strike = atm - step
        else:
            strike = atm
        premium = _seed_premium(first, strike, option_type)
        prev = first
        out: list[Bar] = []
        delta = 0.55 if option_type.upper() == "CE" else -0.45
        for sb in spot_bars:
            move = sb.close - prev
            premium = max(5.0, premium + delta * move)
            hi = premium + max(sb.high - sb.close, 0) * abs(delta)
            lo = max(1.0, premium - max(sb.close - sb.low, 0) * abs(delta))
            out.append(
                Bar(
                    timestamp=sb.timestamp,
                    open=premium,
                    high=max(hi, premium),
                    low=min(lo, premium),
                    close=premium,
                    volume=float(strike),
                )
            )
            prev = sb.close
        return out

    def load_public_bars(self, symbol: str, *, interval: str = "5m", range_: str = "5d") -> list[Bar]:
        """Historical bars from Dhan charts (name kept for call-site compatibility)."""
        if self._dhan is None:
            raise ProviderError(
                self._last_dhan_error or "Dhan client required for historical bars",
                code="NO_DHAN",
            )
        if self._rest_cooling():
            wait = int(max(0, self._rest_cooldown_until - time.time()))
            raise ProviderError(
                f"Dhan charts deferred {wait}s after REST 429 (WS live marks continue)",
                code="429",
            )
        segment, security_id = self._resolve_dhan_key(symbol.upper())
        instrument = "INDEX" if segment == "IDX_I" else "EQUITY"
        interval_key = {"1m": "1", "1": "1", "5m": "5", "5": "5", "15m": "15", "15": "15"}.get(
            interval, "5"
        )
        days = _range_to_days(range_)
        end = date.today()
        start = end - timedelta(days=days)
        self._rest_calls["data"] = self._rest_calls.get("data", 0) + 1
        try:
            payload = self._dhan.post_json(
                "/charts/intraday",
                {
                    "securityId": str(security_id),
                    "exchangeSegment": segment,
                    "instrument": instrument,
                    "interval": interval_key,
                    "oi": False,
                    "fromDate": start.isoformat(),
                    "toDate": end.isoformat(),
                },
            )
        except Exception as exc:
            self._mark_rate_limited(exc, kind="data", context=f"charts:{symbol}:{interval}")
            raise
        self._rest_ok["data"] = self._rest_ok.get("data", 0) + 1
        if not isinstance(payload, dict):
            payload = {}
        candles = parse_columnar_candles(
            payload,
            instrument_id=f"{segment}:{security_id}",
            timeframe=interval if interval.endswith("m") else f"{interval}m",
            source="dhan",
        )
        self._clear_rate_limit_on_success()
        return [
            Bar(
                timestamp=c.timestamp.astimezone(IST)
                if c.timestamp.tzinfo
                else c.timestamp.replace(tzinfo=IST),
                open=float(c.open),
                high=float(c.high),
                low=float(c.low),
                close=float(c.close),
                volume=float(c.volume or 0),
            )
            for c in candles
        ]

    def synthetic_bars(self, symbol: str, *, n: int = 300, start: float | None = None) -> list[Bar]:
        """Deterministic walk for offline UI demos when no feed is available."""
        if start is None:
            price, _, _ = self.get_ltp(symbol)
        else:
            price = start
        now = datetime.now(tz=IST)
        bars: list[Bar] = []
        px = float(price)
        for i in range(n):
            delta = ((i % 17) - 8) * 0.35
            px = max(100.0, px + delta)
            ts = now - timedelta(minutes=5 * (n - i))
            bars.append(Bar(timestamp=ts, open=px, high=px + 1, low=px - 1, close=px, volume=0))
        return bars

    def _resolve_dhan_key(self, symbol: str) -> tuple[str, str]:
        if symbol in INDEX_LTP_KEYS:
            return INDEX_LTP_KEYS[symbol]
        sid = nse_eq_security_id(symbol)
        if sid:
            return "NSE_EQ", sid
        raise ValueError(f"No Dhan securityId for {symbol}")

    def _dhan_ltp(self, symbol: str) -> tuple[float, datetime]:
        if self._dhan is None:
            raise ProviderError("Dhan client not configured", code="NO_DHAN")
        segment, security_id = self._resolve_dhan_key(symbol)
        self._rest_calls["quote"] = self._rest_calls.get("quote", 0) + 1
        try:
            payload = self._dhan.post_json("/marketfeed/ltp", {segment: [int(security_id)]})
        except Exception as exc:
            self._mark_rate_limited(exc, kind="quote", context=f"ltp:{symbol}")
            raise
        self._rest_ok["quote"] = self._rest_ok.get("quote", 0) + 1
        price = _extract_ltp(payload, segment, security_id)
        return price, datetime.now(tz=IST)

    def _dhan_ltp_batch(self, symbols: list[str]) -> dict[str, float]:
        """One marketfeed/ltp call for many symbols, grouped by segment."""
        if self._dhan is None:
            raise ProviderError("Dhan client not configured", code="NO_DHAN")
        by_seg: dict[str, list[int]] = {}
        sid_to_sym: dict[tuple[str, str], str] = {}
        for sym in symbols:
            try:
                segment, security_id = self._resolve_dhan_key(sym.upper())
            except Exception:
                continue
            by_seg.setdefault(segment, []).append(int(security_id))
            sid_to_sym[(segment, str(security_id))] = sym.upper()
        if not by_seg:
            return {}
        body = {seg: ids for seg, ids in by_seg.items()}
        self._rest_calls["quote"] = self._rest_calls.get("quote", 0) + 1
        try:
            payload = self._dhan.post_json("/marketfeed/ltp", body)
        except Exception as exc:
            self._mark_rate_limited(
                exc, kind="quote", context=f"ltp_batch:n={len(symbols)}"
            )
            raise
        self._rest_ok["quote"] = self._rest_ok.get("quote", 0) + 1
        out: dict[str, float] = {}
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            raise ValueError(f"Unexpected LTP batch payload: {payload}")
        for segment, rows in data.items():
            if not isinstance(rows, dict):
                continue
            for sid, row in rows.items():
                sym = sid_to_sym.get((str(segment), str(sid)))
                if not sym:
                    continue
                try:
                    out[sym] = _extract_ltp({"data": {segment: {str(sid): row}}}, str(segment), str(sid))
                except Exception:
                    continue
        return out

    def _dhan_equity_snapshots(self, symbols: list[str]) -> dict[str, dict[str, float | str]]:
        if self._dhan is None:
            raise ProviderError("Dhan client not configured", code="NO_DHAN")
        sid_to_sym: dict[str, str] = {}
        ids: list[int] = []
        for sym in symbols:
            if sym in INDEX_LTP_KEYS:
                continue
            sid = nse_eq_security_id(sym)
            if not sid:
                continue
            sid_to_sym[sid] = sym
            ids.append(int(sid))
        out: dict[str, dict[str, float | str]] = {}
        for i in range(0, len(ids), _DHAN_BATCH):
            chunk = ids[i : i + _DHAN_BATCH]
            if not chunk:
                continue
            self._rest_calls["quote"] = self._rest_calls.get("quote", 0) + 1
            try:
                payload = self._dhan.post_json("/marketfeed/ohlc", {"NSE_EQ": chunk})
            except Exception as exc:
                self._mark_rate_limited(
                    exc, kind="quote", context=f"ohlc_batch:n={len(chunk)}"
                )
                raise
            self._rest_ok["quote"] = self._rest_ok.get("quote", 0) + 1
            out.update(_extract_ohlc_batch(payload, "NSE_EQ", sid_to_sym))
        return out


def _range_to_days(range_: str) -> int:
    raw = (range_ or "5d").strip().lower()
    if raw.endswith("d") and raw[:-1].isdigit():
        return max(1, int(raw[:-1]))
    if raw.isdigit():
        return max(1, int(raw))
    return 5


def _extract_ltp(payload: Any, segment: str, security_id: str) -> float:
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        raise ValueError(f"Unexpected LTP payload: {payload}")
    seg = data.get(segment) or data.get(segment.upper())
    if not isinstance(seg, dict):
        raise ValueError(f"No segment {segment} in LTP response: {data}")
    row = seg.get(str(security_id)) or seg.get(int(security_id))  # type: ignore[arg-type]
    if row is None:
        if len(seg) == 1:
            row = next(iter(seg.values()))
        else:
            raise ValueError(f"No security {security_id} in LTP response: {seg}")
    if isinstance(row, dict):
        for key in ("last_price", "ltp", "Last_Price", "lastPrice"):
            if key in row and row[key] is not None:
                return float(row[key])
    if isinstance(row, (int, float)):
        return float(row)
    raise ValueError(f"Cannot parse LTP row: {row}")


def _extract_ohlc_batch(
    payload: Any,
    segment: str,
    sid_to_sym: dict[str, str],
) -> dict[str, dict[str, float | str]]:
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        raise ValueError(f"Unexpected OHLC payload: {payload}")
    seg = data.get(segment) or data.get(segment.upper())
    if not isinstance(seg, dict):
        raise ValueError(f"No segment {segment} in OHLC response")
    out: dict[str, dict[str, float | str]] = {}
    for sid, row in seg.items():
        sym = sid_to_sym.get(str(sid))
        if not sym or not isinstance(row, dict):
            continue
        ohlc = row.get("ohlc") if isinstance(row.get("ohlc"), dict) else {}
        last = row.get("last_price")
        if last is None:
            continue
        day_open = float(ohlc.get("open") or last)
        day_high = float(ohlc.get("high") or last)
        day_low = float(ohlc.get("low") or last)
        # During session, ohlc.close is typically previous close.
        prev = float(ohlc.get("close") or last)
        out[sym] = {
            "symbol": sym,
            "open": day_open,
            "high": day_high,
            "low": day_low,
            "close": float(last),
            "prev_close": prev,
            "source": "dhan",
        }
    return out


def _seed_premium(spot: float, strike: int, option_type: str) -> float:
    if option_type.upper() == "CE":
        intrinsic = max(spot - strike, 0.0)
    else:
        intrinsic = max(strike - spot, 0.0)
    return max(40.0, intrinsic + 90.0)
