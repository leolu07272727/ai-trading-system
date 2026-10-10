#!/usr/bin/env python3
"""§7 second-layer EOD safety net.

Independent of cycle.py state. Does NOT read open_positions.json — it queries the
broker directly and flattens every open position with a market order. This is the
fire-and-forget net that fires even if cycle.py died. It must never crash.
"""
from __future__ import annotations

import os
import sys
import traceback
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from ib_async import MarketOrder, Stock

from src.ibkr_client import IBKRClient
from src.notify import notify, notify_error

PROJECT_ROOT = Path(__file__).resolve().parent
LOG_DIR = PROJECT_ROOT / "logs"
ERROR_LOG = LOG_DIR / "force_close_errors.log"
ET = ZoneInfo("America/New_York")

CLIENT_ID = 10
FILL_POLL_SECONDS = 30

# Cron fires this on a fixed-UTC wide window to survive the EDT/EST shift (§14
# DST caveat) — this gate is what actually decides whether "now" is force-close
# time, in ET, regardless of which UTC offset cron is currently running under.
WINDOW_START_MINUTES = 15 * 60 + 51  # 15:51 ET
WINDOW_END_MINUTES = 16 * 60 + 10  # 16:10 ET


def in_force_close_window() -> bool:
    now = datetime.now(ET)
    minutes = now.hour * 60 + now.minute
    return WINDOW_START_MINUTES <= minutes <= WINDOW_END_MINUTES


def log_error(message: str) -> None:
    """Append an error to logs/force_close_errors.log. Never raises."""
    try:
        LOG_DIR.mkdir(exist_ok=True)
        stamp = datetime.now(ET).isoformat(timespec="seconds")
        with ERROR_LOG.open("a") as f:
            f.write(f"[{stamp}] {message}\n")
    except Exception as exc:  # noqa: BLE001 - logging must never crash the safety net
        print(f"[force_close] could not write error log: {exc}", file=sys.stderr)


def safety_lock() -> None:
    """Same paper/live port lock as trade.py — this script auto-runs on paper only."""
    paper = os.environ.get("PAPER_TRADING", "").strip().lower()
    port = int(os.environ["IBKR_PORT"])
    if paper != "true":
        notify("EOD force_close", "Refusing to run: PAPER_TRADING is not true")
        sys.exit("ABORT: force_close only auto-runs on paper (PAPER_TRADING != true)")
    if port != 4002:
        sys.exit("ABORT: PAPER_TRADING=true but port is not 4002")


def close_position(client: IBKRClient, position) -> bool:
    """Flatten a single position with a market order. Returns True if order placed."""
    symbol = position.contract.symbol
    try:
        qty = abs(int(position.position))
        if qty == 0:
            return False
        side = "SELL" if position.position > 0 else "BUY"
        order = MarketOrder(side, qty, tif="DAY")
        # Route via SMART, not position.contract: the broker's position contract
        # carries the primary exchange (e.g. NASDAQ), and a directly-routed market
        # order is rejected by the direct-routing precaution (Error 10311 -> 201).
        contract = Stock(symbol, "SMART", "USD")
        client.ib.qualifyContracts(contract)
        client.ib.placeOrder(contract, order)
        client.ib.sleep(2)
        print(f"[force_close] {symbol}: placed market {side} {qty}")
        return True
    except Exception as exc:  # noqa: BLE001 - one symbol failing must not stop others
        log_error(f"{symbol}: failed to place close order: {exc}")
        print(f"[force_close] {symbol}: ERROR {exc}", file=sys.stderr)
        return False


def main() -> None:
    load_dotenv(PROJECT_ROOT / ".env")
    safety_lock()

    if not in_force_close_window():
        now_et = datetime.now(ET).strftime("%H:%M:%S")
        print(f"[force_close] {now_et} ET outside force-close window, exiting")
        return

    host = os.environ.get("IBKR_HOST", "127.0.0.1")
    port = int(os.environ["IBKR_PORT"])

    client: IBKRClient | None = None
    try:
        client = IBKRClient(host=host, port=port, client_id=CLIENT_ID)
    except Exception as exc:  # noqa: BLE001
        log_error(f"connection failed: {exc}")
        notify_error(f"EOD force_close could not connect to IB Gateway: {exc}")
        return

    try:
        # Cancel ALL resting SELL stops first (account-wide, not just for the
        # positions found below). They are GTC: flattening without cancelling
        # them leaves orphan stops that can trigger tomorrow and open a naked
        # short. Sweeping account-wide also cleans stops already orphaned by an
        # earlier manual close or crash.
        try:
            for trade in client.ib.reqAllOpenOrders():
                order = trade.order
                if order.action == "SELL" and order.orderType in ("STP", "STP LMT"):
                    try:
                        client.ib.cancelOrder(order)
                        client.ib.sleep(1)
                        print(
                            f"[force_close] cancelled stop "
                            f"{trade.contract.symbol} x{order.totalQuantity}"
                        )
                    except Exception as exc:  # noqa: BLE001 - keep sweeping
                        log_error(f"{trade.contract.symbol}: stop cancel failed: {exc}")
        except Exception as exc:  # noqa: BLE001 - sweep is best-effort
            log_error(f"open-order sweep failed: {exc}")

        positions = [p for p in client.ib.positions() if int(p.position) != 0]
        if not positions:
            # Runs every 3 min in the close window — the no-op case is just a
            # heartbeat, so log it but don't spam Telegram. Real closes still notify.
            print("[force_close] no open positions to close")
            return

        placed = 0
        for position in positions:
            if close_position(client, position):
                placed += 1

        deadline = client.ib.sleep  # noqa: F841 - keep ib event loop pumping below
        waited = 0
        while waited < FILL_POLL_SECONDS:
            client.ib.sleep(2)
            waited += 2
            try:
                if client.ib.fills():
                    # fills present; keep polling until the window elapses so late
                    # fills are also captured, but we already know orders landed
                    pass
            except Exception as exc:  # noqa: BLE001
                log_error(f"error polling fills: {exc}")
                break

        notify(
            "EOD force_close",
            f"Closed {placed} positions independently of cycle.py",
        )
    except Exception as exc:  # noqa: BLE001 - absolutely must not crash
        log_error(f"unexpected failure: {exc}\n{traceback.format_exc()}")
        notify_error(f"EOD force_close hit an unexpected error: {exc}")
    finally:
        if client is not None:
            try:
                client.disconnect()
            except Exception as exc:  # noqa: BLE001
                log_error(f"disconnect failed: {exc}")


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 - last-resort guard
        log_error(f"top-level guard caught: {exc}\n{traceback.format_exc()}")
        print(f"[force_close] fatal (suppressed): {exc}", file=sys.stderr)
