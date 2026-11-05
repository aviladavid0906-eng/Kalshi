"""Live BTC price and 1-minute history from public exchange APIs.

Kalshi settles on CF Benchmarks' BRTI, a cross-exchange index that isn't
free in real time. Averaging Coinbase and Kraken tracks it closely; the
leftover gap is modelled as SPOT_BASIS_USD of extra uncertainty.
"""
from __future__ import annotations

import statistics
import time
from collections import deque
from datetime import datetime, timezone

import requests

COINBASE = "https://api.exchange.coinbase.com"
KRAKEN = "https://api.kraken.com/0/public"


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class SpotFeed:
    def __init__(self, session: requests.Session | None = None, timeout: float = 8.0, keep: int = 5000):
        self.s = session or requests.Session()
        self.timeout = timeout
        self.history: deque[tuple[float, float]] = deque(maxlen=keep)

    def coinbase(self) -> float:
        d = self.s.get(f"{COINBASE}/products/BTC-USD/ticker", timeout=self.timeout).json()
        return (float(d["bid"]) + float(d["ask"])) / 2

    def kraken(self) -> float:
        d = self.s.get(f"{KRAKEN}/Ticker", params={"pair": "XBTUSD"}, timeout=self.timeout).json()
        t = next(iter(d["result"].values()))
        return (float(t["a"][0]) + float(t["b"][0])) / 2

    def price(self, now: float | None = None) -> float:
        quotes = []
        for source in (self.coinbase, self.kraken):
            try:
                quotes.append(source())
            except Exception:
                pass
        if not quotes:
            raise RuntimeError("No BTC price: Coinbase and Kraken both unreachable")
        p = statistics.fmean(quotes)
        self.history.append((now if now is not None else time.time(), p))
        return p

    def average_since(self, start_ts: float) -> float | None:
        xs = [p for t, p in self.history if t >= start_ts]
        return statistics.fmean(xs) if xs else None


def coinbase_candles(start_ts: float, end_ts: float, session: requests.Session | None = None,
                     granularity: int = 60) -> list[tuple[int, float, float, float, float]]:
    """Candles as (start_ts, low, high, open, close), oldest first."""
    s = session or requests.Session()
    out: dict[int, tuple] = {}
    t = int(start_ts) // granularity * granularity
    end = int(end_ts)
    while t <= end:
        chunk_end = min(t + granularity * 299, end)
        params = {"granularity": granularity, "start": _iso(t), "end": _iso(chunk_end)}
        r = None
        for attempt in range(6):
            try:
                r = s.get(f"{COINBASE}/products/BTC-USD/candles", params=params, timeout=15)
            except requests.RequestException:
                time.sleep(1 + attempt)
                continue
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(1 + attempt)
                continue
            break
        if r is None:
            raise RuntimeError("Coinbase candles unreachable")
        r.raise_for_status()
        for row in r.json():
            ts = int(row[0])
            if int(start_ts) - granularity <= ts <= end:
                out[ts] = (ts, float(row[1]), float(row[2]), float(row[3]), float(row[4]))
        t = chunk_end + granularity
        time.sleep(0.12)
    return [out[k] for k in sorted(out)]
