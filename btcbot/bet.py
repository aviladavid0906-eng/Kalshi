"""One-window paper bets on the 15-minute BTC market ("v2 rule").

What went wrong with the first live $100 bets (Nov 5): entries in the first
seconds of a window on 2-4c "edges" that were really our BTC feed being a few
seconds out of step with Kalshi's. The v2 rule, tuned on Nov 2-5 and checked on
Oct 30 - Nov 2 (and the other way round):

  * skip the first BET_SKIP_FIRST_SECONDS (5 min) of the window
  * bet only when one side's expected profit is >= BET_MIN_EDGE (4c) per
    contract after fees at the full bet size
  * only sides priced BET_MIN_PRICE..BET_MAX_PRICE (35-90c): no long shots
  * no new bet in the last 2 minutes; hold to settlement

Backtest, $100 per bet: +3.3c per $1 (158 bets) and +7.8c (62 bets), +4.6c over
all 220 - but t = 0.7, so it could still be luck. Paper only, by design.
"""
from __future__ import annotations

import time
from datetime import datetime
from zoneinfo import ZoneInfo

from .kalshi import Kalshi, parse_book
from .model import annualized, ewma_sigma, fair_yes, taker_fee
from .spot import SpotFeed, coinbase_candles
from .strategy import parse_quote

WINDOW = 900
LAST_ENTRY_SECONDS = 120
ET = ZoneInfo("America/New_York")  # Kalshi names 15-min markets by their close time in New York


def ticker_for(close_ts: int, series: str = "KXBTC15M") -> str:
    """Market ticker for the window closing at close_ts, e.g. KXBTC15M-26NOV091500-00."""
    d = datetime.fromtimestamp(close_ts, ET)
    return f"{series}-{d:%y%b%d%H%M}".upper() + f"-{d:%M}"


def find_window(client, close_ts: int, series: str = "KXBTC15M"):
    """The window's market: one direct lookup (kind to the rate limit), else search open events."""
    try:
        q = parse_quote(client.market(ticker_for(close_ts, series)), series)
        if q and int(q.close_ts) == close_ts:
            return q
    except Exception:
        pass
    for e in client.events(series, status="open"):
        for m in client.markets(event_ticker=e["event_ticker"], status="open"):
            q = parse_quote(m, series)
            if q and int(q.close_ts) == close_ts:
                return q
    return None


def walk_cost(levels, n: int):
    """(total cost incl. fee, average price) to buy n contracts off best-first asks, or None."""
    got, cost = 0.0, 0.0
    for p, q in levels:
        k = min(q, n - got)
        if k <= 0:
            break
        got += k
        cost += k * p
    if got < n:
        return None
    avg = cost / n
    return cost + taker_fee(avg, n), avg


def max_contracts(levels, budget: float) -> int:
    if not levels:
        return 0
    lo, hi = 0, int(budget / levels[0][0]) + 1
    while lo < hi:
        mid = (lo + hi + 1) // 2
        r = walk_cost(levels, mid)
        if r and r[0] <= budget:
            lo = mid
        else:
            hi = mid - 1
    return lo


def best_bet(book: dict, p_up: float, amount: float, cfg) -> dict | None:
    """The side worth betting `amount` on right now, or None if neither clears the bar."""
    cands = []
    for side in ("yes", "no"):
        asks = book["yes_asks"] if side == "yes" else book["no_asks"]
        if not asks or not (cfg.bet_min_price <= asks[0][0] <= cfg.bet_max_price):
            continue
        levels = [(p, q) for p, q in asks if p <= asks[0][0] + 0.02]  # at most 2c of slippage
        n = max_contracts(levels, amount)
        if n < 1:
            continue
        total, avg = walk_cost(levels, n)
        p = p_up if side == "yes" else 1.0 - p_up
        cands.append({"ev": p - total / n, "side": side, "n": n, "avg": avg, "cost": total, "p": p})
    if not cands:
        return None
    best = max(cands, key=lambda c: c["ev"])
    return best if best["ev"] >= cfg.bet_min_edge else None


def in_entry_window(t_rem: float, cfg) -> bool:
    return LAST_ENTRY_SECONDS <= t_rem <= WINDOW - cfg.bet_skip_first_seconds


def next_close(now: float) -> int:
    """Close time of the window still open for entries (the current one, else the next)."""
    close = (int(now) // WINDOW + 1) * WINDOW
    return close if close - now > LAST_ENTRY_SECONDS else close + WINDOW


def run_window(cfg, amount: float, journal, *, close_ts: int | None = None, client=None, spot=None,
               printer=print, poll: float = 5.0) -> dict:
    client = client or Kalshi(min_interval=0.3)
    spot = spot or SpotFeed()
    close_ts = close_ts or next_close(time.time())
    say = lambda m: printer(f"[{datetime.now():%H:%M:%S}] {m}")  # noqa: E731
    say(f"Paper ${amount:.0f} bet, 15-min window closing {datetime.fromtimestamp(close_ts):%H:%M}. "
        f"Rule: skip first {cfg.bet_skip_first_seconds // 60} min, need a {cfg.bet_min_edge * 100:.0f}c+ edge "
        f"on a side priced {cfg.bet_min_price * 100:.0f}-{cfg.bet_max_price * 100:.0f}c, hold to the close.")
    q, sigma, vol_ts, pos, last_print = None, None, 0.0, None, 0.0
    while True:
        now = time.time()
        if now < close_ts - WINDOW:
            time.sleep(min(5.0, close_ts - WINDOW - now + 0.5))
            continue
        if q is None and close_ts - now < LAST_ENTRY_SECONDS:
            say("MISSED: couldn't reach this window's market before entries closed. Nothing risked.")
            return {"bet": None, "missed": True}
        try:
            S = spot.price(now)
            if sigma is None or now - vol_ts > 60:
                sigma = ewma_sigma([c[4] for c in coinbase_candles(now - 180 * 60, now, session=spot.s)],
                                   halflife=cfg.vol_halflife_min)
                vol_ts = now
            if q is None:
                q = find_window(client, close_ts)
                if q is None:
                    time.sleep(3)
                    continue
                say(f"Window open. Target (average of the minute before the open): ${q.floor:,.2f}")
            t_rem = q.close_ts - now
            if t_rem <= 0:
                break
            book = parse_book(client.orderbook(q.ticker, depth=40))
        except Exception as e:  # network hiccups: keep going
            say(f"error: {e}")
            time.sleep(4)
            continue
        realized = spot.average_since(q.close_ts - 60) if t_rem < 60 else None
        p_up = fair_yes(q.strike_type, q.floor, q.cap, S, t_rem, sigma, realized_avg=realized,
                        basis_usd=cfg.spot_basis_usd, mults=cfg.vol_mults, weights=cfg.vol_weights)[0]
        if pos is None and in_entry_window(t_rem, cfg):
            b = best_bet(book, p_up, amount, cfg)
            if b:
                d = "UP" if b["side"] == "yes" else "DOWN"
                pid = journal.open_position(
                    mode="paper", ticker=q.ticker, series=q.series, event_ticker=q.event_ticker, side=b["side"],
                    count=b["n"], entry_price=b["avg"], entry_fee=b["cost"] - b["n"] * b["avg"],
                    close_ts=q.close_ts, p_win=b["p"], edge=b["ev"], label="bet v2", ts=now)
                pos = {**b, "id": pid, "dir": d}
                say(f"BET {d}: {b['n']} contracts @ ${b['avg']:.3f} = ${b['cost']:.2f} incl. fees | bot {b['p']:.0%} "
                    f"vs price {b['avg']:.0%}, edge +{b['ev'] * 100:.1f}c | win +${b['n'] - b['cost']:.2f}, "
                    f"loss -${b['cost']:.2f}")
        if pos is None and t_rem < LAST_ENTRY_SECONDS:
            say("NO TRADE: no side reached the edge bar in the entry window. Nothing risked.")
            return {"bet": None}
        if now - last_print >= 60:
            last_print = now
            yb = book["yes_bids"][0][0] if book["yes_bids"] else 0.0
            ya = book["yes_asks"][0][0] if book["yes_asks"] else 1.0
            line = (f"{t_rem / 60:4.1f} min left | BTC ${S:,.2f} vs target ${q.floor:,.2f} ({S - q.floor:+,.0f}) | "
                    f"Kalshi UP {yb:.2f}/{ya:.2f} | bot UP {p_up:.2f} | vol {annualized(sigma) * 100:.0f}%")
            if pos:
                bid = yb if pos["side"] == "yes" else (round(1 - ya, 4) if ya < 1 else 0.0)
                line += f" | your {pos['n']} {pos['dir']} worth ~${pos['n'] * bid:.2f}"
            say(line)
        time.sleep(poll if t_rem > 70 else 2)
    if pos is None:
        return {"bet": None}
    for _ in range(60):
        try:
            m = client.market(q.ticker)
            if (m.get("result") or "") in ("yes", "no"):
                pnl = journal.settle(pos["id"], m["result"])
                won = pnl > 0
                say(f"RESULT: settled ${float(m.get('expiration_value') or 0):,.2f} vs target ${q.floor:,.2f} -> "
                    f"{'UP' if m['result'] == 'yes' else 'DOWN'}. Your {pos['dir']} {'WON' if won else 'LOST'}: "
                    f"{pnl:+.2f} ({abs(pnl) / cfg.hourly_wage:.1f} h of work)")
                return {"bet": pos, "result": m["result"], "pnl": pnl}
        except Exception:
            pass
        time.sleep(5)
    say("Settlement not published yet; `python -m btcbot run` or `report` will pick it up later.")
    return {"bet": pos, "result": None}
