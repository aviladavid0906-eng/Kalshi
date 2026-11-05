"""End-to-end paper trading with fake Kalshi/BTC feeds (no network)."""
import math
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from btcbot.bot import Bot, live_gate
from btcbot.config import LIVE_CONFIRM_PHRASE, Settings
from btcbot.journal import Journal
from btcbot.report import brier, render_report, trade_stats, verdict
from btcbot.risk import RiskManager, size_for
from btcbot.strategy import Signal, parse_quote

T0 = 1_793_900_000.0  # a fixed "now"
CLOSE = T0 + 600
CLOSE_ISO = datetime.fromtimestamp(CLOSE, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def market(bid, ask, status="active", result=""):
    return {"ticker": "KXBTC15M-A", "event_ticker": "KXBTC15M-EV", "status": status,
            "strike_type": "greater_or_equal", "floor_strike": 100000.0,
            "close_time": CLOSE_ISO, "yes_bid_dollars": f"{bid:.4f}", "yes_ask_dollars": f"{ask:.4f}",
            "yes_bid_size_fp": "100.00", "yes_ask_size_fp": "100.00", "yes_sub_title": "Target Price: $100,000",
            "result": result}


class FakeKalshi:
    def __init__(self):
        self.bid, self.ask, self.result = 0.30, 0.31, ""

    def events(self, series, status="open"):
        return [{"event_ticker": "KXBTC15M-EV", "strike_date": CLOSE_ISO}] if series == "KXBTC15M" else []

    def markets(self, **kw):
        return [market(self.bid, self.ask)]

    def market(self, ticker):
        return market(self.bid, self.ask, status="finalized" if self.result else "active", result=self.result)

    def orderbook(self, ticker, depth=20):
        return {"orderbook_fp": {"yes_dollars": [[f"{self.bid:.4f}", "100"]],
                                 "no_dollars": [[f"{1 - self.ask:.4f}", "100"]]}}


class FakeSpot:
    def __init__(self, px):
        self.px = px

    def price(self, now=None):
        return self.px

    def average_since(self, ts):
        return None


def make_bot(tmp_path, **kw):
    cfg = replace(Settings(), series=("KXBTC15M",), db_path=str(tmp_path / "t.db"), **kw)
    msgs = []
    closes = [100_000 * math.exp(0.0007 * (1 if i % 2 else -1)) for i in range(180)]
    bot = Bot(cfg, client=FakeKalshi(), spot=FakeSpot(100_000.0), journal=Journal(cfg.db_path),
              notifier=lambda t, m, push=True: msgs.append((t, m)), closes_fn=lambda now: closes)
    return bot, msgs


def test_close_time_fixture_matches():
    assert parse_quote(market(0.3, 0.31), "KXBTC15M").close_ts == CLOSE


def test_paper_bot_buys_mispriced_up_then_settles(tmp_path):
    bot, msgs = make_bot(tmp_path)
    s = bot.step(now=T0)
    assert s["entries"] == 1
    pos = bot.journal.open_positions("paper")[0]
    assert pos.side == "yes" and pos.entry_price == 0.31 and pos.count >= 1
    assert pos.cost <= bot.cfg.max_trade_dollars
    assert any(t.startswith("BOUGHT UP") for t, _ in msgs)
    # same event: no second bet
    assert bot.step(now=T0 + 10)["entries"] == 0
    # market settles YES
    bot.client.result = "yes"
    bot.step(now=CLOSE + 60)
    closed = bot.journal.closed_positions("paper")
    assert len(closed) == 1 and closed[0].exit_reason == "settled_win"
    assert closed[0].pnl == pytest.approx(closed[0].count * (1 - 0.31) - closed[0].entry_fee)
    assert "VERDICT" in render_report(bot.journal, bot.cfg, "paper", CLOSE + 60)


def test_take_profit_when_market_overshoots(tmp_path):
    bot, msgs = make_bot(tmp_path)
    bot.step(now=T0)
    bot.client.bid, bot.client.ask = 0.80, 0.81   # fair is ~0.5, bid now 0.80
    assert bot.step(now=T0 + 10)["exits"] == 1
    closed = bot.journal.closed_positions("paper")[0]
    assert closed.exit_reason == "take_profit" and closed.pnl > 0
    # NO now looks cheap (fair ~0.5, NO ask 0.20), but the bot never flips sides in a market it traded
    assert bot.step(now=T0 + 20)["entries"] == 0


def test_stop_file_blocks_new_trades(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "STOP").write_text("")
    bot, msgs = make_bot(tmp_path)
    assert bot.step(now=T0)["entries"] == 0
    assert any(t == "NO NEW TRADES" for t, _ in msgs)


def test_daily_loss_limit(tmp_path):
    cfg = replace(Settings(), max_daily_loss_dollars=3.0)
    j = Journal(str(tmp_path / "r.db"))
    pid = j.open_position(mode="paper", ticker="X", series="S", event_ticker="E", side="yes", count=10,
                          entry_price=0.3, entry_fee=0.0, close_ts=T0, p_win=0.5, edge=0.1, ts=T0)
    j.settle(pid, "no", ts=T0 + 1)   # -$3.00
    ok, why = RiskManager(cfg, j, "paper").can_open(T0 + 2)
    assert not ok and "daily loss" in why


def test_sizing_respects_caps():
    cfg = Settings()
    q = parse_quote(market(0.30, 0.31), "KXBTC15M")
    sig = Signal(q, "yes", 0.31, 0.5, 0.45, 0.12, 600, 0.5)
    n = size_for(sig, budget=5.0, equity=100.0, cfg=cfg)
    assert 1 <= n and n * 0.31 <= 5.0
    assert size_for(sig, budget=0.2, equity=100.0, cfg=cfg) == 0
    big = size_for(sig, budget=5.0, equity=1_000_000.0, cfg=cfg)
    assert big * 0.31 + 0.1 <= 5.0 + 1e-9   # budget wins over Kelly


def test_partial_close_splits_position(tmp_path):
    j = Journal(str(tmp_path / "p.db"))
    pid = j.open_position(mode="paper", ticker="X", series="S", event_ticker="E", side="no", count=10,
                          entry_price=0.4, entry_fee=0.10, close_ts=T0, p_win=0.5, edge=0.1, ts=T0)
    pnl = j.close_position(pid, exit_price=0.5, exit_fee=0.02, reason="take_profit", count=4)
    assert pnl == pytest.approx(4 * 0.1 - 0.02 - 0.04)
    assert j.get(pid).count == 6 and j.open_cost("paper") == pytest.approx(6 * 0.4 + 0.06)


def test_live_gate(tmp_path):
    j = Journal(str(tmp_path / "g.db"))
    assert "LIVE_CONFIRM" in live_gate(Settings(), j)
    assert "closed trades" in live_gate(replace(Settings(), live_confirm=LIVE_CONFIRM_PHRASE), j)
    assert live_gate(replace(Settings(), live_confirm=LIVE_CONFIRM_PHRASE, skip_paper_gate=True), j) is None


def test_report_math_and_verdicts():
    class T:
        def __init__(self, pnl):
            self.pnl, self.count, self.entry_price, self.entry_fee, self.exit_fee = pnl, 1, 0.5, 0.0, 0.0
    s = trade_stats([T(0.5), T(-0.5), T(0.5)])
    assert s.n == 3 and s.wins == 2 and s.pnl == pytest.approx(0.5) and s.max_drawdown == pytest.approx(0.5)
    assert brier([(1.0, 1), (0.0, 1)]) == 0.5
    assert verdict(s, None, None, 50).startswith("NOT ENOUGH")
    many = trade_stats([T(-0.1)] * 60)
    assert verdict(many, None, None, 50).startswith("NO EDGE")
    good = trade_stats([T(0.5), T(0.4)] * 40)
    assert verdict(good, 0.20, 0.21, 50).startswith("PASS")
    assert verdict(good, 0.22, 0.21, 50).startswith("MARKET IS SHARPER")


class FlakyLiveClient:
    """Order call times out, but the order actually filled on the exchange."""
    authenticated = True

    def __init__(self, fill):
        self.pos, self.fill = 0.0, fill

    def position_fp(self, ticker):
        return self.pos

    def create_order(self, ticker, action, side, count, price, **kw):
        if self.fill:
            self.pos += count
        raise TimeoutError("read timed out")


def test_live_order_timeout_reconciles_with_account(tmp_path):
    from btcbot.broker import LiveBroker
    cfg = Settings()
    j = Journal(str(tmp_path / "l.db"))
    q = parse_quote(market(0.30, 0.31), "KXBTC15M")
    sig = Signal(q, "yes", 0.31, 0.5, 0.45, 0.12, 600, 0.5)
    pid = LiveBroker(FlakyLiveClient(fill=True), j, cfg, "demo").buy(sig, 3, T0)
    assert pid is not None and j.get(pid).count == 3        # recorded, not lost
    with pytest.raises(TimeoutError):                         # nothing filled: surface the error
        LiveBroker(FlakyLiveClient(fill=False), j, cfg, "demo").buy(sig, 3, T0)


class StaleListKalshi(FakeKalshi):
    """Market list still shows the old cheap ask; the live order book has moved."""

    def __init__(self):
        super().__init__()
        self.book_bid, self.book_ask = 0.54, 0.55

    def orderbook(self, ticker, depth=20):
        return {"orderbook_fp": {"yes_dollars": [[f"{self.book_bid:.4f}", "100"]],
                                 "no_dollars": [[f"{1 - self.book_ask:.4f}", "100"]]}}


def test_stale_list_price_is_ignored(tmp_path):
    bot, msgs = make_bot(tmp_path)
    bot.client = bot.scanner.client = bot.broker.client = StaleListKalshi()
    s = bot.step(now=T0)
    assert s["entries"] == 0                          # 0.31 on the list was a phantom
    assert not any("SIGNAL" in t for t, _ in msgs)    # and no misleading alert either
    bot.client.book_bid, bot.client.book_ask = 0.30, 0.31  # the book really is cheap now
    assert bot.step(now=T0 + 10)["entries"] == 1


def test_scanner_relists_when_window_closes(tmp_path):
    bot, _ = make_bot(tmp_path)
    calls = []
    real = bot.client.events
    bot.client.events = lambda series, status="open": calls.append(series) or real(series, status)
    bot.scanner.events(T0)
    bot.scanner.events(T0 + 30)            # cached
    assert len(calls) == 1
    bot.scanner.events(CLOSE + 1)          # cached window closed -> re-list right away
    assert len(calls) == 2


def test_live_quotes_prices_held_and_nearest(tmp_path):
    bot, _ = make_bot(tmp_path)
    q = parse_quote(market(0.30, 0.31), "KXBTC15M")
    far = replace(q, ticker="FAR", floor=150_000.0)
    out = bot.live_quotes([far, q], 100_000.0, T0, limit=1)
    assert [x.ticker for x in out] == [q.ticker]
    assert out[0].yes_ask == pytest.approx(0.31) and out[0].yes_ask_size == 100
