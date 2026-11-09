from btcbot.bet import best_bet, in_entry_window, max_contracts, next_close, run_window, ticker_for, walk_cost
from btcbot.config import Settings
from btcbot.kalshi import parse_book


def book(yes_bid, no_bid, qty="5000"):
    return parse_book({"orderbook_fp": {"yes_dollars": [[f"{yes_bid:.4f}", qty]], "no_dollars": [[f"{no_bid:.4f}", qty]]}})


def test_sizing_spends_the_budget_including_fees():
    lv = [(0.5, 100.0), (0.51, 1000.0)]
    n = max_contracts(lv, 100.0)
    assert walk_cost(lv, n)[0] <= 100.0 < walk_cost(lv, n + 1)[0]


def test_best_bet_needs_the_edge_after_fees():
    cfg = Settings()
    b = book(0.49, 0.50)                      # UP ask 0.50, DOWN ask 0.51
    assert best_bet(b, 0.50, 100, cfg) is None             # fair price: no bet
    up = best_bet(b, 0.56, 100, cfg)                         # 56% vs 50c: ~+4.3c after fees
    assert up["side"] == "yes" and up["ev"] >= 0.04 and up["cost"] <= 100
    assert best_bet(b, 0.545, 100, cfg) is None             # +2.7c: below the 4c bar


def test_no_long_shots():
    cfg = Settings()
    b = book(0.79, 0.20)                      # UP ask 0.80, DOWN ask 0.21
    assert best_bet(b, 0.70, 100, cfg) is None             # DOWN at 21c "looks" +8c, but under 35c


def test_entry_window_skips_first_five_minutes_and_last_two():
    cfg = Settings()
    assert not in_entry_window(890, cfg) and not in_entry_window(601, cfg)
    assert in_entry_window(600, cfg) and in_entry_window(121, cfg)
    assert not in_entry_window(100, cfg)


def test_next_close():
    assert next_close(1_000_000 * 900 + 10) == (1_000_000 + 1) * 900          # current window still open
    assert next_close(1_000_000 * 900 + 800) == (1_000_000 + 2) * 900         # too late: next one


def test_ticker_uses_new_york_close_time():
    assert ticker_for(1794254400) == "KXBTC15M-26NOV091500-00"   # 12:00 PT = 15:00 ET
    assert ticker_for(1794255300) == "KXBTC15M-26NOV091515-15"


class DownClient:
    """Kalshi is unreachable (rate limited) the whole window."""
    def market(self, ticker):
        raise RuntimeError("HTTP 429")

    def events(self, *a, **k):
        raise RuntimeError("HTTP 429")


class Spot:
    s = None

    def price(self, now):
        return 75_000.0


def test_unreachable_window_is_skipped_not_stuck(monkeypatch):
    import btcbot.bet as bet
    close = 1794254400
    clock = [close - 300.0]
    monkeypatch.setattr(bet.time, "time", lambda: clock[0])
    monkeypatch.setattr(bet.time, "sleep", lambda s: clock.__setitem__(0, clock[0] + max(s, 1)))
    monkeypatch.setattr(bet, "coinbase_candles", lambda *a, **k: [(0, 0, 0, 0, 75_000.0 + i) for i in range(60)])
    lines = []
    r = run_window(Settings(), 100, journal=None, close_ts=close, client=DownClient(), spot=Spot(), printer=lines.append)
    assert r == {"bet": None, "missed": True}
    assert any("MISSED" in x for x in lines)
