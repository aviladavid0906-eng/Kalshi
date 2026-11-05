"""Performance numbers and the go / no-go verdict."""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta

from .risk import start_of_local_day


@dataclass
class Stats:
    n: int = 0
    wins: int = 0
    pnl: float = 0.0
    fees: float = 0.0
    cost: float = 0.0
    mean: float = 0.0
    se: float = 0.0
    avg_price: float = 0.0
    max_drawdown: float = 0.0

    @property
    def win_rate(self) -> float:
        return self.wins / self.n if self.n else 0.0

    @property
    def edge_vs_book(self) -> float:
        """Win rate minus the average price paid (= the market's implied odds)."""
        return self.win_rate - self.avg_price

    @property
    def t(self) -> float:
        return self.mean / self.se if self.se > 0 else 0.0

    @property
    def roi(self) -> float:
        return self.pnl / self.cost if self.cost else 0.0


def trade_stats(trades) -> Stats:
    """Works on journal Positions and backtest SimTrades (needs pnl, count, entry_price, fees)."""
    s = Stats()
    pnls = []
    equity = peak = 0.0
    for t in trades:
        pnl = t.pnl or 0.0
        pnls.append(pnl)
        s.n += 1
        s.wins += pnl > 0
        s.pnl += pnl
        s.fees += (t.entry_fee or 0.0) + (t.exit_fee or 0.0)
        s.cost += t.count * t.entry_price + (t.entry_fee or 0.0)
        s.avg_price += t.entry_price
        equity += pnl
        peak = max(peak, equity)
        s.max_drawdown = max(s.max_drawdown, peak - equity)
    if s.n:
        s.avg_price /= s.n
        s.mean = s.pnl / s.n
    if s.n > 1:
        var = sum((x - s.mean) ** 2 for x in pnls) / (s.n - 1)
        s.se = math.sqrt(var / s.n)
    return s


def brier(pairs) -> float | None:
    """Mean squared error of probability forecasts (lower is better)."""
    pairs = list(pairs)
    if not pairs:
        return None
    return sum((p - o) ** 2 for p, o in pairs) / len(pairs)


def verdict(s: Stats, brier_bot: float | None, brier_mkt: float | None, min_trades: int) -> str:
    if s.n < min_trades:
        return f"NOT ENOUGH DATA - {s.n}/{min_trades} closed trades. Keep paper trading."
    if s.pnl <= 0:
        return "NO EDGE - lost money after fees. Do not put real money on this setup."
    if brier_bot is not None and brier_mkt is not None and brier_bot >= brier_mkt:
        return ("MARKET IS SHARPER - Kalshi's own prices predicted outcomes better than the bot, "
                "so the profit so far is most likely luck. Do not go live.")
    if s.t < 2.0:
        return (f"UNPROVEN - up ${s.pnl:.2f}, but the record could still be luck "
                f"(t = {s.t:.1f}, need 2.0+). Keep paper trading.")
    return "PASS SO FAR - profitable with a statistically solid record. Next: demo exchange, then tiny live limits."


def money(x: float) -> str:
    return f"-${abs(x):,.2f}" if x < 0 else f"+${x:,.2f}"


def hours(x: float, wage: float) -> str:
    return f"{abs(x) / wage:.1f} h of work" if wage > 0 else ""


def _week_start(now: float) -> float:
    day = datetime.fromtimestamp(start_of_local_day(now))
    return (day - timedelta(days=day.weekday())).timestamp()


def _month_start(now: float) -> float:
    d = datetime.fromtimestamp(now)
    return datetime(d.year, d.month, 1).timestamp()


def render_report(journal, cfg, mode: str, now: float) -> str:
    lines = [f"btcbot report - {mode.upper()} trades"]
    for name, since in (("Today", start_of_local_day(now)), ("This week", _week_start(now)),
                        ("This month", _month_start(now)), ("All time", 0.0)):
        s = trade_stats(journal.closed_positions(mode, since))
        lines.append(f"  {name:<10} {s.n:>4} trades | win {s.win_rate * 100:3.0f}% | P&L {money(s.pnl):>9} "
                     f"({hours(s.pnl, cfg.hourly_wage)}) | fees ${s.fees:.2f}")
    allt = trade_stats(journal.closed_positions(mode))
    lines.append(f"  Open now: {len(journal.open_positions(mode))} positions, ${journal.open_cost(mode):.2f} at risk")
    if allt.n:
        lines.append(f"  Edge vs book: won {allt.win_rate:.0%} vs {allt.avg_price:.0%} avg price paid "
                     f"({allt.edge_vs_book * 100:+.1f} pts) | worst drawdown ${allt.max_drawdown:.2f}")
    obs = journal.resolved_observations()
    b_bot = brier((r["p_model"], r["result"] == "yes") for r in obs)
    b_mkt = brier(((r["yes_bid"] + r["yes_ask"]) / 2, r["result"] == "yes") for r in obs)
    if b_bot is not None:
        sharper = "bot" if b_bot < b_mkt else "market"
        lines.append(f"  Prediction check ({len(obs)} snapshots): bot {b_bot:.4f} vs market {b_mkt:.4f} "
                     f"(Brier, lower = better) -> {sharper} is sharper")
    lines.append("  VERDICT: " + verdict(allt, b_bot, b_mkt, cfg.min_paper_trades))
    return "\n".join(lines)
