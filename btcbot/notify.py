"""Console messages, plus optional phone push through ntfy (free app, no account)."""
from __future__ import annotations

from datetime import datetime
from urllib.parse import quote

import requests


class Notifier:
    def __init__(self, cfg, session: requests.Session | None = None, printer=print):
        self.topic = cfg.ntfy_topic
        self.s = session or requests.Session()
        self.printer = printer

    def __call__(self, title: str, msg: str, push: bool = True) -> None:
        self.printer(f"[{datetime.now():%H:%M:%S}] {title}: {msg}")
        if push and self.topic:
            try:
                self.s.post(f"https://ntfy.sh/{quote(self.topic)}", data=msg.encode("utf-8"),
                            headers={"Title": title.encode("ascii", "ignore").decode() or "btcbot"}, timeout=5)
            except Exception:
                pass  # alerts are best-effort; never crash the bot over one
