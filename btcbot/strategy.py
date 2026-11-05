"""Turn Kalshi markets + BTC price into UP/DOWN calls with entry, take-profit and stop prices."""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime

from .kalshi import num
from .model import fair_yes, taker_fee


def iso_to_ts(s: str | None) -> float:
    if not s:
        return 0.0
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


@dataclass
class Quote:
    ticker: str
    series: str
    event_ticker: str
    label: str
    strike_type: str
    floor: float | None
    cap: float | None
    close_ts: float
    yes_bid: float
    yes_ask: float
    yes_bid_size: float
    yes_ask_size: float

    # A NO ask is made of YES bids, and a NO bid of YES asks.
    @property
    def no_bid(self) -> float:
        return round(1.0 - self.yes_ask, 4) if 0 < self.yes_ask < 1 else 0.0

    @property
    def no_ask(self) -> float:
        return round(1.0 - self.yes_bid, 4) if 0 < self.yes_bid < 1 else 1.0

    def bid(self, side: str) -> float:
        return self.yes_bid if side == "yes" else self.no_bid

    def ask(self, side: str) -> float:
        return self.yes_ask if side == "yes" else self.no_ask

    def ask_size(self, side: str) -> float:
        return self.yes_ask_size if side == "yes" else self.yes_bid_size

    @property
    def mid(self) -> float:
        return (self.yes_bid + self.yes_ask) / 2.0

    @property
    def spread(self) -> float:
        return self.yes_ask - self.yes_bid


def parse_quote(m: dict, series: str) -> Quote | None:
    if m.get("status") not in (None, "active", "open"):
        return None
    st = (m.get("strike_type") or "").lower()
    floor, cap = num(m.get("floor_strike")), num(m.get("cap_strike"))
    if st.startswith("greater") and floor is None:
        return None
    if st.startswith("less") and cap is None:
        return None
    if st == "between" and (floor is None or cap is None):
        return None
    if not st:
        return None
    return Quote(
        ticker=m["ticker"],
        series=series,
        event_ticker=m.get("event_ticker", ""),
        label=m.get("yes_sub_title") or m.get("subtitle") or m["ticker"],
        strike_type=st,
        floor=floor,
        cap=cap,
        close_ts=iso_to_ts(m.get("close_time")),
        yes_bid=num(m.get("yes_bid_dollars"), 0.0),
        yes_ask=num(m.get("yes_ask_dollars"), 0.0),
        yes_bid_size=num(m.get("yes_bid_size_fp"), 0.0),
        yes_ask_size=num(m.get("yes_ask_size_fp"), 0.0),
    )


def direction(strike_type: str, side: str) -> str:
    if strike_type.startswith("greater"):
        return "UP" if side == "yes" else "DOWN"
    if strike_type.startswith("less"):
        return "DOWN" if side == "yes" else "UP"
    return "IN RANGE" if side == "yes" else "OUT OF RANGE"


# --- trade plan prices (whole cents: valid ticks on every Kalshi BTC market) -----------
def max_entry_price(p_cons: float, count: int, cfg) -> float:
    """Highest price that still clears MIN_EDGE after fees ("buy at or below")."""
    n = max(1, int(count))
    for c in range(99, 0, -1):
        p = c / 100
        if p_cons - p - taker_fee(p, n, cfg.taker_fee_rate) / n >= cfg.min_edge - 1e-9:
            return p
    return 0.0


def take_profit_price(fair_side: float, count: int, cfg) -> float | None:
    """Lowest price where selling beats the bot's fair value after the sell fee.

    That's the point where Kalshi has caught up with the bot, so the edge is used up.
    None when fair value is so close to $1 that holding to settlement is the plan.
    """
    n = max(1, int(count))
    for c in range(max(1, math.ceil(round((fair_side + 0.01) * 100, 6))), 100):
        p = c / 100
        if p - taker_fee(p, n, cfg.taker_fee_rate) / n > fair_side + 0.01 - 1e-9:
            return p
    return None


def take_profit_for(direction_label: str, entry: float, fair_side: float, count: int, cfg) -> float | None:
    """Fixed take-profit for a new position.

    TP_UP_PCT / TP_DOWN_PCT (defaults 0.60 / 0.40): sell when the price is that far above
    what you paid, e.g. UP bought at $0.30 -> sell at $0.48. Rounded up to the cent. If
    the target is $1 or more, there is nothing to take: hold to settlement.
    A percentage of 0 (and range markets) use the fair-value target instead.
    """
    pct = {"UP": cfg.tp_up_pct, "DOWN": cfg.tp_down_pct}.get(direction_label, 0.0)
    if pct > 0:
        tp = math.ceil(round(entry * (1.0 + pct) * 100, 6)) / 100
        return tp if tp < 1.0 else None
    return take_profit_price(fair_side, count, cfg)


def tp_rule(direction_label: str, cfg) -> str:
    pct = {"UP": cfg.tp_up_pct, "DOWN": cfg.tp_down_pct}.get(direction_label, 0.0)
    return f"+{pct:.0%} rule" if pct > 0 else "fair value"


def stop_price(entry: float, cfg) -> float | None:
    if cfg.stop_loss_pct <= 0:
        return None
    return max(0.01, math.floor(round(entry * (1.0 - cfg.stop_loss_pct) * 100, 6)) / 100)


@dataclass
class Signal:
    quote: Quote
    side: str          # "yes" or "no"
    price: float       # ask we'd pay for that side right now
    p_win: float       # blended fair probability this side wins
    p_cons: float      # worst case across vol scenarios
    edge: float        # p_cons - price - fee per contract
    t_rem: float
    fair_yes: float
    max_entry: float = 0.0
    take_profit: float | None = None
    stop: float | None = None
    tp_note: str = ""

    @property
    def direction(self) -> str:
        return direction(self.quote.strike_type, self.side)

    def plan(self, count: int, cfg) -> "Signal":
        """Fill in entry / take-profit / stop for a `count`-contract trade."""
        self.edge = self.p_cons - self.price - taker_fee(self.price, count, cfg.taker_fee_rate) / count
        self.max_entry = max(max_entry_price(self.p_cons, count, cfg), self.price)
        self.take_profit = take_profit_for(self.direction, self.price, self.p_win, count, cfg)
        self.tp_note = tp_rule(self.direction, cfg)
        self.stop = stop_price(self.price, cfg)
        return self

    def text(self, count: int | None = None) -> str:
        q = self.quote
        size = f"{count}x " if count else ""
        tp = (f"sell at ${self.take_profit:.2f} ({self.tp_note})" if self.take_profit
              else "none, hold to settlement (target would be $1+)")
        stop = f" | stop: ${self.stop:.2f}" if self.stop else ""
        return (f"BUY {self.direction} ({self.side.upper()}) {size}on {q.ticker} [{q.label}] | "
                f"entry: buy at or below ${self.max_entry:.2f} (ask now ${self.price:.3f}) | "
                f"take profit: {tp}{stop} | bot fair ${self.p_win:.3f}, edge +{self.edge * 100:.1f}c after fees | "
                f"closes in {max(self.t_rem, 0) / 60:.0f} min")


def evaluate(q: Quote, spot: float, sigma: float, now: float, cfg,
             realized_avg: float | None = None) -> tuple[list[Signal], tuple[float, float, float] | None]:
    """Return (signals that pass every filter, fair-value triple or None)."""
    t_rem = q.close_ts - now
    fy = fair_yes(q.strike_type, q.floor, q.cap, spot, t_rem, sigma,
                  realized_avg=realized_avg, basis_usd=cfg.spot_basis_usd,
                  mults=cfg.vol_mults, weights=cfg.vol_weights)
    if fy is None:
        return [], None
    if not (cfg.min_seconds_to_close <= t_rem <= cfg.max_seconds_to_close):
        return [], fy
    if not (0 < q.yes_bid < q.yes_ask < 1) or q.spread > cfg.max_spread + 1e-9:
        return [], fy
    mix, lo, hi = fy
    out = []
    for side in ("yes", "no"):
        price = q.ask(side)
        if not (cfg.min_price <= price <= cfg.max_price) or q.ask_size(side) < 1:
            continue
        p_win = mix if side == "yes" else 1.0 - mix
        p_cons = lo if side == "yes" else 1.0 - hi
        sig = Signal(q, side, price, p_win, p_cons, 0.0, t_rem, mix)
        sig.plan(max(1, int(cfg.max_trade_dollars // price)), cfg)
        if sig.edge >= cfg.min_edge:
            out.append(sig)
    return out, fy


def exit_decision(entry_price: float, bid: float, fair_side: float, t_rem: float, count: float, cfg,
                  take_profit: float | None = None, stop: float | None = None) -> str | None:
    """Should an open position be sold now at `bid`?

    EXIT_STYLE=target (default): sell when the bid reaches the take-profit price set
        at entry - the same price the alert told you to rest a limit sell at.
    EXIT_STYLE=dynamic: sell whenever the bid (after the sell fee) pays more than the
        bot's *current* fair value. Needs the bot watching; you can't copy it by hand.
    Stop loss (both styles): sell if the bid falls to the stop price (STOP_LOSS_PCT
        below entry). On a binary contract this often sells near the bottom.
        Kalshi has no stop orders, so by hand you have to watch for it.
    """
    if bid <= 0 or count <= 0:
        return None
    if cfg.exit_style == "target":
        if take_profit is not None and bid >= take_profit - 1e-9:
            return "take_profit"
    elif cfg.edge_exit and t_rem >= 20:
        proceeds = bid - taker_fee(bid, count, cfg.taker_fee_rate) / count
        if proceeds > fair_side + 0.01:
            return "take_profit" if proceeds > entry_price else "model_exit"
    if t_rem < 20:  # last seconds: the settlement average is mostly locked in, don't dump
        return None
    if stop is None and cfg.stop_loss_pct > 0:
        stop = entry_price * (1.0 - cfg.stop_loss_pct)
    if stop is not None and bid <= stop + 1e-9:
        return "stop_loss"
    return None
