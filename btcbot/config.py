"""Settings, loaded from environment variables / a .env file.

Every knob has a conservative default. Nothing here turns on real-money
trading by itself: live mode also needs LIVE_CONFIRM and a passing paper
record (see bot.live_gate).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, fields
from pathlib import Path

PROD_URL = "https://api.elections.kalshi.com/trade-api/v2"
DEMO_URL = "https://demo-api.kalshi.co/trade-api/v2"
LIVE_CONFIRM_PHRASE = "I-UNDERSTAND-I-CAN-LOSE-THIS-MONEY"

# Environment variable names that differ from the upper-cased field name.
ENV_ALIASES = {
    "api_key_id": "KALSHI_API_KEY_ID",
    "private_key_path": "KALSHI_PRIVATE_KEY_PATH",
}


def load_dotenv(path: str | os.PathLike = ".env") -> None:
    """Minimal .env reader (KEY=VALUE lines). Real env vars win over the file."""
    p = Path(path)
    if not p.is_file():
        return
    for raw in p.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        val = val.split(" #", 1)[0].strip().strip('"').strip("'")
        os.environ.setdefault(key.strip(), val)


@dataclass
class Settings:
    # paper = fake money on real prices; demo = Kalshi demo exchange; live = real money
    mode: str = "paper"
    api_key_id: str = ""
    private_key_path: str = "kalshi_private_key.pem"

    # Which Kalshi bitcoin series to watch.
    #   KXBTC15M = "BTC up or down in the next 15 min"
    #   KXBTCD   = "BTC above/below $X at <hour>"
    #   KXBTC    = "BTC price range at <hour>"
    series: tuple[str, ...] = ("KXBTC15M", "KXBTCD")

    # --- money limits (dollars) -------------------------------------------
    bankroll: float = 100.0              # paper starting balance / sizing base
    max_trade_dollars: float = 5.0       # most a single trade can cost
    max_open_dollars: float = 15.0       # most that can be at risk at once
    max_daily_loss_dollars: float = 10.0 # stop opening trades for the day
    max_consecutive_losses: int = 5      # pause for the day after this streak

    # --- entry filters ------------------------------------------------------
    min_edge: float = 0.04               # $/contract after fees, worst-case vol
    kelly_fraction: float = 0.10         # fraction of full Kelly to bet
    min_price: float = 0.10              # skip lottery tickets / near-certainties
    max_price: float = 0.90
    max_spread: float = 0.10             # skip illiquid books
    book_markets: int = 8                # markets priced from the live order book each pass
    min_seconds_to_close: int = 90
    max_seconds_to_close: int = 4 * 3600

    # --- exits --------------------------------------------------------------
    stop_loss_pct: float = 0.50          # sell if the bid falls this far below entry (0 = off)
    exit_style: str = "target"           # target = fixed take-profit set at entry; dynamic = follow fair value
    edge_exit: bool = True               # dynamic style only: sell when the bid beats current fair value

    # --- model --------------------------------------------------------------
    poll_seconds: float = 10.0
    vol_lookback_min: int = 180
    vol_halflife_min: float = 30.0
    vol_mults: tuple[float, ...] = (0.75, 1.0, 1.35)
    vol_weights: tuple[float, ...] = (0.25, 0.5, 0.25)
    spot_basis_usd: float = 15.0         # uncertainty between our BTC feed and CF Benchmarks BRTI
    taker_fee_rate: float = 0.07

    # --- plumbing -------------------------------------------------------------
    db_path: str = "btcbot.db"
    ntfy_topic: str = ""                 # phone alerts via the free ntfy app
    hourly_wage: float = 16.90           # for "this cost N hours of work"
    live_confirm: str = ""
    skip_paper_gate: bool = False
    min_paper_trades: int = 50

    @property
    def base_url(self) -> str:
        return DEMO_URL if self.mode == "demo" else PROD_URL

    @classmethod
    def from_env(cls, env_file: str | os.PathLike | None = ".env") -> "Settings":
        if env_file:
            load_dotenv(env_file)
        s = cls()
        for f in fields(cls):
            raw = os.environ.get(ENV_ALIASES.get(f.name, f.name.upper()))
            if raw is None or raw.strip() == "":
                continue
            setattr(s, f.name, _coerce(getattr(s, f.name), raw))
        s.mode = s.mode.lower()
        s.exit_style = s.exit_style.lower()
        s.series = tuple(x.upper() for x in s.series)
        s.validate()
        return s

    def validate(self) -> None:
        problems = []
        if self.mode not in ("paper", "demo", "live"):
            problems.append(f"MODE must be paper, demo or live (got {self.mode!r})")
        if not 0 < self.min_price < self.max_price < 1:
            problems.append("need 0 < MIN_PRICE < MAX_PRICE < 1")
        if len(self.vol_mults) != len(self.vol_weights) or abs(sum(self.vol_weights) - 1) > 1e-6:
            problems.append("VOL_MULTS and VOL_WEIGHTS must be the same length and weights must sum to 1")
        for name in ("bankroll", "max_trade_dollars", "max_open_dollars", "max_daily_loss_dollars"):
            if getattr(self, name) <= 0:
                problems.append(f"{name.upper()} must be > 0")
        if self.max_trade_dollars > self.max_open_dollars:
            problems.append("MAX_TRADE_DOLLARS cannot exceed MAX_OPEN_DOLLARS")
        if self.min_edge < 0:
            problems.append("MIN_EDGE must be >= 0 (a negative edge means a losing bet by the bot's own math)")
        if self.exit_style not in ("target", "dynamic"):
            problems.append("EXIT_STYLE must be target or dynamic")
        if not 0 <= self.stop_loss_pct < 1:
            problems.append("STOP_LOSS_PCT must be in [0, 1)")
        if problems:
            raise ValueError("Bad settings:\n  - " + "\n  - ".join(problems))


def _coerce(current, raw: str):
    raw = raw.strip()
    if isinstance(current, bool):  # before int: bool is an int subclass
        return raw.lower() in ("1", "true", "yes", "on")
    if isinstance(current, int):
        return int(float(raw))
    if isinstance(current, float):
        return float(raw)
    if isinstance(current, tuple):
        parts = [x.strip() for x in raw.split(",") if x.strip()]
        if current and isinstance(current[0], float):
            return tuple(float(x) for x in parts)
        return tuple(parts)
    return raw
