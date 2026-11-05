"""Order execution: PaperBroker (simulated against the real order book) and
LiveBroker (real orders on Kalshi's demo or live exchange).

Both take liquidity with limit prices, so they never pay more than the price
the signal was computed at, and they never leave resting orders behind.
"""
from __future__ import annotations

import json
import math

from .kalshi import num, parse_book
from .model import taker_fee
from .strategy import stop_price


def take(levels: list[tuple[float, float]], want: float, limit: float, buying: bool) -> tuple[float, float]:
    """Walk best-first levels up to `limit`. Returns (filled, average price)."""
    got = cost = 0.0
    for p, q in levels:
        if (buying and p > limit + 1e-9) or (not buying and p < limit - 1e-9):
            break
        k = min(q, want - got)
        if k <= 0:
            break
        got += k
        cost += k * p
    return got, (cost / got if got else 0.0)


def _fill_whole(levels, want: int, limit: float, buying: bool) -> tuple[int, float]:
    got, avg = take(levels, want, limit, buying)
    n = math.floor(got + 1e-9)
    if 0 < n < got:
        _, avg = take(levels, n, limit, buying)
    return n, avg


class PaperBroker:
    """Fake money, real prices: fills against Kalshi's live order book."""

    def __init__(self, client, journal, cfg):
        self.client, self.journal, self.cfg = client, journal, cfg
        self.mode = "paper"

    def cash(self) -> float:
        return self.cfg.bankroll + self.journal.realized_pnl(self.mode) - self.journal.open_cost(self.mode)

    def equity(self) -> float:
        return self.cfg.bankroll + self.journal.realized_pnl(self.mode)

    def buy(self, sig, n: int, now: float) -> int | None:
        book = parse_book(self.client.orderbook(sig.quote.ticker))
        asks = book["yes_asks"] if sig.side == "yes" else book["no_asks"]
        filled, avg = _fill_whole(asks, n, sig.price, buying=True)
        if filled < 1:
            return None
        return self.journal.open_position(
            mode=self.mode, ticker=sig.quote.ticker, series=sig.quote.series,
            event_ticker=sig.quote.event_ticker, side=sig.side, count=filled, entry_price=avg,
            entry_fee=taker_fee(avg, filled, self.cfg.taker_fee_rate), close_ts=sig.quote.close_ts,
            p_win=sig.p_win, edge=sig.edge, label=sig.quote.label, ts=now,
            take_profit=sig.take_profit, stop_price=stop_price(avg, self.cfg))

    def sell(self, pos, min_price: float, reason: str, now: float, at_limit: bool = False) -> float | None:
        """at_limit: record the fill at min_price, like a resting take-profit order would get."""
        book = parse_book(self.client.orderbook(pos.ticker))
        bids = book["yes_bids"] if pos.side == "yes" else book["no_bids"]
        filled, avg = _fill_whole(bids, int(pos.count), min_price, buying=False)
        if filled < 1:
            return None
        if at_limit:
            avg = min_price
        fee = taker_fee(avg, filled, self.cfg.taker_fee_rate)
        return self.journal.close_position(pos.id, exit_price=avg, exit_fee=fee, reason=reason,
                                           ts=now, count=filled)


def _fill_count(order: dict) -> float | None:
    for key in ("fill_count_fp", "fill_count", "taker_fill_count_fp", "taker_fill_count"):
        v = num(order.get(key))
        if v is not None:
            return v
    return None


class LiveBroker:
    """Real orders (immediate-or-cancel limit orders). mode = 'demo' or 'live'."""

    def __init__(self, client, journal, cfg, mode: str):
        if not client.authenticated:
            raise SystemExit(f"{mode} mode needs KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH in .env")
        self.client, self.journal, self.cfg, self.mode = client, journal, cfg, mode

    def cash(self) -> float:
        return self.client.balance()

    def equity(self) -> float:
        return self.client.balance() + self.journal.open_cost(self.mode)

    def _order(self, ticker: str, action: str, side: str, n: int, price: float) -> int:
        before = self.client.position_fp(ticker)
        try:
            order = self.client.create_order(ticker, action, side, n, price)
        except Exception as e:
            # The order may still have reached the exchange (e.g. a timeout). Never guess:
            # read the position back so the journal matches the account.
            self.journal.log("order_error", f"{ticker} {action} {side} {n} @ {price}: {e}")
            filled = abs(self.client.position_fp(ticker) - before)
            if filled < 1:
                raise
            return math.floor(filled + 1e-9)
        self.journal.log("order", json.dumps({"ticker": ticker, "action": action, "side": side,
                                              "count": n, "price": price, "response": order})[:4000])
        filled = _fill_count(order)
        if filled is None:  # response shape unknown: trust the position change
            filled = abs(self.client.position_fp(ticker) - before)
        return math.floor(filled + 1e-9)

    def buy(self, sig, n: int, now: float) -> int | None:
        filled = self._order(sig.quote.ticker, "buy", sig.side, n, sig.price)
        if filled < 1:
            return None
        # Recorded at the limit price: the real fill is the same or better.
        return self.journal.open_position(
            mode=self.mode, ticker=sig.quote.ticker, series=sig.quote.series,
            event_ticker=sig.quote.event_ticker, side=sig.side, count=filled, entry_price=sig.price,
            entry_fee=taker_fee(sig.price, filled, self.cfg.taker_fee_rate), close_ts=sig.quote.close_ts,
            p_win=sig.p_win, edge=sig.edge, label=sig.quote.label, ts=now,
            take_profit=sig.take_profit, stop_price=stop_price(sig.price, self.cfg))

    def sell(self, pos, min_price: float, reason: str, now: float, at_limit: bool = False) -> float | None:
        filled = self._order(pos.ticker, "sell", pos.side, int(pos.count), min_price)
        if filled < 1:
            return None
        fee = taker_fee(min_price, filled, self.cfg.taker_fee_rate)
        return self.journal.close_position(pos.id, exit_price=min_price, exit_fee=fee, reason=reason,
                                           ts=now, count=filled)
