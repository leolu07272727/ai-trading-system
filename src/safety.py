"""Shared paper/live safety lock used by cycle.py, bot.py, trade.py, portfolio_snapshot.py.

force_close.py intentionally has its own stricter variant (paper-only, no live override)
since it auto-runs unattended via cron — see force_close.py:safety_lock.
"""
import os
import sys


def safety_lock() -> None:
    """Refuse to run if the paper/live flag disagrees with the gateway port."""
    paper = os.environ.get("PAPER_TRADING", "").strip().lower()
    port = int(os.environ["IBKR_PORT"])
    if paper == "true" and port != 4002:
        sys.exit("ABORT: PAPER_TRADING=true but port is not 4002")
    if paper == "false" and port == 4002:
        sys.exit("ABORT: PAPER_TRADING=false but port is 4002 (paper)")
