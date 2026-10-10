#!/usr/bin/env python3
"""Single-stock manual tester. For development / spot-checks only — not used by cron."""
import argparse
import csv
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from src.ibkr_client import IBKRClient
from src.notify import notify, notify_error
from src.safety import safety_lock
from strategy import evaluate

PROJECT_ROOT = Path(__file__).resolve().parent
TRADES_CSV = PROJECT_ROOT / "trades.csv"
RULES_PATH = PROJECT_ROOT / "config" / "rules.json"
ET = ZoneInfo("America/New_York")


def now_et() -> datetime:
    return datetime.now(ET)


def log(msg: str) -> None:
    print(f"[{now_et().strftime('%H:%M:%S')} ET] {msg}")


def count_todays_buys() -> int:
    if not TRADES_CSV.exists():
        return 0
    today = now_et().date()
    count = 0
    with TRADES_CSV.open(newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row.get("side") != "BUY":
                continue
            ts = row.get("timestamp", "")
            try:
                row_dt = datetime.fromisoformat(ts)
            except ValueError:
                continue
            if row_dt.astimezone(ET).date() == today:
                count += 1
    return count


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Manual single-stock tester")
    parser.add_argument("--symbol", type=str, required=True)
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="Evaluate strategy but do not submit an order",
    )
    return parser.parse_args()


def print_result(result: dict) -> None:
    verdict = "PASS" if result.get("pass") else "FAIL"
    log(f"Strategy {verdict} for {result.get('symbol', '')}")
    for reason in result.get("reasons", []):
        log(f"  - {reason}")


def submit_via_trade_py(symbol: str, size: int, limit_price: float) -> None:
    log(f"Submitting trade: {symbol} BUY {size} limit@{limit_price}")
    proc = subprocess.run(
        [
            sys.executable,
            str(PROJECT_ROOT / "trade.py"),
            "--symbol",
            symbol,
            "--side",
            "BUY",
            "--size",
            str(size),
            "--limit-price",
            str(limit_price),
        ],
        capture_output=True,
        text=True,
    )
    if proc.stdout:
        print(proc.stdout)
    if proc.returncode != 0 and proc.stderr:
        print(proc.stderr)


def main() -> None:
    args = parse_args()
    load_dotenv(PROJECT_ROOT / ".env")
    safety_lock()

    ibkr = IBKRClient(
        host=os.environ["IBKR_HOST"],
        port=int(os.environ["IBKR_PORT"]),
        client_id=int(os.environ["IBKR_CLIENT_ID"]),
    )

    try:
        # Risk numbers come from rules.json `risk` — the single source of truth
        # shared with cycle.py (.env risk vars are no longer read anywhere).
        with RULES_PATH.open(encoding="utf-8") as fh:
            risk_cfg = json.load(fh)["risk"]

        max_trades = int(risk_cfg["max_trades_per_day"])
        if count_todays_buys() >= max_trades:
            log("Daily trade limit reached")
            return

        result = evaluate(args.symbol, ibkr)
        print_result(result)

        if args.check_only or not result.get("pass"):
            return

        snapshot_price = result["price"]
        slippage_cap = float(risk_cfg["entry_slippage_cap_pct"]) / 100
        limit_price = round(snapshot_price * (1 + slippage_cap), 2)

        portfolio_value = float(risk_cfg["portfolio_value_usd"])
        max_risk_pct = float(risk_cfg["max_risk_per_trade_pct"]) / 100
        max_size_usd = float(risk_cfg["max_trade_size_usd"])
        max_position_pct = float(risk_cfg.get("max_position_pct", 0.10))

        stop_price = result["stop_price"]
        risk_per_share = snapshot_price - stop_price

        risk_budget = portfolio_value * max_risk_pct
        size_by_risk = int(risk_budget / risk_per_share) if risk_per_share > 0 else 0
        size_by_pct = int((portfolio_value * max_position_pct) / snapshot_price)
        size_by_max_usd = int(max_size_usd / snapshot_price)
        size = min(size_by_risk, size_by_pct, size_by_max_usd)

        if size <= 0:
            log("Size is 0, skipping")
            return

        submit_via_trade_py(args.symbol, size, limit_price)
    finally:
        ibkr.disconnect()


if __name__ == "__main__":
    main()
