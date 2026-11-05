"""Pricing math. Pure functions, no network.

What a Kalshi BTC contract settles on: the simple average of CF Benchmarks'
BRTI over the 60 seconds before close. The bot treats BTC as a driftless
random walk over short horizons, estimates volatility from recent 1-minute
returns, and computes the probability that the 60-second average lands on
the YES side of the strike.

That is a *fair value*, not a forecast of direction. The bot only bets when
Kalshi's price disagrees with fair value by more than fees plus a margin,
and the disagreement survives both low- and high-volatility assumptions.
"""
from __future__ import annotations

import math
from typing import Sequence

SECONDS_PER_YEAR = 365.0 * 86400.0
_SQRT2 = math.sqrt(2.0)


def ncdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / _SQRT2))


def taker_fee(price: float, count: float, rate: float = 0.07, multiplier: float = 1.0) -> float:
    """Kalshi's quadratic trading fee: ceil_to_cent(rate * C * P * (1 - P)).

    About 1.75 cents per contract at 50 cents, less near 0 or 1. Charged
    when you trade, not at settlement.
    """
    if count <= 0:
        return 0.0
    raw = rate * multiplier * count * price * (1.0 - price)
    return math.ceil(raw * 100.0 - 1e-9) / 100.0


def annualized(sigma_per_sqrt_s: float) -> float:
    return sigma_per_sqrt_s * math.sqrt(SECONDS_PER_YEAR)


def clamp_sigma(sigma: float, floor_annual: float = 0.15, cap_annual: float = 3.0) -> float:
    lo = floor_annual / math.sqrt(SECONDS_PER_YEAR)
    hi = cap_annual / math.sqrt(SECONDS_PER_YEAR)
    return min(max(sigma, lo), hi)


def ewma_sigma(closes: Sequence[float], halflife: float = 30.0, step_seconds: float = 60.0) -> float:
    """Volatility per sqrt(second) from evenly spaced closes (EWMA of squared log returns)."""
    rets = [math.log(b / a) for a, b in zip(closes, closes[1:]) if a > 0 and b > 0]
    if len(rets) < 10:
        return clamp_sigma(0.5 / math.sqrt(SECONDS_PER_YEAR))  # no data: assume 50%/yr
    lam = 0.5 ** (1.0 / halflife)
    var = sum(r * r for r in rets[:10]) / 10.0
    for r in rets[10:]:
        var = lam * var + (1.0 - lam) * r * r
    return clamp_sigma(math.sqrt(var / step_seconds))


def settle_dist(spot: float, t_rem: float, sigma: float, window: float = 60.0,
                realized_avg: float | None = None, basis_usd: float = 0.0) -> tuple[float, float]:
    """Mean and standard deviation (USD) of the settlement value.

    Settlement = average price over the final `window` seconds. For a random
    walk, averaging over the window removes 2/3 of the window's variance:
        before the window:  Var = (S*sigma)^2 * (t - 2w/3)
        inside the window:  the elapsed part is known; the remaining t
                            seconds contribute (t/w)^2 * (S*sigma)^2 * t/3
    """
    t = max(t_rem, 0.0)
    if t >= window:
        mean = spot
        var = (spot * sigma) ** 2 * (t - 2.0 * window / 3.0)
    else:
        elapsed = window - t
        known = spot if realized_avg is None else realized_avg
        mean = (elapsed * known + t * spot) / window
        var = (t / window) ** 2 * (spot * sigma) ** 2 * (t / 3.0)
    var += basis_usd**2
    return mean, math.sqrt(max(var, 1e-12))


def prob_above(level: float, mean: float, sd: float) -> float:
    return 1.0 - ncdf((level - mean) / sd)


def yes_probability(strike_type: str, floor: float | None, cap: float | None,
                    mean: float, sd: float) -> float | None:
    st = (strike_type or "").lower()
    if st in ("greater", "greater_or_equal"):
        return None if floor is None else prob_above(floor, mean, sd)
    if st in ("less", "less_or_equal"):
        return None if cap is None else 1.0 - prob_above(cap, mean, sd)
    if st == "between":
        if floor is None or cap is None:
            return None
        return max(0.0, prob_above(floor, mean, sd) - prob_above(cap, mean, sd))
    return None


def fair_yes(strike_type: str, floor: float | None, cap: float | None, spot: float, t_rem: float,
             sigma: float, *, window: float = 60.0, realized_avg: float | None = None,
             basis_usd: float = 0.0, mults: Sequence[float] = (0.75, 1.0, 1.35),
             weights: Sequence[float] = (0.25, 0.5, 0.25)) -> tuple[float, float, float] | None:
    """(blended, lowest, highest) YES probability across volatility scenarios.

    Blending several volatilities gives fatter tails than a single normal,
    which matches BTC better. The low/high pair is the "am I sure?" check.
    """
    probs = []
    for m in mults:
        mean, sd = settle_dist(spot, t_rem, sigma * m, window, realized_avg, basis_usd)
        p = yes_probability(strike_type, floor, cap, mean, sd)
        if p is None:
            return None
        probs.append(min(max(p, 0.0), 1.0))
    blended = sum(w * p for w, p in zip(weights, probs))
    return blended, min(probs), max(probs)


def kelly_fraction(p_win: float, cost: float) -> float:
    """Full-Kelly bankroll fraction for a $1-payout contract bought at `cost`."""
    if not 0 < cost < 1:
        return 0.0
    return max(0.0, (p_win - cost) / (1.0 - cost))
