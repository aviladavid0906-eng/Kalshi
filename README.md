# btcbot: Kalshi bitcoin bot

Works out what each Kalshi bitcoin contract is really worth, calls **UP** or **DOWN** when Kalshi's price is off by more than fees, and trades it with hard loss limits. **It paper-trades by default** (fake money, real prices).

## Read this first
- **No bot can guarantee profit.** Kalshi's 15-minute BTC markets trade millions of contracts per window against professional market makers with faster data than you have.
- The bot is built to **prove or disprove an edge before you risk money.** Live trading stays locked until your paper record passes a statistical test.
- **Backtest on Kalshi's real settled markets:** see [Backtest results](#backtest-results). Run it yourself any time with `python -m btcbot backtest`.

## How it decides
- **Fair value, not a hunch.** Every contract settles on the 60-second average of CF Benchmarks' BRTI before close. The bot takes the BTC price (Coinbase + Kraken), the time left and recent volatility, and computes the chance that average lands on the YES side.
- **Only bets on clear mispricings.** Edge = worst-case win chance (under both low- and high-volatility assumptions) − price − Kalshi fee. It must be ≥ `MIN_EDGE` (default 4¢ per $1 contract).
- **UP** = YES on "above $X" / "up in 15 min"; **DOWN** = NO on the same market.
- **Live prices only.** Every decision uses the live order book. Kalshi's market list can lag the book by 10+ seconds, so the bot never trades on it.
- **Every call is a trade plan:** an entry price, an exit plan and a size for your balance.
  - *Entry:* buy at or below this price. Above it, the edge is gone after fees.
  - *Exit (default `EXIT_STYLE=hold`):* hold until it settles at $1 or $0. That lost the least in both test periods.
  - *Other exit styles:*
    - `EXIT_STYLE=target`: a fixed take-profit (`TP_UP_PCT` / `TP_DOWN_PCT`, e.g. 0.50 = +50% over what you paid).
    - `EXIT_STYLE=dynamic`: sell when the bid beats the bot's current fair value.
    - Either can add a stop with `STOP_LOSS_PCT` (e.g. 0.50). Kalshi has no stop orders, so `run` alerts you to sell by hand.
- **Watches the 15-minute markets only** (`SERIES=KXBTC15M`). The hourly markets lost more in testing; add `KXBTCD` to watch them anyway.
- **Limits that hold no matter what the model says**
  - Per trade (`MAX_TRADE_DOLLARS`, $5), total open (`MAX_OPEN_DOLLARS`, $15).
  - Daily loss (`MAX_DAILY_LOSS_DOLLARS`, $10). Open positions count as already lost, so the day can't go past the limit.
  - Pause for the day after `MAX_CONSECUTIVE_LOSSES` (5) losses in a row.
  - One bet per event: strikes in the same hour are the same bet on BTC.
  - Kill switch: create a file named `STOP` in the folder.

## Setup (about 10 minutes)
1. Install Python 3.10+, then `pip install -r requirements.txt`
2. `cp .env.example .env`. Defaults are safe; nothing needs an account in paper mode.
3. `python -m btcbot backtest --days 3`: how the rules did on the last 3 days of real markets.
4. `python -m btcbot scan`: the live board, showing each market's bid/ask, the bot's fair price and its UP/DOWN call.
5. `python -m btcbot run`: paper-trades continuously. The computer must stay on; a ~$5/mo cloud server also works.
6. `python -m btcbot report`: P&L for today, the week and the month, shown in hours of work, plus the go/no-go verdict.

**Example alert:**
`CALL DOWN: BUY DOWN (NO) 2x on KXBTC15M-26NOV051530-30 [Target Price: $75,971.55] | entry: buy at or below $0.35 (ask now $0.310) | take profit: none, hold to settlement | bot fair $0.565, edge +4.6c after fees | closes in 3 min`

Phone alerts: install the free **ntfy** app, subscribe to a long random topic name, and set `NTFY_TOPIC` to the same name in `.env`.

## Going live (only after the report says PASS)
- **What PASS means:** 50+ closed paper trades, profitable after fees, a t-stat of at least 2 (unlikely to be luck), and the bot's predictions scoring better than Kalshi's own prices.
- **Demo first:** make an account and API key at demo.kalshi.co, set `MODE=demo`, check the key with `python -m btcbot check`, then `run`.
- **Then live:** make an API key on kalshi.com and save the `.pem` file next to the bot. Set `MODE=live` and `LIVE_CONFIRM=I-UNDERSTAND-I-CAN-LOSE-THIS-MONEY`. Keep the limits small.
- Real orders are limit orders filled immediately or cancelled, so the bot never pays more than the signal price and never leaves orders sitting on the book.

## Commands
| Command | What it does |
|---|---|
| `python -m btcbot scan` | Live board with UP/DOWN calls (never trades) |
| `python -m btcbot run` | Run the bot in `MODE` from `.env` (`--once` for a single pass) |
| `python -m btcbot report` | P&L and verdict (`--mode paper/demo/live`) |
| `python -m btcbot backtest` | Replay settled markets (`--days`, `--series`, `--lag` delay stress test) |
| `python -m btcbot check` | Test your API key (demo/live) |

## Backtest results
Default entry rules ($100 bankroll, $5 max per trade, 4¢ minimum edge), 15-minute markets. Each period uses the same entries under every exit rule, after fees.

| Exit rule | Nov 2–5 (39 trades) | Oct 30–Nov 2, unseen during tuning (156 trades) |
|---|---|---|
| **Hold to settlement** (default) | −9¢ per $1 | −13¢ per $1 (−$21.97) |
| Bot manages exits, no stop | −6¢ | −16¢ (−$25.67) |
| Bot manages exits + 50% stop | −8¢ | −23¢ (−$38.44) |
| Take-profit +50% + 50% stop | −10¢ | −20¢ (−$32.47) |

- **Every rule lost in both periods.** The best rule in one period was among the worst in the other. Choosing a rule from one period's results is just fitting to noise.
- **Hourly markets were worse:** −10¢ to −25¢ per $1 over Nov 2–5 (84 trades), depending on the exit rule.
- **Kalshi's prices predicted outcomes better than the bot** (Brier 0.1329 vs 0.1364 over 35,203 one-minute snapshots).
- **Verdict: NO EDGE. Don't fund it.** Live trading stays locked until a paper record of 50+ trades is profitable with t ≥ 2.

## Other methods tested (2026-11-05, same 3 days)
- **Buying cheap contracts loses.** Hourly-market contracts priced 3–40¢, bought 15–60 minutes before close, lost 2–5¢ per $1 after fees (t as low as −3.0). The bot's average buy (31¢) sat right in this zone.
- **Buying likely winners roughly breaks even.** Contracts priced 60–97¢ made 0 to +1.5¢ per $1, which isn't statistically different from zero.
- **Arbitrage is gone.** Across strikes in the same hour, and between the 15-minute and hourly markets that settle on the same average: 0 and 1 opportunities (worth 0.5¢) in 7,354 one-minute snapshots.
- **Conclusion: no profitable method found.** The prices are efficient and fees take the rest.

## Files
- `btcbot/model.py` - pricing math (settlement averaging, volatility, fees, Kelly)
- `btcbot/strategy.py` - UP/DOWN signals and exit rules
- `btcbot/risk.py` - sizing and hard limits
- `btcbot/broker.py` - paper fills against the real order book; live v2 orders
- `btcbot/kalshi.py` - Kalshi API client (RSA-PSS signed)
- `btcbot/backtest.py`, `btcbot/report.py` - the honesty checks
- `tests/` - `python -m pytest`
