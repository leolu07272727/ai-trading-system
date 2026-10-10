"""Pre-market scanner (§8.2).

Runs before market open. Scans the S&P 500 universe for gap-ups using
yfinance. yfinance (not IBKR) is intentional here: pre-market, scanning ~500
tickers for gap%, a few minutes of delay is irrelevant. IBKR is reserved for
intraday decisions where the live price actually matters.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf
from dotenv import load_dotenv

from src.notify import notify, notify_changed
from src.sp500_tickers import SP500_TICKERS

ET = ZoneInfo("America/New_York")
PROJECT_ROOT = Path(__file__).resolve().parent
RULES_PATH = PROJECT_ROOT / "config" / "rules.json"
WATCHLIST_PATH = PROJECT_ROOT / "watchlist.txt"
EXTRA_UNIVERSE_PATH = PROJECT_ROOT / "config" / "etf_universe.txt"

BATCH_SIZE = 50
TOP_N = 30
WARN_FAILURE_RATE = 0.30
HARD_FAILURE_RATE = 0.95


def load_rules() -> dict:
    with RULES_PATH.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def load_extra_universe() -> dict[str, float]:
    """Extra non-S&P-500 tickers (leveraged ETFs) to fold into the gap scan.

    Format per line: `SYMBOL [LEVERAGE]` — e.g. `NVDL 2`. Leverage defaults to 1
    and scales the gap threshold: a 2x ETF must gap 2x the configured % so its
    UNDERLYING moved as much as a qualifying stock. Blank lines and '#' comments
    (whole-line or trailing) are ignored. Missing file → empty dict."""
    if not EXTRA_UNIVERSE_PATH.exists():
        return {}
    out: dict[str, float] = {}
    for raw in EXTRA_UNIVERSE_PATH.read_text(encoding="utf-8").splitlines():
        body = raw.split("#", 1)[0].strip().upper()
        if not body:
            continue
        parts = body.split()
        sym = parts[0]
        try:
            lev = float(parts[1]) if len(parts) > 1 else 1.0
        except ValueError:
            lev = 1.0
        out[sym] = max(1.0, lev)
    return out


def build_universe() -> tuple[list[str], dict[str, float]]:
    """S&P 500 names plus the extra ETF universe, de-duplicated, order kept.

    Returns (tickers, leverage_map); anything absent from the map is 1x."""
    leverage = load_extra_universe()
    seen: set[str] = set()
    universe: list[str] = []
    for sym in list(SP500_TICKERS) + list(leverage.keys()):
        if sym not in seen:
            seen.add(sym)
            universe.append(sym)
    return universe, leverage


def resolve_threshold(rules: dict) -> float:
    """Pick the gap threshold, honouring test_mode relaxation."""
    test_mode = rules.get("test_mode", {})
    if test_mode.get("relax_filters"):
        return float(test_mode.get("relaxed_min_gap_pct", 2.0))
    return float(rules["daily_filters"]["D3_min_gap_pct_from_prior_close"])


def _batches(items: list[str], size: int):
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _extract_gap(frame: pd.DataFrame) -> tuple[float, float] | None:
    """Return (gap_pct, today_open) from a 2-day OHLC frame, or None."""
    if frame is None or frame.empty or len(frame) < 2:
        return None
    try:
        prior_close = float(frame["Close"].iloc[-2])
        today_open = float(frame["Open"].iloc[-1])
    except (KeyError, IndexError, ValueError, TypeError):
        return None
    if pd.isna(prior_close) or pd.isna(today_open) or prior_close <= 0:
        return None
    gap_pct = (today_open - prior_close) / prior_close * 100.0
    return gap_pct, today_open


def scan(
    tickers: list[str],
    threshold_pct: float,
    min_price: float,
    leverage: dict[str, float] | None = None,
) -> dict:
    leverage = leverage or {}
    candidates: list[dict] = []
    failures = 0
    total = len(tickers)

    for batch in _batches(tickers, BATCH_SIZE):
        try:
            data = yf.download(
                tickers=batch,
                period="2d",
                interval="1d",
                threads=5,
                group_by="ticker",
                auto_adjust=True,
                progress=False,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"[scan] batch download failed: {exc}", file=sys.stderr)
            failures += len(batch)
            continue

        for ticker in batch:
            try:
                frame = data[ticker] if len(batch) > 1 else data
            except (KeyError, TypeError):
                failures += 1
                continue

            result = _extract_gap(frame)
            if result is None:
                failures += 1
                continue

            gap_pct, today_open = result
            # Leverage-adjusted gap: divide by the ETF's leverage so a 2x/3x
            # product competes (and gates) on its UNDERLYING's move — otherwise
            # leveraged ETFs dominate the TOP_N sort every day and crowd out
            # the single-stock candidates the strategy is actually about.
            lev = float(leverage.get(ticker, 1.0))
            adj_gap = gap_pct / lev
            if adj_gap >= threshold_pct and today_open >= min_price:
                candidates.append(
                    {
                        "ticker": ticker,
                        "gap_pct": round(gap_pct, 2),
                        "adj_gap_pct": round(adj_gap, 2),
                        "open": round(today_open, 2),
                    }
                )

    candidates.sort(key=lambda c: c["adj_gap_pct"], reverse=True)
    failure_rate = failures / total if total else 0.0
    return {
        "candidates": candidates[:TOP_N],
        "failures": failures,
        "total": total,
        "failure_rate": failure_rate,
    }


def report_degradation(failure_rate: float) -> None:
    if failure_rate >= HARD_FAILURE_RATE:
        print("Yahoo Finance completely failed", file=sys.stderr)
    elif failure_rate >= WARN_FAILURE_RATE:
        print(
            f"WARNING: high yfinance failure rate "
            f"({failure_rate * 100:.0f}%)",
            file=sys.stderr,
        )


def write_watchlist(tickers: list[str]) -> None:
    WATCHLIST_PATH.write_text("\n".join(tickers) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Pre-market gap scanner.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print results but do not write watchlist.txt.",
    )
    args = parser.parse_args(argv)

    load_dotenv(PROJECT_ROOT / ".env")

    # Pre-open gate: before ~09:31 ET yfinance has no daily row for today yet,
    # so "gap" would compare YESTERDAY's open to the prior close — garbage that
    # also got pushed as the once-per-day Telegram heartbeat. Cron fires every
    # 30 min; the 09:30+ runs do the real scan. --dry-run bypasses for testing.
    now_et = datetime.now(ET)
    if not args.dry_run and now_et.time() < dtime(9, 31):
        print(f"[prefilter] {now_et:%H:%M} ET pre-open — skipping scan")
        return 0

    rules = load_rules()

    threshold_pct = resolve_threshold(rules)
    min_price = float(rules["universe_filters"]["min_price_usd"])

    today = datetime.now(ET).date().isoformat()
    universe, leverage = build_universe()
    result = scan(universe, threshold_pct, min_price, leverage)

    report_degradation(result["failure_rate"])

    watchlist = [c["ticker"] for c in result["candidates"]]
    count = len(watchlist)

    summary = {
        "date": today,
        "candidates": count,
        "watchlist": watchlist,
        "threshold_pct": threshold_pct,
    }
    print(json.dumps(summary, indent=2))

    if not args.dry_run:
        write_watchlist(watchlist)

    # Heartbeat: morning_prefilter runs every 30 min — notify at most once/day.
    notify_changed(
        "morning_scan",
        "Morning scan",
        f"{count} candidates above {threshold_pct:.1f}% gap",
        per_day_only=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
