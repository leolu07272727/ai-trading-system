#!/usr/bin/env python3
"""§12.1 active portfolio broadcast.

Pushes a full position snapshot to Telegram. Triggerable by cron (hourly) or
manually. Position data always comes from the broker (ib.positions()); local json
is only consulted best-effort for stop levels. Never crashes a cron run.
"""
from __future__ import annotations

import csv
import json
import os
import sys
from collections import deque
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from src.ibkr_client import IBKRClient
from src.notify import notify
from src.safety import safety_lock

PROJECT_ROOT = Path(__file__).resolve().parent
POSITIONS_FILE = PROJECT_ROOT / "open_positions.json"
TRADES_CSV = PROJECT_ROOT / "trades.csv"
ET = ZoneInfo("America/New_York")

CLIENT_ID = 11


def load_stop_prices() -> dict:
    """Best-effort read of stop levels from open_positions.json."""
    if not POSITIONS_FILE.exists():
        return {}
    try:
        with POSITIONS_FILE.open() as f:
            return json.load(f)
    except Exception:  # noqa: BLE001 - best-effort only
        return {}


def todays_realized_pnl() -> float:
    """Best-effort realized P&L from trades.csv via FIFO matching of today's fills."""
    if not TRADES_CSV.exists():
        return 0.0
    today = datetime.now(ET).date().isoformat()
    lots: dict[str, deque] = {}
    realized = 0.0
    try:
        with TRADES_CSV.open(newline="") as f:
            for row in csv.DictReader(f):
                if not (row.get("timestamp", "")).startswith(today):
                    continue
                if (row.get("status") or "").strip() != "Filled":
                    continue
                symbol = row["symbol"]
                side = (row["side"] or "").upper()
                qty = int(float(row["size"]))
                price = float(row["fill_price"])
                book = lots.setdefault(symbol, deque())
                if side == "BUY":
                    book.append([qty, price])
                elif side == "SELL":
                    remaining = qty
                    while remaining > 0 and book:
                        lot = book[0]
                        matched = min(remaining, lot[0])
                        realized += (price - lot[1]) * matched
                        lot[0] -= matched
                        remaining -= matched
                        if lot[0] == 0:
                            book.popleft()
    except Exception:  # noqa: BLE001 - best-effort only
        return 0.0
    return realized


def format_position_line(symbol: str, pnl_pct: float, pnl_usd: float, stop_pct) -> str:
    stop_txt = f"{stop_pct:+.1f}%" if stop_pct is not None else "N/A"
    return f"{symbol}  {pnl_pct:+.1f}%  {pnl_usd:+.0f}$  停損 {stop_txt}"


def build_message(client: IBKRClient) -> str | None:
    positions = [p for p in client.ib.positions() if int(p.position) != 0]
    if not positions:
        notify("📊 Portfolio Snapshot", "No open positions")
        return None

    stops = load_stop_prices()
    # rules.json is the single risk source (the old MAX_POSITIONS env var never
    # existed — this line displayed "N/?" since day one).
    try:
        with (PROJECT_ROOT / "config" / "rules.json").open() as f:
            max_pos = json.load(f)["risk"]["max_concurrent_positions"]
    except Exception:  # noqa: BLE001 - display-only
        max_pos = "?"
    lines = []
    total_pnl = 0.0

    for pos in positions:
        symbol = pos.contract.symbol
        qty = int(pos.position)
        avg_cost = float(pos.avgCost)
        try:
            current_price = client.get_snapshot_price(symbol)
        except Exception:  # noqa: BLE001 - skip symbols without a price
            lines.append(f"{symbol}  (no price)")
            continue

        pnl_pct = (current_price - avg_cost) / avg_cost * 100 if avg_cost else 0.0
        pnl_usd = (current_price - avg_cost) * qty
        total_pnl += pnl_usd

        stop_pct = None
        entry = stops.get(symbol)
        if isinstance(entry, dict) and "stop_price" in entry and current_price:
            stop_price = float(entry["stop_price"])
            stop_pct = (current_price - stop_price) / current_price * 100

        lines.append(format_position_line(symbol, pnl_pct, pnl_usd, stop_pct))

    now_et = datetime.now(ET).strftime("%H:%M")
    realized = todays_realized_pnl()
    divider = "─" * 13
    body = "\n".join(
        [
            f"📊 持倉快照 {now_et} ET",
            divider,
            *lines,
            divider,
            f"總未實現 {total_pnl:+.0f}$ | {len(positions)}/{max_pos} 部位",
            f"今日已實現 {realized:+.0f}$",
        ]
    )
    return body


def main() -> None:
    load_dotenv(PROJECT_ROOT / ".env")
    safety_lock()

    host = os.environ.get("IBKR_HOST", "127.0.0.1")
    port = int(os.environ["IBKR_PORT"])

    client: IBKRClient | None = None
    try:
        client = IBKRClient(host=host, port=port, client_id=CLIENT_ID)
        body = build_message(client)
        if body is not None:
            notify("📊 Portfolio Snapshot", body)
    except Exception as exc:  # noqa: BLE001 - never crash a cron run
        print(f"[portfolio_snapshot] failed: {exc}", file=sys.stderr)
    finally:
        if client is not None:
            try:
                client.disconnect()
            except Exception as exc:  # noqa: BLE001
                print(f"[portfolio_snapshot] disconnect failed: {exc}", file=sys.stderr)


if __name__ == "__main__":
    main()
