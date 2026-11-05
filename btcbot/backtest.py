"""Replay recently settled Kalshi BTC markets through the bot's exact rules.

For every minute of each market's life it rebuilds what the bot would have
seen (Kalshi's bid/ask from 1-minute candlesticks, BTC from Coinbase 1-minute
candles, volatility from the minutes before), applies the same entry, sizing
and exit logic, and settles on Kalshi's real result.

Limits, stated plainly:
  * Fills assume you got the quoted ask/bid for your size. Small sizes only.
  * Coinbase is a stand-in for CF Benchmarks' BRTI (the gap is measured below).
  * Minute snapshots miss what happens inside the minute, and real bots
    compete with faster traders for the same mispricings. Treat a positive
    backtest as "maybe", and a negative one as "almost certainly no".
"""
from __future__ import annotations

import math
import statistics
import time
from dataclasses import dataclass, replace

from .config import PROD_URL
from .kalshi import Kalshi, num
from .model import clamp_sigma, taker_fee
from .report import Stats, brier, money, trade_stats, verdict
from .risk import size_for
from .spot import coinbase_candles
from .strategy import evaluate, exit_decision, iso_to_ts, parse_quote


@dataclass
class SimTrade:
    ticker: str
    series: str
    side: str
    direction: str
    count: int
    entry_price: float
    entry_fee: float
    exit_price: float
    exit_fee: float
    reason: str
    t_rem: float
    edge: float

    @property
    def pnl(self) -> float:
        return self.count * (self.exit_price - self.entry_price) - self.entry_fee - self.exit_fee


def rolling_sigma(closes: list[float], halflife: float) -> list[float | None]:
    """Volatility known after each close; same estimator as model.ewma_sigma."""
    lam = 0.5 ** (1.0 / halflife)
    out: list[float | None] = [None] * len(closes)
    var, seed = None, []
    for i in range(1, len(closes)):
        if closes[i - 1] <= 0 or closes[i] <= 0:
            continue
        r = math.log(closes[i] / closes[i - 1])
        if var is None:
            seed.append(r * r)
            if len(seed) == 10:
                var = sum(seed) / 10.0
        else:
            var = lam * var + (1.0 - lam) * r * r
        if var is not None:
            out[i] = clamp_sigma(math.sqrt(var / 60.0))
    return out


def _strike(q) -> float | None:
    if q.strike_type.startswith("greater"):
        return q.floor
    if q.strike_type.startswith("less"):
        return q.cap
    return (q.floor + q.cap) / 2 if q.floor is not None and q.cap is not None else None


def _price_near(px_at: dict[int, float], ts: float) -> float | None:
    t = int(ts) // 60 * 60
    for k in (t, t - 60, t + 60, t - 120):
        if k in px_at:
            return px_at[k]
    return None


def _bid_high(c: dict, side: str) -> float | None:
    """Best bid reached during the minute (what a resting sell order could have hit)."""
    if side == "yes":
        v = num((c.get("yes_bid") or {}).get("high_dollars"))
        return v if v is not None and 0 < v < 1 else None
    v = num((c.get("yes_ask") or {}).get("low_dollars"))  # NO bid = 1 - YES ask
    return round(1 - v, 4) if v is not None and 0 < v < 1 else None


def simulate_market(q0, candles, outcome: int, px_at, sig_at, cfg, lag_min: int = 0,
                    obs: list | None = None) -> SimTrade | None:
    """One market, at most one trade, same rules as the live bot."""
    pos = None
    for c in candles:
        e = int(c["end_period_ts"])
        t_rem = q0.close_ts - e
        if pos is not None and cfg.exit_style == "target":
            best, n, fee, t_entry = pos
            hi = _bid_high(c, best.side)
            if best.take_profit is not None and hi is not None and hi >= best.take_profit - 1e-9:
                # the resting take-profit order fills at its limit price
                return SimTrade(q0.ticker, q0.series, best.side, best.direction, n, best.price, fee,
                                best.take_profit, taker_fee(best.take_profit, n, cfg.taker_fee_rate),
                                "take_profit", t_entry, best.edge)
        if t_rem < 60:
            continue
        spot, sigma = px_at.get(e - 60 * lag_min), sig_at.get(e - 60 * lag_min)
        yb = num((c.get("yes_bid") or {}).get("close_dollars"))
        ya = num((c.get("yes_ask") or {}).get("close_dollars"))
        if spot is None or sigma is None or not yb or not ya or not (0 < yb < ya < 1):
            continue
        q = replace(q0, yes_bid=yb, yes_ask=ya, yes_bid_size=1e6, yes_ask_size=1e6)
        sigs, fy = evaluate(q, spot, sigma, e, cfg)
        if fy is None:
            continue
        if obs is not None and q.spread <= 0.10 and 0.03 < q.mid < 0.97:
            obs.append((fy[0], q.mid, outcome))
        if pos is None:
            if not sigs:
                continue
            best = max(sigs, key=lambda s: s.edge)
            n = size_for(best, cfg.max_trade_dollars, cfg.bankroll, cfg)
            if n < 1:
                continue
            best.plan(n, cfg)
            if best.edge >= cfg.min_edge:
                pos = (best, n, taker_fee(best.price, n, cfg.taker_fee_rate), t_rem)
            continue
        best, n, fee, t_entry = pos
        fair_side = fy[0] if best.side == "yes" else 1.0 - fy[0]
        bid = q.bid(best.side)
        reason = exit_decision(best.price, bid, fair_side, t_rem, n, cfg,
                               take_profit=best.take_profit, stop=best.stop)
        if reason:
            return SimTrade(q0.ticker, q0.series, best.side, best.direction, n, best.price, fee,
                            bid, taker_fee(bid, n, cfg.taker_fee_rate), reason, t_entry, best.edge)
    if pos is None:
        return None
    best, n, fee, t_entry = pos
    won = (outcome == 1) == (best.side == "yes")
    return SimTrade(q0.ticker, q0.series, best.side, best.direction, n, best.price, fee,
                    1.0 if won else 0.0, 0.0, "settled_win" if won else "settled_loss", t_entry, best.edge)


def _line(name: str, s: Stats) -> str:
    if not s.n:
        return f"  {name:<22} no trades"
    return (f"  {name:<22} {s.n:>4} trades | win {s.win_rate * 100:3.0f}% vs {s.avg_price * 100:3.0f}% paid | "
            f"P&L {money(s.pnl):>9} | ROI {s.roi * 100:+5.1f}% | t {s.t:+.1f}")


def load_markets(cfg, series, days, client, out, near_pct):
    """Fetch settled markets + their candlesticks once, so variants re-use them."""
    now = time.time()
    start = now - days * 86400
    out(f"Loading BTC 1-minute prices from Coinbase ({days:g} days)...")
    candles = coinbase_candles(start - (cfg.vol_lookback_min + 15) * 60 - cfg.max_seconds_to_close, now)
    ends = [c[0] + 60 for c in candles]
    closes = [c[4] for c in candles]
    px_at = dict(zip(ends, closes))
    sig_at = dict(zip(ends, rolling_sigma(closes, cfg.vol_halflife_min)))
    minute_avg = {c[0] + 60: (c[1] + c[2] + c[3] + c[4]) / 4 for c in candles}
    loaded, basis = [], []
    for s in series:
        ms = [m for m in client.markets(series_ticker=s, status="settled", min_close_ts=int(start),
                                        max_close_ts=int(now), max_pages=60)
              if (m.get("result") or "") in ("yes", "no")]
        picked = []
        for m in ms:
            q0 = parse_quote({**m, "status": "active"}, s)
            if q0 is None:
                continue
            ev, ca = num(m.get("expiration_value")), minute_avg.get(int(q0.close_ts))
            if ev and ca and s == "KXBTC15M":
                basis.append(ev - ca)
            first = max(iso_to_ts(m.get("open_time")), q0.close_ts - cfg.max_seconds_to_close)
            ref, k = _price_near(px_at, first), _strike(q0)
            # Pick strikes by distance from BTC when trading could start - never by the outcome.
            if ref and k and abs(k / ref - 1) <= near_pct:
                picked.append((q0, m, first))
        events = len({m.get("event_ticker") for m in ms})
        out(f"{s}: {len(ms)} settled markets in {events} events, {len(picked)} near the money -> "
            f"fetching Kalshi price history")
        for i, batch in enumerate(_batches(picked), 1):
            b_start = min(first for _, _, first in batch)
            b_end = max(q0.close_ts for q0, _, _ in batch)
            try:
                got = client.candlesticks_batch([q0.ticker for q0, _, _ in batch], int(b_start), int(b_end))
            except Exception:
                got = {}
            for q0, m, first in batch:
                cs = got.get(q0.ticker)
                if cs is None:  # batch call failed or skipped this market: fetch it alone
                    try:
                        cs = client.candlesticks(s, q0.ticker, int(first), int(q0.close_ts), 1)
                    except Exception as e:
                        out(f"  skipped {q0.ticker}: {e}")
                        continue
                cs = [c for c in cs if int(c["end_period_ts"]) >= first]
                loaded.append((q0, cs, 1 if m["result"] == "yes" else 0))
            if i % 20 == 0:
                out(f"  ...{sum(1 for _ in loaded)} markets loaded")
    return loaded, px_at, sig_at, basis


def _batches(picked, max_tickers: int = 20, max_window: float = 6 * 3600):
    """Group markets with nearby time windows so one request covers many of them."""
    batch: list = []
    for item in sorted(picked, key=lambda x: x[0].close_ts):
        if batch and (len(batch) >= max_tickers or item[0].close_ts - min(f for _, _, f in batch) > max_window):
            yield batch
            batch = []
        batch.append(item)
    if batch:
        yield batch


def run_backtest(cfg, series=("KXBTC15M", "KXBTCD"), days: float = 3.0, *, client=None, out=print,
                 lag_min: int = 0, near_pct: float = 0.01) -> dict:
    client = client or Kalshi(PROD_URL, min_interval=0.3)
    loaded, px_at, sig_at, basis = load_markets(cfg, series, days, client, out, near_pct)

    def simulate(c, obs=None):
        trades = []
        for q0, cs, outcome in loaded:
            t = simulate_market(q0, cs, outcome, px_at, sig_at, c, lag_min, obs)
            if t:
                trades.append(t)
        return trades

    obs: list = []
    trades = simulate(cfg, obs)
    st = trade_stats(trades)
    b_bot = brier((p, o) for p, _, o in obs)
    b_mkt = brier((m, o) for _, m, o in obs)

    out("")
    out(f"BACKTEST - last {days:g} days | {len(loaded)} markets replayed | min edge {cfg.min_edge * 100:.0f}c | "
        f"${cfg.max_trade_dollars:g} max per trade")
    out(_line(f"Your settings ({cfg.exit_style})", st))
    out("  Exit styles on the same entries:")
    for name, c in (("Follow the calls: TP+stop", replace(cfg, exit_style="target")),
                    ("Entry + TP, no stop", replace(cfg, exit_style="target", stop_loss_pct=0.0)),
                    ("Fair-value exits + stop", replace(cfg, exit_style="dynamic", edge_exit=True)),
                    ("Hold to settlement", replace(cfg, exit_style="dynamic", edge_exit=False, stop_loss_pct=0.0))):
        out(_line(f"    {name}", trade_stats(simulate(c))))
    for s in series:
        out(_line(f"  {s}", trade_stats([t for t in trades if t.series == s])))
    for d in sorted({t.direction for t in trades}):
        out(_line(f"  bets {d}", trade_stats([t for t in trades if t.direction == d])))
    out("  Min-edge sensitivity (does a bigger 'edge' actually win more?):")
    for me in (0.02, 0.04, 0.08, 0.12):
        out(_line(f"    min edge {me * 100:.0f}c", trade_stats(simulate(replace(cfg, min_edge=me)))))
    if b_bot is not None:
        out(f"  Prediction check on {len(obs):,} minute snapshots (Brier, lower = better): "
            f"bot {b_bot:.4f} vs Kalshi {b_mkt:.4f} -> {'BOT' if b_bot < b_mkt else 'KALSHI'} is sharper")
    if len(basis) > 5:
        out(f"  BRTI vs Coinbase at settlement: mean {statistics.fmean(basis):+.1f} USD, "
            f"sd {statistics.pstdev(basis):.1f} USD over {len(basis)} markets (SPOT_BASIS_USD = {cfg.spot_basis_usd:g})")
    out("  VERDICT: " + verdict(st, b_bot, b_mkt, min_trades=30))
    out("  Caveat: assumes fills at the quoted price and uses Coinbase in place of BRTI. Real results are usually worse.")
    return {"trades": trades, "stats": st, "brier_bot": b_bot, "brier_market": b_mkt, "basis": basis,
            "observations": len(obs)}
