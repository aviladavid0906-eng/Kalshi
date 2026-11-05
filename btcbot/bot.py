"""The trading loop: price -> fair value -> exits -> entries -> settlement."""
from __future__ import annotations

import time
from datetime import datetime

from .broker import LiveBroker, PaperBroker
from .config import LIVE_CONFIRM_PHRASE, PROD_URL, Settings
from .journal import Journal
from .kalshi import Kalshi
from .model import annualized, ewma_sigma, taker_fee
from .notify import Notifier
from .report import trade_stats
from .risk import RiskManager
from .spot import SpotFeed, coinbase_candles
from .strategy import Quote, evaluate, exit_decision, iso_to_ts, parse_quote


def live_gate(cfg: Settings, journal: Journal) -> str | None:
    """Reasons live trading stays locked, or None when it may start."""
    if cfg.live_confirm != LIVE_CONFIRM_PHRASE:
        return f"set LIVE_CONFIRM={LIVE_CONFIRM_PHRASE} in .env"
    if cfg.skip_paper_gate:
        return None
    s = trade_stats(journal.closed_positions("paper"))
    if s.n < cfg.min_paper_trades:
        return f"paper record has {s.n} closed trades; need {cfg.min_paper_trades} first"
    if s.pnl <= 0 or s.t < 2.0:
        return (f"paper record doesn't prove an edge yet (P&L ${s.pnl:.2f}, t = {s.t:.1f}, need > $0 and t >= 2). "
                "SKIP_PAPER_GATE=1 overrides this - not recommended")
    return None


class Scanner:
    """Finds open BTC markets closing inside the trading window."""

    def __init__(self, client, cfg: Settings):
        self.client, self.cfg = client, cfg
        self._events: list[tuple[str, str]] = []
        self._events_ts = float("-inf")

    def events(self, now: float) -> list[tuple[str, str]]:
        if now - self._events_ts > 60:
            found = []
            for series in self.cfg.series:
                for e in self.client.events(series, status="open"):
                    close = iso_to_ts(e.get("strike_date"))
                    if close and not (now < close <= now + self.cfg.max_seconds_to_close + 120):
                        continue
                    found.append((series, e["event_ticker"]))
            self._events, self._events_ts = found, now
        return self._events

    def quotes(self, now: float) -> list[Quote]:
        out = []
        for series, event in self.events(now):
            for m in self.client.markets(event_ticker=event, status="open", max_pages=2):
                q = parse_quote(m, series)
                if q and now < q.close_ts <= now + self.cfg.max_seconds_to_close + 120:
                    out.append(q)
        return out


class Bot:
    def __init__(self, cfg: Settings, *, client=None, spot=None, journal=None, broker=None,
                 notifier=None, closes_fn=None):
        self.cfg, self.mode = cfg, cfg.mode
        if client is None:
            client = (Kalshi(PROD_URL) if self.mode == "paper"
                      else Kalshi(cfg.base_url, cfg.api_key_id, cfg.private_key_path))
        self.client = client
        self.spot = spot or SpotFeed()
        self.journal = journal or Journal(cfg.db_path)
        if self.mode == "live":
            problem = live_gate(cfg, self.journal)
            if problem:
                raise SystemExit("Live trading is locked: " + problem)
        self.broker = broker or (PaperBroker(client, self.journal, cfg) if self.mode == "paper"
                                 else LiveBroker(client, self.journal, cfg, self.mode))
        self.risk = RiskManager(cfg, self.journal, self.mode)
        self.scanner = Scanner(client, cfg)
        self.notify = notifier or Notifier(cfg)
        self.closes_fn = closes_fn or self._coinbase_closes
        self.sigma: float | None = None
        self._vol_ts = float("-inf")
        self._settle_ts = float("-inf")
        self._obs_ts: dict[str, float] = {}
        self._last_block = ""

    # --- inputs -----------------------------------------------------------------
    def _coinbase_closes(self, now: float) -> list[float]:
        candles = coinbase_candles(now - self.cfg.vol_lookback_min * 60, now, session=getattr(self.spot, "s", None))
        return [c[4] for c in candles]

    def refresh_vol(self, now: float) -> None:
        if self.sigma is not None and now - self._vol_ts < 60:
            return
        try:
            self.sigma = ewma_sigma(self.closes_fn(now), halflife=self.cfg.vol_halflife_min)
            self._vol_ts = now
        except Exception as e:
            if self.sigma is None:
                raise
            self.journal.log("warn", f"volatility refresh failed, reusing last value: {e}")

    def evaluate(self, q: Quote, spot: float, now: float):
        realized = self.spot.average_since(q.close_ts - 60) if q.close_ts - now < 60 else None
        return evaluate(q, spot, self.sigma, now, self.cfg, realized_avg=realized)

    # --- one loop iteration -----------------------------------------------------
    def step(self, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        spot = self.spot.price(now)
        self.refresh_vol(now)
        quotes = self.scanner.quotes(now)
        evals = {q.ticker: self.evaluate(q, spot, now) for q in quotes}
        self._log_observations(quotes, evals, spot, now)
        exits = self._manage_exits({q.ticker: q for q in quotes}, evals, now)
        entries = self._enter(quotes, evals, now)
        settled = self._settle(now)
        st = self.risk.status(now)
        return {"ts": now, "spot": spot, "vol": annualized(self.sigma), "markets": len(quotes),
                "entries": entries, "exits": exits, "settled": settled,
                "open": len(self.journal.open_positions(self.mode)), "open_cost": st["open_cost"],
                "today": st["realized_today"]}

    def _log_observations(self, quotes, evals, spot, now) -> None:
        for q in quotes:
            fy = evals[q.ticker][1]
            if fy is None or not (0 < q.yes_bid < q.yes_ask < 1) or q.spread > 0.10 or not 0.03 < q.mid < 0.97:
                continue
            if now - self._obs_ts.get(q.ticker, float("-inf")) < 60:
                continue
            self._obs_ts[q.ticker] = now
            self.journal.add_observation(ts=now, ticker=q.ticker, series=q.series, close_ts=q.close_ts,
                                         t_rem=q.close_ts - now, spot=spot, sigma=self.sigma, p_model=fy[0],
                                         yes_bid=q.yes_bid, yes_ask=q.yes_ask)
        if len(self._obs_ts) > 5000:
            live = {q.ticker for q in quotes}
            self._obs_ts = {k: v for k, v in self._obs_ts.items() if k in live}

    def _manage_exits(self, by_ticker, evals, now) -> int:
        n = 0
        for pos in self.journal.open_positions(self.mode):
            q = by_ticker.get(pos.ticker)
            if q is None or evals[pos.ticker][1] is None:
                continue
            fair = evals[pos.ticker][1][0]
            fair_side = fair if pos.side == "yes" else 1.0 - fair
            bid = q.bid(pos.side)
            reason = exit_decision(pos.entry_price, bid, fair_side, q.close_ts - now, pos.count, self.cfg)
            if not reason:
                continue
            pnl = self.broker.sell(pos, bid, reason, now)
            if pnl is not None:
                n += 1
                sold = self.journal.closed_positions(self.mode, since=now)[-1]
                self.notify(f"SOLD ({reason.replace('_', ' ')})",
                            f"{sold.count:g}x {pos.side.upper()} {pos.ticker} @ ${sold.exit_price:.3f} "
                            f"(bought @ ${pos.entry_price:.3f}) | P&L {pnl:+.2f} [{self.mode}]")
        return n

    def _enter(self, quotes, evals, now) -> int:
        signals = []
        for q in quotes:
            # One bet per market (no flipping sides after an exit), and one open bet per event:
            # strikes in the same event are the same bet on BTC.
            if self.journal.ever_traded(self.mode, q.ticker) or \
               self.journal.has_open(self.mode, event_ticker=q.event_ticker):
                continue
            signals.extend(evals[q.ticker][0])
        signals.sort(key=lambda s: s.edge, reverse=True)
        opened, used_events = 0, set()
        for sig in signals:
            if sig.quote.event_ticker in used_events:
                continue
            ok, why = self.risk.can_open(now)
            if not ok:
                if why != self._last_block:
                    self.notify("NO NEW TRADES", why)
                    self._last_block = why
                break
            self._last_block = ""
            n = self.risk.size(sig, cash=self.broker.cash(), equity=self.broker.equity(), now=now)
            if n < 1:
                continue
            sig.edge = sig.p_cons - sig.price - taker_fee(sig.price, n, self.cfg.taker_fee_rate) / n
            if sig.edge < self.cfg.min_edge:
                continue
            pid = self.broker.buy(sig, n, now)
            if pid is None:
                self.notify(f"SIGNAL {sig.direction} (not filled)", sig.text(n))
                continue
            pos = self.journal.get(pid)
            self.notify(f"BOUGHT {sig.direction}",
                        f"{sig.text(int(pos.count))} | filled @ ${pos.entry_price:.3f}, "
                        f"cost ${pos.cost:.2f} incl. fees [{self.mode}]")
            used_events.add(sig.quote.event_ticker)
            opened += 1
            if opened >= 2:
                break
        return opened

    def _settle(self, now) -> int:
        if now - self._settle_ts < 30:
            return 0
        self._settle_ts = now
        n = 0
        for pos in self.journal.open_positions(self.mode):
            if now < (pos.close_ts or 0) + 10:
                continue
            try:
                m = self.client.market(pos.ticker)
            except Exception:
                continue
            result = (m.get("result") or "").lower()
            if result in ("yes", "no"):
                pnl = self.journal.settle(pos.id, result, ts=now)
                n += 1
                self.notify("SETTLED " + ("WIN" if pnl > 0 else "LOSS"),
                            f"{pos.ticker} -> {result.upper()} | P&L {pnl:+.2f} [{self.mode}]")
        for ticker in self.journal.pending_observation_tickers(now - 10, limit=10):
            try:
                m = self.client.market(ticker)
            except Exception:
                continue
            result = (m.get("result") or "").lower()
            if result in ("yes", "no"):
                self.journal.set_observation_result(ticker, result)
            elif m.get("status") in ("settled", "finalized"):
                self.journal.set_observation_result(ticker, result or "void")
        return n

    # --- scan (no trading) and the forever loop -------------------------------------
    def _fee_pc(self, price: float) -> float:
        n = max(1, int(self.cfg.max_trade_dollars // price))
        return taker_fee(price, n, self.cfg.taker_fee_rate) / n

    def scan(self, now: float | None = None):
        now = time.time() if now is None else now
        spot = self.spot.price(now)
        self.refresh_vol(now)
        rows = []
        for q in self.scanner.quotes(now):
            sigs, fy = self.evaluate(q, spot, now)
            if fy is None or not (0 < q.yes_bid < q.yes_ask < 1):
                continue
            mix, lo, hi = fy
            best_raw = max(lo - q.yes_ask - self._fee_pc(q.yes_ask), (1 - hi) - q.no_ask - self._fee_pc(q.no_ask))
            rows.append({"quote": q, "fair": mix, "signals": sigs, "t_rem": q.close_ts - now, "best_raw": best_raw})
        rows.sort(key=lambda r: abs(r["fair"] - 0.5))
        return spot, self.sigma, rows

    def run(self, max_steps: int | None = None, printer=print) -> None:
        c = self.cfg
        printer(f"btcbot | MODE: {self.mode.upper()}"
                + {"paper": " (fake money, real Kalshi prices)", "demo": " (Kalshi demo exchange)",
                   "live": " (REAL MONEY)"}[self.mode])
        printer(f"Watching {', '.join(c.series)} | limits: ${c.max_trade_dollars:g}/trade, ${c.max_open_dollars:g} open, "
                f"${c.max_daily_loss_dollars:g}/day loss, pause after {c.max_consecutive_losses} straight losses")
        printer("Stop: Ctrl+C, or create a file named STOP in this folder.")
        steps = errors = 0
        while True:
            t0 = time.time()
            try:
                s = self.step()
                errors = 0
                if steps % 6 == 0 or s["entries"] or s["exits"] or s["settled"]:
                    printer(f"[{datetime.now():%H:%M:%S}] BTC ${s['spot']:,.0f} | vol {s['vol'] * 100:.0f}%/yr | "
                            f"{s['markets']} markets | open {s['open']} (${s['open_cost']:.2f}) | today {s['today']:+.2f}")
            except KeyboardInterrupt:
                raise
            except Exception as e:
                errors += 1
                self.journal.log("error", repr(e))
                printer(f"[{datetime.now():%H:%M:%S}] error #{errors}: {e}")
                time.sleep(min(60, 5 * errors))
            steps += 1
            if max_steps is not None and steps >= max_steps:
                return
            time.sleep(max(0.0, c.poll_seconds - (time.time() - t0)))
