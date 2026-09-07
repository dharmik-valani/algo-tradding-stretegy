from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo
import time

import httpx

from algo.config import Settings
from algo.ingest.rate_limiter import RateLimiter
from algo.paper.dhan_equity_ids import nse_eq_security_id
from algo.paper.models import Bar
from algo.providers.base import ProviderError
from algo.providers.dhan.client import DhanClient
from algo.providers.dhan.live_feed import DhanLiveFeed

IST = ZoneInfo("Asia/Kolkata")

INDEX_LTP_KEYS = {
    "NIFTY": ("IDX_I", "13"),
    "BANKNIFTY": ("IDX_I", "25"),
    "FINNIFTY": ("IDX_I", "27"),
    "MIDCPNIFTY": ("IDX_I", "442"),
    "SENSEX": ("IDX_I", "51"),
}

# Public Yahoo symbols for paper prices when Dhan Data API is not subscribed.
YAHOO_SYMBOLS = {
    "NIFTY": "^NSEI",
    "BANKNIFTY": "^NSEBANK",
    "FINNIFTY": "NIFTY_FIN_SERVICE.NS",
    "MIDCPNIFTY": "NIFTY_MID_SELECT.NS",
    "SENSEX": "^BSESN",
    "RELIANCE": "RELIANCE.NS",
    "HDFCBANK": "HDFCBANK.NS",
    "ICICIBANK": "ICICIBANK.NS",
    "INFY": "INFY.NS",
    "TCS": "TCS.NS",
    "SBIN": "SBIN.NS",
    "ITC": "ITC.NS",
    "BHARTIARTL": "BHARTIARTL.NS",
    "LT": "LT.NS",
    "AXISBANK": "AXISBANK.NS",
}

# Dhan marketfeed allows up to 1000 ids / request.
_DHAN_BATCH = 900


class LiveQuoteProvider:
    """Paper price feed.

    - public: Yahoo (no Dhan Data API)
    - dhan / auto: prefer Dhan WebSocket live feed, REST LTP/OHLC fallback, then Yahoo (auto)
    """

    def __init__(
        self,
        settings: Settings,
        client: DhanClient | None = None,
        *,
        enable_dhan_feed: bool = True,
    ) -> None:
        self.settings = settings
        self.source = (getattr(settings, "paper_price_source", None) or "auto").lower()
        live_url = settings.yaml_config.get("dhan", {}).get("base_url", "https://api.dhan.co/v2")
        self._dhan: DhanClient | None = None
        self._ws: DhanLiveFeed | None = None
        self._http = httpx.Client(timeout=20.0, headers={"User-Agent": "algo-paper-desk/0.1"})
        self._dhan_feed_ok: bool | None = None  # None=unknown, False=disable REST for session
        self._last_dhan_error: str | None = None
        self._token: str = ""
        self._rest_cooldown_until = 0.0
        self._enable_dhan_feed = enable_dhan_feed and self.source in {"dhan", "auto", "dhan-ws"}

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
                        # Paper live poll is slow; keep REST gentle to avoid 429.
                        limiter=RateLimiter(min(settings.requests_per_second, 0.5), settings.requests_per_day),
                    )
                    self._ws = DhanLiveFeed(
                        client_id=settings.dhan_client_id,
                        token_provider=self._live_token,
                        prefer_quote=True,
                    )
                    # Lazy-start on first subscribe — avoids opening sockets during Yahoo-only replay.
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
        return time.time() < self._rest_cooldown_until

    def _mark_rate_limited(self, exc: Exception | str, *, seconds: float = 180.0) -> None:
        msg = str(exc)
        self._last_dhan_error = msg
        self._dhan_feed_ok = False
        if "429" in msg or "too many" in msg.lower():
            self._rest_cooldown_until = max(self._rest_cooldown_until, time.time() + seconds)
            if self._ws is not None:
                self._ws._rate_limited_until = max(
                    getattr(self._ws, "_rate_limited_until", 0.0),
                    time.time() + seconds,
                )

    def close(self) -> None:
        try:
            from algo.providers.dhan.auth import TokenRotator

            TokenRotator.instance().off_renew(self._on_token_renewed)
        except Exception:
            pass
        if self._ws is not None:
            self._ws.stop()
            self._ws = None
        if self._dhan is not None:
            self._dhan.close()
            self._dhan = None
        self._http.close()

    def ensure_subscribed(self, symbols: list[str]) -> None:
        """Subscribe WebSocket instruments for active paper symbols."""
        if self._ws is None:
            return
        if self._ws.rate_limited():
            return
        if not self._ws._thread or not self._ws._thread.is_alive():
            self._ws.start()
        instruments: list[tuple[str, str]] = []
        for sym in symbols:
            try:
                instruments.append(self._resolve_dhan_key(sym.upper()))
            except Exception:
                continue
        if instruments:
            self._ws.subscribe(instruments)

    def _ws_quote(self, symbol: str) -> dict | None:
        if self._ws is None:
            return None
        try:
            segment, sid = self._resolve_dhan_key(symbol.upper())
        except Exception:
            return None
        return self._ws.get(segment, sid)

    def equity_day_snapshot(self, symbol: str) -> dict[str, float | str] | None:
        """Today's OHLCV + prev close for scanner filters (gainers/losers)."""
        snaps = self.equity_day_snapshots([symbol])
        return snaps.get(symbol.upper())

    def equity_day_snapshots(self, symbols: list[str]) -> dict[str, dict[str, float | str]]:
        """Batch day snapshots — prefers WS cache / Dhan OHLC, falls back to Yahoo."""
        wanted = [s.upper() for s in symbols if s]
        out: dict[str, dict[str, float | str]] = {}
        if not wanted:
            return out

        self.ensure_subscribed(wanted)
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
            and self.source in {"dhan", "auto", "dhan-ws"}
            and self._dhan is not None
            and self._dhan_feed_ok is not False
            and not self._rest_cooling()
            and not (self._ws is not None and self._ws.rate_limited())
        ):
            try:
                out.update(self._dhan_equity_snapshots(missing))
                if out:
                    self._dhan_feed_ok = True
            except Exception as exc:
                self._mark_rate_limited(exc)
                if self.source == "dhan":
                    raise

        missing = [s for s in wanted if s not in out]
        if missing and self.source != "dhan" and self.source != "dhan-ws":
            for sym in missing:
                try:
                    snap = self._yahoo_equity_snapshot(sym)
                    if snap:
                        out[sym] = snap
                except Exception:
                    continue
        return out

    def get_ltp(self, symbol: str) -> tuple[float, datetime, str]:
        symbol = symbol.upper()
        self.ensure_subscribed([symbol])
        q = self._ws_quote(symbol)
        if q and q.get("ltp") is not None:
            return float(q["ltp"]), datetime.now(tz=IST), "dhan-ws"

        if self.source in {"dhan", "dhan-ws"}:
            if self._rest_cooling() or (self._ws is not None and self._ws.rate_limited()):
                raise ProviderError(self._last_dhan_error or "Dhan rate limited", code="429")
            try:
                return (*self._dhan_ltp(symbol), "dhan")
            except Exception as exc:
                self._mark_rate_limited(exc)
                raise
        if self.source == "public":
            return (*self._yahoo_ltp(symbol), "public")
        # auto — skip REST while cooling; use Yahoo quietly
        if (
            self._dhan is not None
            and self._dhan_feed_ok is not False
            and not self._rest_cooling()
            and not (self._ws is not None and self._ws.rate_limited())
        ):
            try:
                price, ts = self._dhan_ltp(symbol)
                self._dhan_feed_ok = True
                return price, ts, "dhan"
            except Exception as exc:
                self._mark_rate_limited(exc)
        return (*self._yahoo_ltp(symbol), "public")

    @property
    def feed_status(self) -> str:
        if self.source == "public":
            return "public (Yahoo)"
        if self._ws is not None and self._ws.connected:
            return self._ws.status
        if self._rest_cooling() or (self._ws is not None and self._ws.rate_limited()):
            wait = 0
            if self._rest_cooling():
                wait = max(wait, int(self._rest_cooldown_until - time.time()))
            if self._ws is not None and self._ws.rate_limited():
                wait = max(wait, int(self._ws._rate_limited_until - time.time()))
            return f"public (Yahoo; Dhan cooling {wait}s after 429)"
        if self._dhan_feed_ok is True:
            return "dhan-rest"
        parts = []
        if self._ws is not None:
            parts.append(self._ws.status)
        if self._last_dhan_error and "429" not in (self._last_dhan_error or ""):
            parts.append(self._last_dhan_error)
        if self.source in {"auto"}:
            return "public (Yahoo; " + ("; ".join(parts) or "probing Dhan") + ")"
        return "; ".join(parts) or f"{self.source} (probing)"

    def _yahoo_symbol(self, symbol: str) -> str:
        symbol = symbol.upper()
        if symbol in YAHOO_SYMBOLS:
            return YAHOO_SYMBOLS[symbol]
        return f"{symbol}.NS"

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
        spot, ts, _ = self.get_ltp(underlying)
        step = max(int(strike_step), 1)
        atm = int(round(spot / step) * step)
        if strike_mode == "ATM+1":
            strike = atm + step
        elif strike_mode == "ATM-1":
            strike = atm - step
        else:
            strike = atm
        st = state or {}
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
        """Historical-ish bars from Yahoo for Replay without Dhan Data API."""
        ysym = self._yahoo_symbol(symbol)
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ysym}"
        response = self._http.get(url, params={"interval": interval, "range": range_})
        response.raise_for_status()
        payload = response.json()
        result = (payload.get("chart") or {}).get("result") or []
        if not result:
            raise ValueError(f"No Yahoo chart data for {symbol}")
        block = result[0]
        ts_list = block.get("timestamp") or []
        quote = ((block.get("indicators") or {}).get("quote") or [{}])[0]
        opens = quote.get("open") or []
        highs = quote.get("high") or []
        lows = quote.get("low") or []
        closes = quote.get("close") or []
        volumes = quote.get("volume") or []
        bars: list[Bar] = []
        for i, ts in enumerate(ts_list):
            close = closes[i] if i < len(closes) else None
            if close is None:
                continue
            bars.append(
                Bar(
                    timestamp=datetime.fromtimestamp(int(ts), tz=IST),
                    open=float(opens[i] if i < len(opens) and opens[i] is not None else close),
                    high=float(highs[i] if i < len(highs) and highs[i] is not None else close),
                    low=float(lows[i] if i < len(lows) and lows[i] is not None else close),
                    close=float(close),
                    volume=float(volumes[i] if i < len(volumes) and volumes[i] is not None else 0),
                )
            )
        return bars

    def synthetic_bars(self, symbol: str, *, n: int = 300, start: float | None = None) -> list[Bar]:
        """Deterministic walk for offline UI demos when no feed is available."""
        price, _, _ = self.get_ltp(symbol) if start is None else (start, datetime.now(tz=IST), "manual")
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
        payload = self._dhan.post_json("/marketfeed/ltp", {segment: [int(security_id)]})
        price = _extract_ltp(payload, segment, security_id)
        return price, datetime.now(tz=IST)

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
            payload = self._dhan.post_json("/marketfeed/ohlc", {"NSE_EQ": chunk})
            out.update(_extract_ohlc_batch(payload, "NSE_EQ", sid_to_sym))
        return out

    def _yahoo_equity_snapshot(self, symbol: str) -> dict[str, float | str] | None:
        ysym = self._yahoo_symbol(symbol)
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ysym}"
        response = self._http.get(url, params={"interval": "5m", "range": "1d"})
        response.raise_for_status()
        payload = response.json()
        result = (payload.get("chart") or {}).get("result") or []
        if not result:
            return None
        block = result[0]
        meta = block.get("meta") or {}
        quote = ((block.get("indicators") or {}).get("quote") or [{}])[0]
        opens = [x for x in (quote.get("open") or []) if x is not None]
        highs = [x for x in (quote.get("high") or []) if x is not None]
        lows = [x for x in (quote.get("low") or []) if x is not None]
        closes = [x for x in (quote.get("close") or []) if x is not None]
        if not opens or not closes:
            return None
        day_open = float(opens[0])
        day_high = float(max(highs)) if highs else day_open
        day_low = float(min(lows)) if lows else day_open
        last = float(closes[-1])
        prev = meta.get("chartPreviousClose") or meta.get("previousClose") or day_open
        return {
            "symbol": symbol.upper(),
            "open": day_open,
            "high": day_high,
            "low": day_low,
            "close": last,
            "prev_close": float(prev),
            "source": "public",
        }

    def _yahoo_ltp(self, symbol: str) -> tuple[float, datetime]:
        ysym = self._yahoo_symbol(symbol)
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ysym}"
        response = self._http.get(url, params={"interval": "1m", "range": "1d"})
        response.raise_for_status()
        payload = response.json()
        result = (payload.get("chart") or {}).get("result") or []
        if not result:
            raise ValueError(f"No public quote for {symbol}")
        meta = result[0].get("meta") or {}
        price = meta.get("regularMarketPrice") or meta.get("postMarketPrice") or meta.get("previousClose")
        if price is None:
            closes = (((result[0].get("indicators") or {}).get("quote") or [{}])[0].get("close") or [])
            closes = [c for c in closes if c is not None]
            if not closes:
                raise ValueError(f"Empty public quote for {symbol}")
            price = closes[-1]
        return float(price), datetime.now(tz=IST)


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
