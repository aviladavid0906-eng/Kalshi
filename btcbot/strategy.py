"""Turn Kalshi markets + BTC price into UP/DOWN calls, and decide exits."""
from __future__ import annotations

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


@dataclass
class Signal:
    quote: Quote
    side: str          # "yes" or "no"
    price: float       # ask we'd pay for that side
    p_win: float       # blended fair probability this side wins
    p_cons: float      # worst case across vol scenarios
    edge: float        # p_cons - price - fee per contract
    t_rem: float
    fair_yes: float

    @property
    def direction(self) -> str:
        return direction(self.quote.strike_type, self.side)

    def text(self, count: int | None = None) -> str:
        q = self.quote
        mins = max(self.t_rem, 0) / 60.0
        size = f"{count}x " if count else ""
        return (f"BUY {self.direction} ({self.side.upper()}) {size}@ ${self.price:.3f} on {q.ticker} [{q.label}] "
                f"- bot fair {self.p_win:.3f} (worst case {self.p_cons:.3f}), "
                f"edge +{self.edge * 100:.1f}c/contract after fees, closes in {mins:.0f} min")


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
        n_ref = max(1, int(cfg.max_trade_dollars // price))
        edge = p_cons - price - taker_fee(price, n_ref, cfg.taker_fee_rate) / n_ref
        if edge >= cfg.min_edge:
            out.append(Signal(q, side, price, p_win, p_cons, edge, t_rem, mix))
    return out, fy


def exit_decision(entry_price: float, bid: float, fair_side: float, t_rem: float,
                  count: float, cfg) -> str | None:
    """Should an open position be sold now at `bid`?

    edge exit  : the bid (after the sell fee) pays more than the bot thinks the
                 contract is worth. Locks in profit, or cuts a bet the model
                 no longer likes. Always positive expected value.
    stop loss  : the bid fell STOP_LOSS_PCT below entry. Caps the loss, but on
                 a binary contract it often sells near the bottom. Set
                 STOP_LOSS_PCT=0 to hold to settlement instead.
    """
    if bid <= 0 or t_rem < 20 or count <= 0:
        return None
    proceeds = bid - taker_fee(bid, count, cfg.taker_fee_rate) / count
    if cfg.edge_exit and proceeds > fair_side + 0.01:
        return "take_profit" if proceeds > entry_price else "model_exit"
    if cfg.stop_loss_pct > 0 and bid <= entry_price * (1.0 - cfg.stop_loss_pct):
        return "stop_loss"
    return None
