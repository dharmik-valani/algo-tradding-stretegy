from __future__ import annotations

"""DhanHQ Live Market Feed (WebSocket) for paper trading marks.

Docs: https://docs.dhanhq.co/api/v2/guides/live-market-feed
Endpoint: wss://api-feed.dhan.co?version=2&token=...&clientId=...&authType=2

We intentionally keep a thin native client (same binary protocol as
``dhanhq.MarketFeed``) so paper can dynamically subscribe/reconnect when
``TokenRotator`` refreshes the JWT. Auth renew/profile uses official
``dhanhq.DhanLogin`` — see ``algo.providers.dhan.auth``.

Subscribe Quote (RequestCode 17) so we get LTP + day OHLC in one stream.
Responses are little-endian binary packets.
"""

import asyncio
import json
import logging
import struct
import threading
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")

WS_URL = "wss://api-feed.dhan.co"

# Feed request codes (annexure)
REQ_DISCONNECT = 12
REQ_SUB_TICKER = 15
REQ_SUB_QUOTE = 17

# Feed response codes
RESP_INDEX = 1
RESP_TICKER = 2
RESP_QUOTE = 4
RESP_OI = 5
RESP_PREV_CLOSE = 6
RESP_FULL = 8
RESP_DISCONNECT = 50

SEGMENT_BY_CODE = {
    0: "IDX_I",
    1: "NSE_EQ",
    2: "NSE_FNO",
    3: "NSE_CURRENCY",
    4: "BSE_EQ",
    5: "MCX_COMM",
    7: "BSE_CURRENCY",
    8: "BSE_FNO",
}


def _u8(buf: bytes, i: int) -> int:
    return buf[i]


def _i16(buf: bytes, i: int) -> int:
    return struct.unpack_from("<h", buf, i)[0]


def _i32(buf: bytes, i: int) -> int:
    return struct.unpack_from("<i", buf, i)[0]


def _f32(buf: bytes, i: int) -> float:
    return float(struct.unpack_from("<f", buf, i)[0])


def parse_header(buf: bytes) -> tuple[int, int, str, str]:
    if len(buf) < 8:
        raise ValueError("short header")
    code = _u8(buf, 0)
    length = _i16(buf, 1)
    seg_code = _u8(buf, 3)
    security_id = str(_i32(buf, 4))
    segment = SEGMENT_BY_CODE.get(seg_code, str(seg_code))
    return code, length, segment, security_id


def parse_ticker_payload(buf: bytes) -> dict[str, Any]:
    # header(8) + ltp(4) + ltt(4)
    if len(buf) < 16:
        raise ValueError("short ticker")
    code, _, segment, sid = parse_header(buf)
    return {
        "feed_code": code,
        "segment": segment,
        "security_id": sid,
        "ltp": _f32(buf, 8),
        "ltt": _i32(buf, 12),
    }


def parse_quote_payload(buf: bytes) -> dict[str, Any]:
    # header(8) + ltp + last_qty + ltt + atp + volume + sell + buy + open + close + high + low
    if len(buf) < 50:
        raise ValueError("short quote")
    code, _, segment, sid = parse_header(buf)
    return {
        "feed_code": code,
        "segment": segment,
        "security_id": sid,
        "ltp": _f32(buf, 8),
        "last_qty": _i16(buf, 12),
        "ltt": _i32(buf, 14),
        "atp": _f32(buf, 18),
        "volume": _i32(buf, 22),
        "sell_qty": _i32(buf, 26),
        "buy_qty": _i32(buf, 30),
        "open": _f32(buf, 34),
        "close": _f32(buf, 38),
        "high": _f32(buf, 42),
        "low": _f32(buf, 46),
    }


def parse_prev_close_payload(buf: bytes) -> dict[str, Any]:
    if len(buf) < 16:
        raise ValueError("short prev close")
    code, _, segment, sid = parse_header(buf)
    return {
        "feed_code": code,
        "segment": segment,
        "security_id": sid,
        "prev_close": _f32(buf, 8),
        "prev_oi": _i32(buf, 12),
    }


class DhanLiveFeed:
    """Background WebSocket quote cache for paper marks."""

    def __init__(
        self,
        *,
        client_id: str,
        token_provider: Callable[[], str],
        prefer_quote: bool = True,
    ) -> None:
        self.client_id = client_id
        self._token_provider = token_provider
        self._prefer_quote = prefer_quote
        self._lock = threading.RLock()
        self._wanted: set[tuple[str, str]] = set()  # (segment, security_id)
        self._subscribed: set[tuple[str, str]] = set()
        self._cache: dict[tuple[str, str], dict[str, Any]] = {}
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._connected = False
        self._last_error: str | None = None
        self._last_msg_at: float | None = None
        self._wake: asyncio.Event | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._force_reconnect = False
        self._ws_ref: Any = None
        self._rate_limited_until = 0.0
        self._backoff = 1.0
        self._429_hits = 0

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def last_error(self) -> str | None:
        return self._last_error

    @property
    def status(self) -> str:
        now = time.time()
        if self._rate_limited_until > now:
            wait = int(self._rate_limited_until - now)
            return f"dhan-ws cooling down ({wait}s; HTTP 429)"
        if self._connected:
            n = len(self._cache)
            return f"dhan-ws ({n} ticks)"
        if self._last_error:
            return f"dhan-ws down ({self._last_error})"
        return "dhan-ws connecting"

    def rate_limited(self) -> bool:
        return time.time() < self._rate_limited_until

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._thread_main, name="dhan-live-feed", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._force_reconnect = True
        self._signal_wake()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3.0)
        self._connected = False
        self._ws_ref = None

    def force_reconnect(self) -> None:
        """Drop the socket so the next loop iteration reconnects with a fresh token."""
        self._force_reconnect = True
        self._signal_wake()

    def subscribe(self, instruments: list[tuple[str, str]]) -> None:
        """instruments: list of (exchange_segment, security_id)."""
        changed = False
        with self._lock:
            for seg, sid in instruments:
                key = (str(seg), str(sid))
                if key not in self._wanted:
                    self._wanted.add(key)
                    changed = True
        if changed:
            self._signal_wake()

    def get(self, segment: str, security_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._cache.get((str(segment), str(security_id)))
            return dict(row) if row else None

    def _signal_wake(self) -> None:
        loop = self._loop
        wake = self._wake
        if loop and wake and loop.is_running():
            loop.call_soon_threadsafe(wake.set)

    def _thread_main(self) -> None:
        try:
            asyncio.run(self._async_main())
        except Exception as exc:
            self._last_error = str(exc)
            self._connected = False
            logger.exception("Dhan live feed crashed")

    async def _async_main(self) -> None:
        import ssl

        import websockets
        from websockets.exceptions import ConnectionClosed

        ssl_ctx = None
        try:
            import certifi

            ssl_ctx = ssl.create_default_context(cafile=certifi.where())
        except Exception:
            ssl_ctx = ssl.create_default_context()

        self._loop = asyncio.get_running_loop()
        self._wake = asyncio.Event()
        backoff = 1.0
        # Escalate cool on repeated 429s so we don't stampede reconnects.
        # Cap at 60s (never the old ~180s double-sleep).
        max_429_cool = 60.0
        while not self._stop.is_set():
            now = time.time()
            if self._rate_limited_until > now:
                wait = self._rate_limited_until - now
                self._last_error = f"HTTP 429 — waiting {int(wait)}s"
                self._connected = False
                await asyncio.sleep(min(wait, 5.0))
                continue
            token = ""
            try:
                token = (self._token_provider() or "").strip()
            except Exception as exc:
                self._last_error = str(exc)
                await asyncio.sleep(min(backoff, 30))
                backoff = min(backoff * 2, 30)
                continue
            if not token or not self.client_id:
                self._last_error = "missing token/client id"
                await asyncio.sleep(5)
                continue
            url = (
                f"{WS_URL}?version=2&token={token}"
                f"&clientId={self.client_id}&authType=2"
            )
            hit_429 = False
            try:
                async with websockets.connect(
                    url,
                    ping_interval=20,
                    ping_timeout=40,
                    max_size=2_000_000,
                    open_timeout=20,
                    ssl=ssl_ctx,
                ) as ws:
                    self._ws_ref = ws
                    self._connected = True
                    self._last_error = None
                    self._force_reconnect = False
                    # Don't zero 429 hits on a flash connect — only after we stay up.
                    connect_ok_at = time.time()
                    backoff = 1.0
                    self._backoff = 1.0
                    with self._lock:
                        self._subscribed.clear()
                    await self._flush_subscriptions(ws)
                    while not self._stop.is_set():
                        if self._429_hits and (time.time() - connect_ok_at) >= 30.0:
                            self._429_hits = 0
                        if self._force_reconnect:
                            self._force_reconnect = False
                            await ws.close()
                            break
                        self._wake.clear()
                        try:
                            msg = await asyncio.wait_for(ws.recv(), timeout=2.0)
                        except TimeoutError:
                            await self._flush_subscriptions(ws)
                            continue
                        if isinstance(msg, bytes):
                            self._handle_binary(msg)
                        elif isinstance(msg, str):
                            logger.debug("dhan-ws text: %s", msg[:200])
                    self._ws_ref = None
            except ConnectionClosed as exc:
                self._connected = False
                self._last_error = f"closed {exc.code}"
            except Exception as exc:
                self._connected = False
                err = str(exc)
                self._last_error = err
                if "429" in err or "Too many" in err.lower():
                    self._429_hits = min(self._429_hits + 1, 6)
                    # 15 → 25 → 35 → 45 → 55 → 60 (stop hammering; marks still serve)
                    cool = min(15.0 + (self._429_hits - 1) * 10.0, max_429_cool)
                    self._rate_limited_until = time.time() + cool
                    hit_429 = True
                    self._last_error = (
                        f"HTTP 429 — reconnect in {int(cool)}s (hit {self._429_hits})"
                    )
                    logger.warning(
                        "dhan-ws rate limited; reconnect in %.0fs (hit %s)",
                        cool,
                        self._429_hits,
                    )
                else:
                    logger.warning("dhan-ws reconnect: %s", exc)
            if self._stop.is_set():
                break
            if hit_429:
                # Cool loop above owns the wait; don't add another long sleep.
                self._backoff = cool if hit_429 else backoff
                backoff = float(self._backoff)
                continue
            await asyncio.sleep(min(backoff, 15))
            backoff = min(backoff * 2, 15)
            self._backoff = backoff

    async def _flush_subscriptions(self, ws: Any) -> None:
        with self._lock:
            pending = [k for k in self._wanted if k not in self._subscribed]
        if not pending:
            return
        # Max 100 instruments per message
        code = REQ_SUB_QUOTE if self._prefer_quote else REQ_SUB_TICKER
        for i in range(0, len(pending), 100):
            chunk = pending[i : i + 100]
            body = {
                "RequestCode": code,
                "InstrumentCount": len(chunk),
                "InstrumentList": [
                    {"ExchangeSegment": seg, "SecurityId": sid} for seg, sid in chunk
                ],
            }
            await ws.send(json.dumps(body))
            with self._lock:
                self._subscribed.update(chunk)
            await asyncio.sleep(0.05)

    def _handle_binary(self, buf: bytes) -> None:
        try:
            code = buf[0] if buf else -1
            if code == RESP_TICKER:
                pkt = parse_ticker_payload(buf)
                self._upsert(pkt["segment"], pkt["security_id"], {"ltp": pkt["ltp"], "ltt": pkt["ltt"]})
            elif code == RESP_QUOTE or code == RESP_FULL:
                # Full packet starts like quote for the fields we care about
                if code == RESP_FULL and len(buf) >= 50:
                    # reuse quote layout for first OHLC fields (docs: ltp..low before depth)
                    pkt = parse_quote_payload(buf[:50] if len(buf) >= 50 else buf)
                else:
                    pkt = parse_quote_payload(buf)
                self._upsert(
                    pkt["segment"],
                    pkt["security_id"],
                    {
                        "ltp": pkt["ltp"],
                        "ltt": pkt["ltt"],
                        "open": pkt.get("open"),
                        "high": pkt.get("high"),
                        "low": pkt.get("low"),
                        "close": pkt.get("close"),
                        "volume": pkt.get("volume"),
                    },
                )
            elif code == RESP_PREV_CLOSE:
                pkt = parse_prev_close_payload(buf)
                self._upsert(
                    pkt["segment"],
                    pkt["security_id"],
                    {"prev_close": pkt["prev_close"]},
                )
            elif code == RESP_INDEX:
                # Index packet — treat first float after header as LTP when long enough
                if len(buf) >= 12:
                    _, _, segment, sid = parse_header(buf)
                    self._upsert(segment, sid, {"ltp": _f32(buf, 8)})
            elif code == RESP_DISCONNECT:
                self._last_error = "server disconnect packet"
                self._connected = False
            self._last_msg_at = time.time()
        except Exception as exc:
            logger.debug("dhan-ws parse skip: %s", exc)

    def _upsert(self, segment: str, security_id: str, fields: dict[str, Any]) -> None:
        key = (str(segment), str(security_id))
        with self._lock:
            row = dict(self._cache.get(key) or {})
            row.update({k: v for k, v in fields.items() if v is not None})
            row["updated_at"] = datetime.now(tz=IST).isoformat()
            self._cache[key] = row
