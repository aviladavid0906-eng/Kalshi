from btcbot.bet import best_bet, in_entry_window, max_contracts, next_close, walk_cost
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
