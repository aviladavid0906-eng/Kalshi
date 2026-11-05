"""Hard limits that hold no matter what the model says."""
from __future__ import annotations

import math
from datetime import datetime
from pathlib import Path

from .model import kelly_fraction, taker_fee


def start_of_local_day(now: float) -> float:
    d = datetime.fromtimestamp(now)
    return datetime(d.year, d.month, d.day).timestamp()


def size_for(sig, budget: float, equity: float, cfg) -> int:
    """Contracts to buy: fractional Kelly, never above `budget` dollars or the shown size."""
    if budget <= 0 or equity <= 0:
        return 0
    n_cap = 0
    k = int(budget // sig.price)
    while k > 0:
        if k * sig.price + taker_fee(sig.price, k, cfg.taker_fee_rate) <= budget + 1e-9:
            n_cap = k
            break
        k -= 1
    if n_cap < 1:
        return 0
    fee_pc = taker_fee(sig.price, n_cap, cfg.taker_fee_rate) / n_cap
    f = kelly_fraction(sig.p_cons, sig.price + fee_pc) * cfg.kelly_fraction
    if f <= 0:
        return 0
    n_kelly = max(1, math.floor(f * equity / (sig.price + fee_pc)))  # 1 contract is the smallest bet
    shown = math.floor(sig.quote.ask_size(sig.side))
    return max(0, min(n_cap, n_kelly, shown))


class RiskManager:
    def __init__(self, cfg, journal, mode: str, stop_file: str = "STOP"):
        self.cfg = cfg
        self.journal = journal
        self.mode = mode
        self.stop_file = stop_file

    def status(self, now: float) -> dict:
        day = start_of_local_day(now)
        realized_today = self.journal.realized_pnl(self.mode, since=day)
        open_cost = self.journal.open_cost(self.mode)
        return {
            "realized_today": realized_today,
            "open_cost": open_cost,
            # Worst case: every open position goes to zero.
            "loss_room": self.cfg.max_daily_loss_dollars + realized_today - open_cost,
            "streak": self.journal.consecutive_losses(self.mode, since=day),
        }

    def can_open(self, now: float) -> tuple[bool, str]:
        if Path(self.stop_file).exists():
            return False, f"'{self.stop_file}' file found - delete it to resume trading"
        st = self.status(now)
        if st["loss_room"] <= 0.01:
            return False, f"daily loss limit (${self.cfg.max_daily_loss_dollars:.2f}) reached - done for today"
        if st["streak"] >= self.cfg.max_consecutive_losses:
            return False, f"{st['streak']} losses in a row today - paused until tomorrow"
        if st["open_cost"] >= self.cfg.max_open_dollars - 0.01:
            return False, "max open exposure reached"
        return True, ""

    def size(self, sig, *, cash: float, equity: float, now: float) -> int:
        st = self.status(now)
        budget = min(self.cfg.max_trade_dollars,
                     self.cfg.max_open_dollars - st["open_cost"],
                     st["loss_room"],
                     cash)
        return size_for(sig, budget, equity, self.cfg)
