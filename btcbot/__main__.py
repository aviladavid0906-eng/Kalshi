"""Command line: python -m btcbot {scan,run,report,backtest,check}"""
from __future__ import annotations

import argparse
import sys
import time
from dataclasses import replace
from datetime import datetime

from .config import Settings

SHORT = {"KXBTC15M": "15m", "KXBTCD": "hourly", "KXBTC": "range"}


def _dur(sec: float) -> str:
    m = int(max(sec, 0) // 60)
    return f"{m // 60}h{m % 60:02d}m" if m >= 60 else f"{m}m{int(max(sec, 0) % 60):02d}s"


def cmd_scan(cfg: Settings, args) -> None:
    from .bot import Bot

    bot = Bot(replace(cfg, mode="paper"))  # scanning never trades and needs no keys
    spot, sigma, rows = bot.scan(rows_limit=args.rows)
    from .model import annualized

    print(f"BTC ${spot:,.2f} | volatility {annualized(sigma) * 100:.0f}%/yr | {datetime.now():%Y-%m-%d %H:%M} | "
          f"min edge {cfg.min_edge * 100:.0f}c")
    shown = [r for r in rows if r["signals"]] + [r for r in rows if not r["signals"]]
    if not shown:
        print("No open BTC markets in the trading window right now.")
        return
    print(f"{'MARKET':<44} {'CLOSES':>7} {'BID':>6} {'ASK':>6} {'BOT':>6}  CALL")
    for r in shown:
        q = r["quote"]
        name = f"{SHORT.get(q.series, q.series)} {datetime.fromtimestamp(q.close_ts):%H:%M} {q.label}"[:44]
        if r["signals"]:
            s = max(r["signals"], key=lambda x: x.edge)
            call = f"BUY {s.direction} ({s.side.upper()}) @ {s.price:.3f}  edge +{s.edge * 100:.1f}c"
        else:
            call = f"pass (best edge {r['best_raw'] * 100:+.1f}c)"
        print(f"{name:<44} {_dur(r['t_rem']):>7} {q.yes_bid:>6.3f} {q.yes_ask:>6.3f} {r['fair']:>6.3f}  {call}")
    print("BOT = bot's fair YES price. Edge = worst-case win chance minus price minus fees, per $1 contract.")


def cmd_run(cfg: Settings, args) -> None:
    from .bot import Bot

    try:
        Bot(cfg).run(max_steps=1 if args.once else None)
    except KeyboardInterrupt:
        print("\nStopped.")


def cmd_report(cfg: Settings, args) -> None:
    from .journal import Journal
    from .report import render_report

    print(render_report(Journal(cfg.db_path), cfg, args.mode or cfg.mode, time.time()))


def cmd_backtest(cfg: Settings, args) -> None:
    from .backtest import run_backtest

    series = tuple(s.strip().upper() for s in args.series.split(",") if s.strip())
    run_backtest(cfg, series=series, days=args.days, lag_min=args.lag, near_pct=args.near)


def cmd_check(cfg: Settings, args) -> None:
    from .kalshi import Kalshi

    if cfg.mode == "paper":
        print("MODE=paper doesn't use an account. Set MODE=demo (or live) to test your API key.")
        return
    client = Kalshi(cfg.base_url, cfg.api_key_id, cfg.private_key_path)
    print(f"Connected to {cfg.base_url}")
    print(f"Balance: ${client.balance():,.2f}")
    pos = [p for p in client.positions() if abs(float(p.get('position_fp') or 0)) > 0]
    print(f"Open positions: {len(pos)}")
    for p in pos[:20]:
        print(f"  {p.get('ticker')}: {p.get('position_fp')}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="btcbot", description="Kalshi bitcoin fair-value bot (paper trading by default).")
    ap.add_argument("--env", default=".env", help="settings file (default .env)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("scan", help="show live BTC markets with the bot's fair price and UP/DOWN calls (never trades)")
    p.add_argument("--rows", type=int, default=12)
    p = sub.add_parser("run", help="run the bot (MODE from .env; paper unless you change it)")
    p.add_argument("--once", action="store_true", help="single pass, then exit")
    p = sub.add_parser("report", help="P&L by day/week/month and the go/no-go verdict")
    p.add_argument("--mode", choices=["paper", "demo", "live"])
    p = sub.add_parser("backtest", help="replay recent settled markets through the bot's rules")
    p.add_argument("--days", type=float, default=3.0)
    p.add_argument("--series", default="KXBTC15M,KXBTCD")
    p.add_argument("--lag", type=int, default=0, help="minutes of delay on BTC price (stress test)")
    p.add_argument("--near", type=float, default=0.01, help="only strikes within this fraction of BTC (0.01 = 1%%)")
    sub.add_parser("check", help="test your Kalshi API key (demo/live)")
    args = ap.parse_args(argv)
    try:
        cfg = Settings.from_env(args.env)
    except ValueError as e:
        print(e, file=sys.stderr)
        return 2
    {"scan": cmd_scan, "run": cmd_run, "report": cmd_report, "backtest": cmd_backtest, "check": cmd_check}[args.cmd](cfg, args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
