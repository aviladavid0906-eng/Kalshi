"""Small Kalshi Trade API v2 client.

Public market data needs no account. Portfolio calls (balance, positions,
orders) are signed with your API key: RSA-PSS/SHA-256 over
``timestamp_ms + METHOD + path`` (path includes /trade-api/v2, no query).

Orders use the v2 endpoint (POST /portfolio/events/orders). The old
/portfolio/orders endpoint now answers HTTP 410.
"""
from __future__ import annotations

import base64
import time
import uuid
from typing import Any, Iterator
from urllib.parse import urlparse

import requests

from .config import PROD_URL


class KalshiError(RuntimeError):
    pass


def num(x: Any, default: float | None = None) -> float | None:
    """Parse Kalshi's string numbers ("0.4200", "5824.01") safely."""
    if x is None or x == "":
        return default
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def load_private_key(path: str):
    from cryptography.hazmat.primitives import serialization

    with open(path, "rb") as f:
        return serialization.load_pem_private_key(f.read(), password=None)


def build_order_payload(
    ticker: str,
    action: str,
    side: str,
    count: int,
    price: float,
    *,
    time_in_force: str = "immediate_or_cancel",
    client_order_id: str | None = None,
) -> dict:
    """Translate (buy|sell, yes|no, price of that side) into a v2 order.

    v2 orders quote the YES leg of a single book:
        buy YES @ p  -> bid @ p          sell YES @ p -> ask @ p
        buy NO  @ p  -> ask @ (1 - p)    sell NO  @ p -> bid @ (1 - p)
    """
    action, side = action.lower(), side.lower()
    if action not in ("buy", "sell") or side not in ("yes", "no"):
        raise ValueError(f"bad action/side: {action}/{side}")
    if not 0 < price < 1:
        raise ValueError(f"price must be between $0 and $1, got {price}")
    if count <= 0:
        raise ValueError("count must be positive")
    yes_leg = price if side == "yes" else 1.0 - price
    is_bid = (action == "buy") == (side == "yes")
    return {
        "ticker": ticker,
        "side": "bid" if is_bid else "ask",
        "count": f"{float(count):.2f}",
        "price": f"{yes_leg:.4f}",
        "time_in_force": time_in_force,
        "self_trade_prevention_type": "taker_at_cross",
        "client_order_id": client_order_id or str(uuid.uuid4()),
    }


def parse_book(ob: dict) -> dict:
    """Orderbook -> best-first ladders for all four sides.

    Kalshi returns only bids: YES bids and NO bids. A NO bid at p is a YES
    ask at 1-p, and vice versa.
    """
    book = ob.get("orderbook_fp") or ob.get("orderbook") or {}

    def ladder(new_key: str, old_key: str) -> list[tuple[float, float]]:
        rows = book.get(new_key)
        if rows is None and book.get(old_key) is not None:  # legacy cents format
            return [(float(p) / 100.0, float(q)) for p, q in book[old_key]]
        return [(float(p), float(q)) for p, q in rows or []]

    yes_bids = sorted(ladder("yes_dollars", "yes"), key=lambda x: -x[0])
    no_bids = sorted(ladder("no_dollars", "no"), key=lambda x: -x[0])
    return {
        "yes_bids": yes_bids,
        "no_bids": no_bids,
        "yes_asks": [(round(1 - p, 4), q) for p, q in no_bids],
        "no_asks": [(round(1 - p, 4), q) for p, q in yes_bids],
    }


class Kalshi:
    def __init__(
        self,
        base_url: str = PROD_URL,
        key_id: str = "",
        private_key_path: str = "",
        *,
        session: requests.Session | None = None,
        min_interval: float = 0.15,
        timeout: float = 15.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.key_id = key_id
        self.s = session or requests.Session()
        self.s.headers.update({"Accept": "application/json", "User-Agent": "btcbot/0.1"})
        self.min_interval = min_interval
        self.timeout = timeout
        self._last = 0.0
        self._key = load_private_key(private_key_path) if key_id and private_key_path else None

    # --- plumbing -----------------------------------------------------------
    @property
    def authenticated(self) -> bool:
        return self._key is not None and bool(self.key_id)

    def sign(self, ts_ms: str, method: str, path: str) -> str:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding

        if self._key is None:
            raise KalshiError("no private key loaded")
        sig = self._key.sign(
            f"{ts_ms}{method.upper()}{path}".encode(),
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
        return base64.b64encode(sig).decode()

    def _throttle(self) -> None:
        wait = self._last + self.min_interval - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()

    @staticmethod
    def _backoff(attempt: int, retry_after: str | None = None) -> None:
        wait = num(retry_after)
        time.sleep(wait if wait and wait > 0 else min(0.5 * 2**attempt, 30))

    def request(self, method: str, endpoint: str, *, params=None, body=None, auth=False, retries=8) -> dict:
        url = self.base_url + endpoint
        path = urlparse(url).path
        last: Exception | None = None
        for attempt in range(retries):
            self._throttle()
            headers = {}
            if auth:
                if not self.authenticated:
                    raise KalshiError("This needs KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH in .env")
                ts = str(int(time.time() * 1000))
                headers = {
                    "KALSHI-ACCESS-KEY": self.key_id,
                    "KALSHI-ACCESS-TIMESTAMP": ts,
                    "KALSHI-ACCESS-SIGNATURE": self.sign(ts, method, path),
                }
            try:
                r = self.s.request(method, url, params=params, json=body, headers=headers, timeout=self.timeout)
            except requests.RequestException as e:
                last = e
                if attempt < retries - 1:
                    self._backoff(attempt)
                continue
            if r.status_code == 429 or r.status_code >= 500:
                last = KalshiError(f"HTTP {r.status_code}: {r.text[:200]}")
                if attempt < retries - 1:
                    self._backoff(attempt, r.headers.get("Retry-After"))
                continue
            if r.status_code >= 400:
                raise KalshiError(f"{method} {endpoint} -> HTTP {r.status_code}: {r.text[:300]}")
            return r.json() if r.content.strip() else {}
        raise KalshiError(f"{method} {endpoint} failed after {retries} tries: {last}")

    def _paged(self, endpoint: str, key: str, params: dict, max_pages: int) -> Iterator[dict]:
        params = {k: v for k, v in params.items() if v is not None}
        cursor = None
        for _ in range(max_pages):
            if cursor:
                params["cursor"] = cursor
            d = self.request("GET", endpoint, params=params)
            yield from d.get(key) or []
            cursor = d.get("cursor")
            if not cursor:
                break

    # --- public market data -------------------------------------------------
    def series(self, series_ticker: str) -> dict:
        return self.request("GET", f"/series/{series_ticker}").get("series", {})

    def events(self, series_ticker: str, status: str = "open", max_pages: int = 5) -> list[dict]:
        return list(self._paged("/events", "events",
                                {"series_ticker": series_ticker, "status": status, "limit": 200}, max_pages))

    def markets(self, *, event_ticker=None, series_ticker=None, status=None,
                min_close_ts=None, max_close_ts=None, max_pages: int = 20) -> list[dict]:
        params = {"event_ticker": event_ticker, "series_ticker": series_ticker, "status": status,
                  "min_close_ts": min_close_ts, "max_close_ts": max_close_ts, "limit": 1000}
        return list(self._paged("/markets", "markets", params, max_pages))

    def market(self, ticker: str) -> dict:
        return self.request("GET", f"/markets/{ticker}").get("market", {})

    def orderbook(self, ticker: str, depth: int = 20) -> dict:
        return self.request("GET", f"/markets/{ticker}/orderbook", params={"depth": depth})

    def candlesticks(self, series_ticker: str, ticker: str, start_ts: int, end_ts: int, interval: int = 1) -> list[dict]:
        d = self.request("GET", f"/series/{series_ticker}/markets/{ticker}/candlesticks",
                         params={"start_ts": int(start_ts), "end_ts": int(end_ts), "period_interval": interval})
        return d.get("candlesticks") or []

    def candlesticks_batch(self, tickers: list[str], start_ts: int, end_ts: int, interval: int = 1) -> dict[str, list[dict]]:
        """Candlesticks for several markets in one call (much kinder to the rate limit)."""
        d = self.request("GET", "/markets/candlesticks",
                         params={"market_tickers": ",".join(tickers), "start_ts": int(start_ts),
                                 "end_ts": int(end_ts), "period_interval": interval})
        return {m.get("market_ticker"): m.get("candlesticks") or [] for m in d.get("markets") or []}

    # --- portfolio (signed) -----------------------------------------------------
    def balance(self) -> float:
        d = self.request("GET", "/portfolio/balance", auth=True)
        if d.get("balance_dollars") is not None:
            return num(d["balance_dollars"], 0.0)
        return num(d.get("balance"), 0.0) / 100.0  # cents

    def positions(self, ticker: str | None = None) -> list[dict]:
        params = {"limit": 200}
        if ticker:
            params["ticker"] = ticker
        return self.request("GET", "/portfolio/positions", params=params, auth=True).get("market_positions") or []

    def position_fp(self, ticker: str) -> float:
        """Signed contract count: + = YES contracts, - = NO contracts."""
        for p in self.positions(ticker):
            if p.get("ticker") == ticker:
                return num(p.get("position_fp"), num(p.get("position"), 0.0))
        return 0.0

    def create_order(self, ticker: str, action: str, side: str, count: int, price: float, *,
                     time_in_force: str = "immediate_or_cancel") -> dict:
        body = build_order_payload(ticker, action, side, count, price, time_in_force=time_in_force)
        # retries=1: never resend an order blindly after a timeout.
        d = self.request("POST", "/portfolio/events/orders", body=body, auth=True, retries=1)
        return d.get("order", d) if isinstance(d, dict) else {}

    def cancel_order(self, order_id: str) -> dict:
        return self.request("DELETE", f"/portfolio/events/orders/{order_id}", auth=True, retries=1)
