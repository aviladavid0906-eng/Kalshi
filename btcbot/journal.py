"""SQLite record of every trade and of the bot's predictions.

`positions` holds paper, demo and live trades (column `mode`), so results
from each stay separate. `observations` stores the bot's fair price next
to Kalshi's price once a minute per market; after settlement this shows
whether the bot predicts better than the market.
"""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass

SCHEMA = """
CREATE TABLE IF NOT EXISTS positions(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  mode TEXT NOT NULL, ticker TEXT NOT NULL, series TEXT, event_ticker TEXT,
  side TEXT NOT NULL, count REAL NOT NULL,
  entry_price REAL NOT NULL, entry_fee REAL NOT NULL, entry_ts REAL NOT NULL,
  close_ts REAL, p_win REAL, edge REAL, label TEXT, order_id TEXT,
  status TEXT NOT NULL DEFAULT 'open',
  exit_price REAL, exit_fee REAL, exit_ts REAL, exit_reason TEXT, pnl REAL,
  take_profit REAL, stop_price REAL);
CREATE INDEX IF NOT EXISTS positions_mode_status ON positions(mode, status);
CREATE TABLE IF NOT EXISTS observations(
  ts REAL NOT NULL, ticker TEXT NOT NULL, series TEXT, close_ts REAL, t_rem REAL,
  spot REAL, sigma REAL, p_model REAL, yes_bid REAL, yes_ask REAL, result TEXT,
  PRIMARY KEY (ticker, ts));
CREATE TABLE IF NOT EXISTS log(ts REAL NOT NULL, kind TEXT, msg TEXT);
"""


@dataclass
class Position:
    id: int
    mode: str
    ticker: str
    series: str
    event_ticker: str
    side: str
    count: float
    entry_price: float
    entry_fee: float
    entry_ts: float
    close_ts: float
    p_win: float
    edge: float
    label: str
    order_id: str | None
    status: str
    exit_price: float | None
    exit_fee: float | None
    exit_ts: float | None
    exit_reason: str | None
    pnl: float | None
    take_profit: float | None = None
    stop_price: float | None = None

    @property
    def cost(self) -> float:
        return self.count * self.entry_price + self.entry_fee


class Journal:
    def __init__(self, path: str = "btcbot.db"):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(positions)")}
        for col in ("take_profit", "stop_price"):  # databases made by older versions
            if col not in cols:
                self.db.execute(f"ALTER TABLE positions ADD COLUMN {col} REAL")
        self.db.commit()

    def _pos(self, row: sqlite3.Row) -> Position:
        return Position(**{k: row[k] for k in row.keys()})

    # --- trades ----------------------------------------------------------------
    def open_position(self, *, mode, ticker, series, event_ticker, side, count, entry_price, entry_fee,
                      close_ts, p_win, edge, label="", order_id=None, ts=None,
                      take_profit=None, stop_price=None) -> int:
        cur = self.db.execute(
            "INSERT INTO positions(mode,ticker,series,event_ticker,side,count,entry_price,entry_fee,entry_ts,"
            "close_ts,p_win,edge,label,order_id,take_profit,stop_price) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (mode, ticker, series, event_ticker, side, count, entry_price, entry_fee,
             ts if ts is not None else time.time(), close_ts, p_win, edge, label, order_id,
             take_profit, stop_price))
        self.db.commit()
        return cur.lastrowid

    def get(self, pid: int) -> Position:
        return self._pos(self.db.execute("SELECT * FROM positions WHERE id=?", (pid,)).fetchone())

    def close_position(self, pid: int, *, exit_price: float, exit_fee: float, reason: str,
                       ts: float | None = None, count: float | None = None) -> float:
        """Close a position (or `count` contracts of it). Returns realized P&L."""
        pos = self.get(pid)
        if count is not None and count < pos.count:
            frac = count / pos.count
            fee_part = pos.entry_fee * frac
            self.db.execute("UPDATE positions SET count=?, entry_fee=? WHERE id=?",
                            (pos.count - count, pos.entry_fee - fee_part, pid))
            pid = self.open_position(mode=pos.mode, ticker=pos.ticker, series=pos.series,
                                     event_ticker=pos.event_ticker, side=pos.side, count=count,
                                     entry_price=pos.entry_price, entry_fee=fee_part, close_ts=pos.close_ts,
                                     p_win=pos.p_win, edge=pos.edge, label=pos.label,
                                     order_id=pos.order_id, ts=pos.entry_ts,
                                     take_profit=pos.take_profit, stop_price=pos.stop_price)
            pos = self.get(pid)
        pnl = pos.count * exit_price - exit_fee - pos.count * pos.entry_price - pos.entry_fee
        self.db.execute(
            "UPDATE positions SET status='closed', exit_price=?, exit_fee=?, exit_ts=?, exit_reason=?, pnl=? "
            "WHERE id=?", (exit_price, exit_fee, ts if ts is not None else time.time(), reason, pnl, pid))
        self.db.commit()
        return pnl

    def settle(self, pid: int, result: str, ts: float | None = None) -> float:
        pos = self.get(pid)
        won = result.lower() == pos.side
        return self.close_position(pid, exit_price=1.0 if won else 0.0, exit_fee=0.0,
                                   reason="settled_win" if won else "settled_loss", ts=ts)

    def open_positions(self, mode: str) -> list[Position]:
        rows = self.db.execute("SELECT * FROM positions WHERE mode=? AND status='open' ORDER BY id", (mode,))
        return [self._pos(r) for r in rows]

    def closed_positions(self, mode: str, since: float = 0.0) -> list[Position]:
        rows = self.db.execute("SELECT * FROM positions WHERE mode=? AND status='closed' AND exit_ts>=? "
                               "ORDER BY exit_ts", (mode, since))
        return [self._pos(r) for r in rows]

    def has_open(self, mode: str, *, ticker: str | None = None, event_ticker: str | None = None) -> bool:
        if ticker:
            q, arg = "ticker=?", ticker
        else:
            q, arg = "event_ticker=?", event_ticker
        row = self.db.execute(f"SELECT 1 FROM positions WHERE mode=? AND status='open' AND {q} LIMIT 1",
                              (mode, arg)).fetchone()
        return row is not None

    def ever_traded(self, mode: str, ticker: str) -> bool:
        row = self.db.execute("SELECT 1 FROM positions WHERE mode=? AND ticker=? LIMIT 1", (mode, ticker)).fetchone()
        return row is not None

    def open_cost(self, mode: str) -> float:
        row = self.db.execute("SELECT COALESCE(SUM(count*entry_price+entry_fee),0) FROM positions "
                              "WHERE mode=? AND status='open'", (mode,)).fetchone()
        return float(row[0])

    def realized_pnl(self, mode: str, since: float = 0.0) -> float:
        row = self.db.execute("SELECT COALESCE(SUM(pnl),0) FROM positions WHERE mode=? AND status='closed' "
                              "AND exit_ts>=?", (mode, since)).fetchone()
        return float(row[0])

    def consecutive_losses(self, mode: str, since: float = 0.0) -> int:
        streak = 0
        for pos in reversed(self.closed_positions(mode, since)):
            if (pos.pnl or 0.0) < 0:
                streak += 1
            else:
                break
        return streak

    # --- predictions ---------------------------------------------------------------
    def add_observation(self, *, ts, ticker, series, close_ts, t_rem, spot, sigma, p_model, yes_bid, yes_ask):
        self.db.execute("INSERT OR IGNORE INTO observations(ts,ticker,series,close_ts,t_rem,spot,sigma,p_model,"
                        "yes_bid,yes_ask) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (ts, ticker, series, close_ts, t_rem, spot, sigma, p_model, yes_bid, yes_ask))
        self.db.commit()

    def pending_observation_tickers(self, before_ts: float, limit: int = 25) -> list[str]:
        rows = self.db.execute("SELECT DISTINCT ticker FROM observations WHERE result IS NULL AND close_ts<? "
                               "LIMIT ?", (before_ts, limit))
        return [r[0] for r in rows]

    def set_observation_result(self, ticker: str, result: str) -> None:
        self.db.execute("UPDATE observations SET result=? WHERE ticker=?", (result, ticker))
        self.db.commit()

    def resolved_observations(self, since: float = 0.0) -> list[sqlite3.Row]:
        return list(self.db.execute("SELECT * FROM observations WHERE result IN ('yes','no') AND ts>=?", (since,)))

    def log(self, kind: str, msg: str) -> None:
        self.db.execute("INSERT INTO log(ts,kind,msg) VALUES(?,?,?)", (time.time(), kind, msg))
        self.db.commit()
