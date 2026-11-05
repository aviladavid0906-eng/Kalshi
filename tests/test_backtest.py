import math

from btcbot.backtest import _batches, rolling_sigma, simulate_market
from btcbot.config import Settings
from btcbot.model import SECONDS_PER_YEAR
from btcbot.strategy import parse_quote

CLOSE = 1_793_908_800  # minute-aligned


def candle(end_ts, bid, ask):
    return {"end_period_ts": end_ts, "yes_bid": {"close_dollars": f"{bid:.4f}"},
            "yes_ask": {"close_dollars": f"{ask:.4f}"}}


def q0():
    m = {"ticker": "KXBTC15M-B", "event_ticker": "E", "status": "active", "strike_type": "greater_or_equal",
         "floor_strike": 100000.0, "close_time": "2026-11-05T20:00:00Z", "yes_sub_title": "x"}
    q = parse_quote(m, "KXBTC15M")
    assert q.close_ts == CLOSE
    return q


def test_simulate_enters_cheap_up_and_settles():
    cfg = Settings()
    sig = 0.5 / math.sqrt(SECONDS_PER_YEAR)
    ends = [CLOSE - 60 * k for k in range(14, 0, -1)]
    px_at = {e: 100_000.0 for e in ends}
    sig_at = {e: sig for e in ends}
    cs = [candle(e, 0.30, 0.31) for e in ends]          # YES far below the ~0.50 fair value
    obs = []
    t = simulate_market(q0(), cs, 1, px_at, sig_at, cfg, 0, obs)
    assert t.side == "yes" and t.direction == "UP" and t.reason == "settled_win"
    assert t.pnl == t.count * (1 - 0.31) - t.entry_fee
    assert obs and all(o[2] == 1 for o in obs)
    lost = simulate_market(q0(), cs, 0, px_at, sig_at, cfg, 0, None)
    assert lost.reason == "settled_loss" and lost.pnl < 0


def test_simulate_no_trade_when_fair():
    cfg = Settings()
    sig = 0.5 / math.sqrt(SECONDS_PER_YEAR)
    ends = [CLOSE - 60 * k for k in range(14, 0, -1)]
    cs = [candle(e, 0.49, 0.51) for e in ends]
    assert simulate_market(q0(), cs, 1, {e: 100_000.0 for e in ends}, {e: sig for e in ends}, cfg) is None


def test_batches_group_by_time_and_size():
    class Q:
        def __init__(self, c):
            self.close_ts = c
    picked = [(Q(i * 900), None, i * 900 - 900) for i in range(45)]
    bs = list(_batches(picked, max_tickers=20, max_window=6 * 3600))
    assert sum(len(b) for b in bs) == 45 and all(len(b) <= 20 for b in bs)


def test_rolling_sigma_warms_up():
    closes = [100.0 * math.exp(0.001 * (1 if i % 2 else -1)) for i in range(30)]
    s = rolling_sigma(closes, 30)
    assert s[5] is None and s[-1] is not None


def test_target_take_profit_fills_on_the_minute_high():
    cfg = Settings()
    sig = 0.5 / math.sqrt(SECONDS_PER_YEAR)
    ends = [CLOSE - 60 * k for k in range(14, 0, -1)]
    cs = [candle(e, 0.30, 0.31) for e in ends]
    cs[5]["yes_bid"]["high_dollars"] = "0.9000"        # bid spiked inside minute 6
    t = simulate_market(q0(), cs, 0, {e: 100_000.0 for e in ends}, {e: sig for e in ends}, cfg)
    assert t.reason == "take_profit" and t.exit_price < 0.9   # sold at the TP limit, not the spike
    assert t.pnl > 0                                           # even though the market settled NO
