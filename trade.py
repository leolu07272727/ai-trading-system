#!/usr/bin/env python3
"""Pure order executor. Called as a subprocess by cycle.py — never run directly."""
import argparse
import csv
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from src.ibkr_client import IBKRClient
from src.notify import notify, notify_error
from src.safety import safety_lock

PROJECT_ROOT = Path(__file__).resolve().parent
TRADES_CSV = PROJECT_ROOT / "trades.csv"
ET = ZoneInfo("America/New_York")
CSV_HEADER = [
    "timestamp",
    "symbol",
    "side",
    "size",
    "fill_price",
    "order_id",
    "status",
    "limit_price",
]


def append_trade_row(row: list) -> None:
    write_header = not TRADES_CSV.exists()
    with TRADES_CSV.open("a", newline="") as f:
        writer = csv.writer(f)
        if write_header:
            writer.writerow(CSV_HEADER)
        writer.writerow(row)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="IBKR limit order executor")
    parser.add_argument("--symbol", type=str, required=True)
    parser.add_argument("--side", type=str, required=True, choices=["BUY", "SELL"])
    parser.add_argument("--size", type=int, required=True)
    parser.add_argument("--limit-price", type=float, required=True, dest="limit_price")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    load_dotenv(PROJECT_ROOT / ".env")
    safety_lock()

    ibkr = IBKRClient(
        host=os.environ["IBKR_HOST"],
        port=int(os.environ["IBKR_PORT"]),
        client_id=int(os.environ["IBKR_EXEC_CLIENT_ID"]),
    )

    def record_fill_and_exit(trade) -> None:
        fill_price = trade.orderStatus.avgFillPrice
        now = datetime.now(ET)
        append_trade_row(
            [
                now.isoformat(),
                args.symbol,
                args.side,
                args.size,
                fill_price,
                trade.order.orderId,
                "Filled",
                args.limit_price,
            ]
        )
        msg = (
            f"[{now.strftime('%H:%M:%S')} ET] FILLED "
            f"{args.symbol} {args.side} {args.size}@{fill_price}"
        )
        print(msg)
        notify("Trade Filled", msg)
        sys.exit(0)

    CANCELLED = ("Cancelled", "Inactive", "ApiCancelled")
    FILL_WAIT_S = 20    # extra wait beyond place_limit_order's own poll
    CANCEL_WAIT_S = 15  # wait for the cancel to reach a terminal state

    try:
        trade = ibkr.place_limit_order(
            args.symbol, args.side, args.size, args.limit_price
        )

        # Give a live limit order a real chance to fill before judging it.
        for _ in range(FILL_WAIT_S):
            if trade.orderStatus.status in ("Filled",) + CANCELLED:
                break
            ibkr.ib.sleep(1)
        status = trade.orderStatus.status

        if status == "Filled":
            record_fill_and_exit(trade)
        if status in CANCELLED:
            print(f"Order not filled: {status}")
            sys.exit(1)

        # Still pending → CANCEL before exiting. Never abandon a live order:
        # 2026-07-02 AAPB exited 2 here with the order still resting, it filled
        # seconds later, and the book had an untracked unprotected position for
        # 5 minutes until reconcile adopted it.
        print(f"Order pending after wait: {status} — cancelling")
        ibkr.ib.cancelOrder(trade.order)
        for _ in range(CANCEL_WAIT_S):
            ibkr.ib.sleep(1)
            status = trade.orderStatus.status
            if status in ("Filled",) + CANCELLED:
                break

        if status == "Filled":  # fill/cancel race resolved as a fill — book it
            record_fill_and_exit(trade)
        if status in CANCELLED:
            print(f"Order not filled (cancelled after timeout): {status}")
            sys.exit(1)

        # Cancel didn't confirm — truly ambiguous. Reconcile is the backstop;
        # make noise so a human looks.
        msg = f"{args.symbol} {args.side} order state ambiguous after cancel: {status}"
        print(msg)
        notify_error("Trade ambiguous", msg)
        sys.exit(2)
    finally:
        ibkr.disconnect()


if __name__ == "__main__":
    main()
