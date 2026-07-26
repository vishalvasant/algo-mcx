"""Which in-app notifications are pushed to external channels (Telegram)."""

from __future__ import annotations

IMPORTANT_TITLES = frozenset(
    {
        "Trade entry",
        "Trade exit",
        "BUY filled",
        "SELL filled",
        "Paper entry filled",
        "Paper exit",
        "Entry blocked by risk",
        "Auto trading ON",
        "Auto trading OFF",
        "Kill switch updated",
        "Flattrade login required",
        "Flattrade API key missing",
        "Paper account reset",
        "Weekly expiry rolled",
        "Paper trading enabled",
        "LIVE trading enabled",
    }
)


def is_important_alert(type_: str, title: str) -> bool:
    if type_ in ("trade", "kill_switch"):
        return True
    return title in IMPORTANT_TITLES
