import base64
import math
from dataclasses import replace

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from btcbot.config import Settings, _coerce
from btcbot.kalshi import Kalshi, build_order_payload, parse_book
from btcbot.model import (SECONDS_PER_YEAR, ewma_sigma, fair_yes, kelly_fraction, ncdf, settle_dist,
                          taker_fee, yes_probability)
from btcbot.strategy import direction, evaluate, exit_decision, parse_quote

SIG = 0.5 / math.sqrt(SECONDS_PER_YEAR)  # 50%/yr


def cfg(**kw):
    return replace(Settings(), **kw)


# --- fees & model ---------------------------------------------------------------
def test_fee_matches_kalshi_formula():
    assert taker_fee(0.50, 1) == 0.02          # 1.75c rounds up
    assert taker_fee(0.50, 100) == 1.75
    assert taker_fee(0.90, 10) == 0.07         # 6.3c rounds up
    assert taker_fee(0.50, 0) == 0.0


def test_ncdf():
    assert ncdf(0) == pytest.approx(0.5)
    assert ncdf(1.96) == pytest.approx(0.975, abs=1e-3)


def test_settle_dist_window_math():
    s, sig = 100_000.0, SIG
    m, sd = settle_dist(s, 600, sig)
    assert m == s and sd == pytest.approx(s * sig * math.sqrt(600 - 40))
    # inside the window the already-averaged part is locked in
    m, sd = settle_dist(s, 30, sig, realized_avg=99_000.0)
    assert m == pytest.approx((30 * 99_000 + 30 * 100_000) / 60)
    assert sd == pytest.approx(0.5 * s * sig * math.sqrt(10))


def test_probabilities_behave():
    p_atm = fair_yes("greater", 100_000, None, 100_000, 900, SIG)[0]
    assert p_atm == pytest.approx(0.5, abs=1e-6)
    above = fair_yes("greater", 99_500, None, 100_000, 900, SIG)
    below = fair_yes("greater", 100_500, None, 100_000, 900, SIG)
    assert above[0] > 0.5 > below[0]
    assert above[1] <= above[0] <= above[2]
    less = fair_yes("less", None, 100_500, 100_000, 900, SIG)[0]
    assert less == pytest.approx(1 - below[0])
    # range = P(above floor) - P(above cap)
    m, sd = settle_dist(100_000, 900, SIG)
    rng = yes_probability("between", 99_900, 100_100, m, sd)
    assert rng == pytest.approx(yes_probability("greater", 99_900, None, m, sd)
                                - yes_probability("greater", 100_100, None, m, sd))
    assert yes_probability("weird", 1, 2, m, sd) is None


def test_last_seconds_use_realized_average():
    # 10s left, 50s already averaged far above the strike: YES is nearly certain
    p = fair_yes("greater", 100_000, None, 100_050, 10, SIG, realized_avg=100_400, basis_usd=15)[0]
    assert p > 0.99


def test_ewma_sigma():
    closes = [100_000 * math.exp(0.001 * (1 if i % 2 else -1)) for i in range(200)]
    s = ewma_sigma(closes)
    assert s == pytest.approx(math.sqrt((0.002 ** 2) / 60), rel=0.05)
    assert ewma_sigma([1, 2]) > 0  # falls back to a default


def test_kelly():
    assert kelly_fraction(0.6, 0.5) == pytest.approx(0.2)
    assert kelly_fraction(0.4, 0.5) == 0.0


# --- Kalshi client pieces -----------------------------------------------------------
@pytest.mark.parametrize("action,side,price,want_side,want_price", [
    ("buy", "yes", 0.42, "bid", "0.4200"),
    ("sell", "yes", 0.42, "ask", "0.4200"),
    ("buy", "no", 0.30, "ask", "0.7000"),
    ("sell", "no", 0.30, "bid", "0.7000"),
])
def test_v2_order_mapping(action, side, price, want_side, want_price):
    o = build_order_payload("T", action, side, 3, price)
    assert (o["side"], o["price"], o["count"]) == (want_side, want_price, "3.00")
    assert o["time_in_force"] == "immediate_or_cancel"


def test_order_rejects_bad_price():
    with pytest.raises(ValueError):
        build_order_payload("T", "buy", "yes", 1, 1.0)


def test_signature_verifies(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    from cryptography.hazmat.primitives import serialization
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption())
    path = tmp_path / "k.pem"
    path.write_bytes(pem)
    c = Kalshi(key_id="abc", private_key_path=str(path))
    sig = c.sign("1700000000000", "get", "/trade-api/v2/portfolio/balance")
    key.public_key().verify(base64.b64decode(sig), b"1700000000000GET/trade-api/v2/portfolio/balance",
                            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
                            hashes.SHA256())


def test_parse_book():
    b = parse_book({"orderbook_fp": {"yes_dollars": [["0.40", "10"], ["0.41", "5"]],
                                     "no_dollars": [["0.55", "7"], ["0.57", "3"]]}})
    assert b["yes_bids"][0] == (0.41, 5.0)
    assert b["yes_asks"][0] == (0.43, 3.0)   # 1 - best NO bid
    assert b["no_asks"][0] == (0.59, 5.0)    # 1 - best YES bid


# --- strategy -------------------------------------------------------------------------
MARKET = {
    "ticker": "KXBTC15M-X", "event_ticker": "KXBTC15M-E", "status": "active", "strike_type": "greater_or_equal",
    "floor_strike": 100000.0, "close_time": "2026-11-05T19:45:00Z", "yes_bid_dollars": "0.3000",
    "yes_ask_dollars": "0.3100", "yes_bid_size_fp": "500.00", "yes_ask_size_fp": "800.00",
    "yes_sub_title": "Target Price: $100,000.00",
}


def test_parse_quote_and_sides():
    q = parse_quote(MARKET, "KXBTC15M")
    assert q.floor == 100000.0 and q.yes_ask == 0.31
    assert q.no_ask == 0.7 and q.no_bid == 0.69
    assert q.ask_size("no") == 500.0
    assert parse_quote({**MARKET, "status": "closed"}, "KXBTC15M") is None
    assert parse_quote({**MARKET, "floor_strike": None}, "KXBTC15M") is None


def test_direction_labels():
    assert direction("greater", "yes") == "UP" and direction("greater", "no") == "DOWN"
    assert direction("less", "yes") == "DOWN"
    assert direction("between", "no") == "OUT OF RANGE"


def test_evaluate_flags_cheap_yes_and_passes_fair_price():
    q = parse_quote(MARKET, "KXBTC15M")
    now = q.close_ts - 600
    # BTC sits at the strike -> fair ~0.50, YES offered at 0.31 -> buy UP
    sigs, fy = evaluate(q, 100_000, SIG, now, cfg())
    assert fy[0] == pytest.approx(0.5, abs=0.01)
    assert len(sigs) == 1 and sigs[0].side == "yes" and sigs[0].direction == "UP"
    assert sigs[0].edge >= 0.04
    # priced fairly -> no trade
    fair_q = replace(q, yes_bid=0.49, yes_ask=0.51)
    assert evaluate(fair_q, 100_000, SIG, now, cfg())[0] == []
    # too close to the close -> no trade
    assert evaluate(q, 100_000, SIG, q.close_ts - 30, cfg())[0] == []
    # spread too wide -> no trade
    assert evaluate(replace(q, yes_bid=0.05), 100_000, SIG, now, cfg())[0] == []


def test_exit_rules_dynamic():
    c = cfg(exit_style="dynamic", stop_loss_pct=0.5)
    assert exit_decision(0.40, 0.70, 0.60, 300, 5, c) == "take_profit"     # bid pays more than fair
    assert exit_decision(0.40, 0.18, 0.30, 300, 5, c) == "stop_loss"       # down 55%
    assert exit_decision(0.40, 0.35, 0.45, 300, 5, c) is None              # still worth holding
    assert exit_decision(0.40, 0.70, 0.60, 10, 5, c) is None               # too close to settlement
    assert exit_decision(0.40, 0.18, 0.30, 300, 5, cfg(exit_style="dynamic", stop_loss_pct=0.0)) is None


def test_exit_rules_target():
    c = cfg(exit_style="target", stop_loss_pct=0.5)
    assert exit_decision(0.31, 0.59, 0.50, 300, 2, c, take_profit=0.59, stop=0.15) == "take_profit"
    assert exit_decision(0.31, 0.59, 0.50, 5, 2, c, take_profit=0.59, stop=0.15) == "take_profit"  # resting order
    assert exit_decision(0.31, 0.58, 0.70, 300, 2, c, take_profit=0.59, stop=0.15) is None   # not there yet
    assert exit_decision(0.31, 0.15, 0.20, 300, 2, c, take_profit=0.59, stop=0.15) == "stop_loss"
    assert exit_decision(0.31, 0.15, 0.20, 300, 2, cfg(exit_style="target"), take_profit=0.59) is None


def test_exit_rules_hold_default():
    c = cfg()
    assert (c.exit_style, c.stop_loss_pct, c.series) == ("hold", 0.0, ("KXBTC15M",))
    assert exit_decision(0.31, 0.99, 0.50, 300, 2, c, take_profit=0.47) is None   # never sells early
    assert exit_decision(0.31, 0.05, 0.10, 300, 2, c) is None                      # no stop
    assert exit_decision(0.31, 0.05, 0.10, 300, 2, cfg(stop_loss_pct=0.5)) == "stop_loss"
    assert exit_decision(0.31, 0.90, 0.95, 300, 2, c, take_profit=None, stop=0.15) is None   # plan: hold


def test_trade_plan_prices():
    from btcbot.strategy import max_entry_price, stop_price, take_profit_price
    c = cfg()
    # worst-case 0.45 win chance: highest buy price that still clears 4c after fees
    e = max_entry_price(0.45, 2, c)
    assert e == 0.39 and 0.45 - e - taker_fee(e, 2) / 2 >= 0.04 - 1e-9
    assert 0.45 - 0.40 - taker_fee(0.40, 2) / 2 < 0.04
    # take profit: first price that beats fair value (0.565) + 1c after the sell fee
    tp = take_profit_price(0.565, 2, c)
    assert tp == 0.6 and tp - taker_fee(tp, 2) / 2 > 0.575
    assert take_profit_price(0.99, 2, c) is None          # nothing left to take: hold
    assert stop_price(0.31, cfg(stop_loss_pct=0.5)) == 0.15 and stop_price(0.31, c) is None


def test_signal_text_shows_the_plan():
    q = parse_quote(MARKET, "KXBTC15M")
    sigs, _ = evaluate(q, 100_000, SIG, q.close_ts - 600, cfg(exit_style="target", stop_loss_pct=0.5))
    t = sigs[0].text(3)
    assert "entry: buy at or below $" in t and "take profit: sell at $" in t and "stop: $" in t
    held = evaluate(q, 100_000, SIG, q.close_ts - 600, cfg())[0][0]
    assert held.take_profit is None and "take profit: none, hold to settlement" in held.text(3)
    assert "stop:" not in held.text(3)
    assert sigs[0].max_entry >= sigs[0].price and sigs[0].take_profit > sigs[0].price


def test_settings_coercion_and_validation(monkeypatch):
    assert _coerce(True, "0") is False and _coerce(1, "3") == 3 and _coerce((1.0,), "1,2") == (1.0, 2.0)
    with pytest.raises(ValueError):
        replace(Settings(), min_edge=-0.01).validate()
    monkeypatch.setenv("MAX_TRADE_DOLLARS", "50")
    monkeypatch.setenv("MAX_OPEN_DOLLARS", "20")
    with pytest.raises(ValueError):
        Settings.from_env(None)


def test_percent_take_profits():
    from btcbot.strategy import take_profit_for, take_profit_price
    c = cfg(exit_style="target")
    assert (c.tp_up_pct, c.tp_down_pct) == (0.50, 0.50)
    assert take_profit_for("UP", 0.30, 0.5, 2, cfg()) is None   # default style holds to settlement
    assert take_profit_for("UP", 0.30, 0.5, 2, c) == 0.45       # +50%
    assert take_profit_for("DOWN", 0.48, 0.5, 2, c) == 0.72     # today's bet: 48c -> 72c
    assert take_profit_for("UP", 0.31, 0.5, 2, c) == 0.47       # 0.465 rounds up to the cent
    assert take_profit_for("UP", 0.67, 0.9, 2, c) is None       # $1.005 is impossible: hold
    assert take_profit_for("DOWN", 0.30, 0.5, 2, cfg(exit_style="target", tp_down_pct=0.4)) == 0.42
    assert take_profit_for("UP", 0.30, 0.5, 2, cfg(exit_style="target", tp_up_pct=0.0)) == take_profit_price(0.5, 2, c)
    assert take_profit_for("IN RANGE", 0.30, 0.5, 2, c) == take_profit_price(0.5, 2, c)
    q = parse_quote(MARKET, "KXBTC15M")
    sig = evaluate(q, 100_000, SIG, q.close_ts - 600, c)[0][0]
    assert sig.direction == "UP" and sig.take_profit == 0.47 and "(+50% rule)" in sig.text(1)


def test_scan_hides_calls_that_fail_at_the_real_size(monkeypatch, capsys):
    import btcbot.__main__ as cli
    from btcbot.strategy import Signal
    q = parse_quote({**MARKET, "yes_bid_dollars": "0.1900", "yes_ask_dollars": "0.2000"}, "KXBTC15M")
    # edge clears 4c at the $5 reference size but not at the 2-contract size the bankroll allows
    sig = Signal(q, "yes", 0.20, 0.27, 0.2525, 0.041, 600, 0.27)  # 4.1c at 25x, 3.75c at 2x

    class FakeBot:
        def __init__(self, *a, **k):
            pass

        def scan(self, rows_limit=12):
            return 100_000.0, SIG, [{"quote": q, "fair": 0.27, "signals": [sig], "t_rem": 600, "best_raw": 0.04}]

    monkeypatch.setattr("btcbot.bot.Bot", FakeBot)

    class Args:
        rows = 5
    cli.cmd_scan(cfg(), Args())
    out = capsys.readouterr().out
    assert "BUY" not in out and "below the 4c bar" in out
