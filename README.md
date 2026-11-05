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
- **Every call is a trade plan**: entry, take-profit and stop, plus a size for your balance.
  - *Entry:* buy at or below this price. Above it, the edge is gone after fees.
  - *Take profit:* sell at +50% over what you paid, for both UP and DOWN bets (`TP_UP_PCT`, `TP_DOWN_PCT`). Rest a limit sell there right after you buy. If the target would be $1 or more, hold to settlement. Set a % to 0 to use the bot's fair-value target instead.
  - *Stop:* `STOP_LOSS_PCT` below entry (default 50%). Kalshi has no stop orders, so `run` alerts you to sell by hand. `STOP_LOSS_PCT=0` turns it off.
  - If neither is hit, the contract settles at $1 or $0.
  - The paper bot follows the same fixed plan (`EXIT_STYLE=target`), so paper results match following the alerts. `EXIT_STYLE=dynamic` instead sells whenever the bid beats the bot's *current* fair value; you can't copy that by hand.
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
`CALL DOWN: BUY DOWN (NO) 2x on KXBTC15M-26NOV051530-30 [Target Price: $75,971.55] | entry: buy at or below $0.35 (ask now $0.310) | take profit: sell at $0.60 | stop: $0.15 | bot fair $0.565, edge +4.6c after fees | closes in 3 min`

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
Run on 2026-11-05 over the previous 3 days: 1,307 real settled markets, default entry rules ($100 bankroll, $5 max per trade, 4¢ minimum edge). These are the same 123 entries under each exit rule:

| Exit rule | P&L | Per $1 bet | t |
|---|---|---|---|
| **+50% take-profit + stop** (default) | −$19.49 | −19¢ | −2.9 |
| +50% take-profit, no stop | −$29.33 | −29¢ | −3.6 |
| UP +60% / DOWN +40% take-profit + stop | −$17.27 | −17¢ | −2.7 |
| UP +60% / DOWN +40%, no stop | −$23.49 | −23¢ | −3.0 |
| Fair-value take-profit + stop (`TP_UP_PCT=0 TP_DOWN_PCT=0`) | −$16.07 | −16¢ | −2.5 |
| Bot manages exits + stop (`EXIT_STYLE=dynamic`) | −$9.52 | −10¢ | −0.8 |
| Hold to settlement | −$15.02 | −15¢ | −1.0 |

- **Every exit rule loses.** A t of −2.5 or lower means the loss is very unlikely to be bad luck.
- **The take-profit level barely matters.** Every level from +20% to +200% lost 15–19¢ per $1. The entries are the problem, and an exit rule can't fix a bad entry.
- **15-minute markets lost far less than hourly ones:** −6¢ vs −24¢ per $1 under the default rule. They still lost.
- **Kalshi's prices predicted outcomes better than the bot** (Brier 0.1329 vs 0.1364 over 35,203 one-minute snapshots).
- **Bigger minimum edges looked better but prove nothing:** 8¢ gave +2.8% on 23 trades and 12¢ gave +27% on 8 trades. Both are too few to trust.
- **Verdict: NO EDGE. Don't fund it.**

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
