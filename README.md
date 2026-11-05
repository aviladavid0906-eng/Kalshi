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
- **Exits**
  - *Take profit / model exit:* sell when the bid pays more than the bot's fair value.
  - *Stop loss:* sell if the bid falls `STOP_LOSS_PCT` (default 50%) below entry. On binary contracts this often sells near the bottom. `STOP_LOSS_PCT=0` holds to settlement.
  - Otherwise hold until it settles at $1 or $0.
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
Run on 2026-11-05 over the previous 3 days: 1,307 real settled markets, default settings ($5 max per trade, 4¢ minimum edge).

| Slice | Trades | Win rate vs price paid | P&L | ROI |
|---|---|---|---|---|
| Default (with exits) | 120 | 28% vs 31% | −$9.20 | −9.5% |
| Hold to settlement | 120 | 28% vs 31% | −$14.25 | −14.7% |
| 15-min markets | 39 | 41% vs 40% | −$5.06 | −13.8% |
| Hourly markets | 81 | 22% vs 26% | −$4.14 | −6.8% |

- **Kalshi's prices predicted outcomes better than the bot** across 35,195 one-minute snapshots (Brier 0.1343 vs 0.1376, lower is better).
- **Bigger "edges" didn't reliably win more.** The minimum edge setting gave: 2¢ −0.8%, 4¢ −9.5%, 8¢ −6.4%, 12¢ +55%. The 12¢ result is only 8 trades (t = 1.0), which is within the range of luck. Don't tune the bot to it.
- **The BTC price feed isn't the problem.** Coinbase vs. the official settlement index differed by +$3.6 ± $9.2 at settlement.
- **Verdict: NO EDGE. Don't fund it.** The cheap contracts it buys win slightly *less* often than their price implies, so the market already knows what the model misses.

## Files
- `btcbot/model.py` - pricing math (settlement averaging, volatility, fees, Kelly)
- `btcbot/strategy.py` - UP/DOWN signals and exit rules
- `btcbot/risk.py` - sizing and hard limits
- `btcbot/broker.py` - paper fills against the real order book; live v2 orders
- `btcbot/kalshi.py` - Kalshi API client (RSA-PSS signed)
- `btcbot/backtest.py`, `btcbot/report.py` - the honesty checks
- `tests/` - `python -m pytest`
