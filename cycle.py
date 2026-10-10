#!/usr/bin/env python3
"""Main trading orchestrator for the Trend Join Long paper-trading bot.

Run once per scheduled tick (e.g. every 5 minutes during RTH). Decides — based on
the current Eastern-time window — whether to reconcile, manage open positions,
pyramid, force-close at EOD, or scan for new entries. Order execution itself is
delegated to trade.py as a subprocess; cycle.py owns state in open_positions.json
and the broker-side protective stops.

Never run trade.py directly — this script is the only intended driver.
"""

from __future__ import annotations

import csv
import json
import os
import subprocess
import sys
import traceback
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
from dotenv import load_dotenv
from ib_async import MarketOrder, Order, Stock

from src.ibkr_client import IBKRClient
from src.notify import notify, notify_changed
from src.safety import safety_lock
from src.sp500_tickers import get_sector
from strategy import _sma200, evaluate

ROOT = Path(__file__).resolve().parent
ET = ZoneInfo("America/New_York")

LOG_DIR = ROOT / "logs"
DECISION_LOG = LOG_DIR / "decision_log.jsonl"
CRASH_LOG = LOG_DIR / "cycle_errors.log"
POSITIONS_PATH = ROOT / "open_positions.json"
POSITIONS_TMP = ROOT / "open_positions.json.tmp"
RULES_PATH = ROOT / "config" / "rules.json"
TRADES_CSV = ROOT / "trades.csv"
WATCHLIST_PATH = ROOT / "watchlist.txt"

TRADE_SCRIPT = ROOT / "trade.py"
PENDING_ENTRIES = ROOT / "logs" / ".pending_entries.json"


# --------------------------------------------------------------------------- #
# Time gate
# --------------------------------------------------------------------------- #


def time_gate() -> str:
    """Classify the current Eastern-time moment into a trading-window state."""
    now = datetime.now(ET)
    if now.weekday() >= 5:  # Saturday=5, Sunday=6
        return "weekend"
    minutes = now.hour * 60 + now.minute
    if minutes < 9 * 60:  # before 09:00
        return "too_early"
    if minutes > 16 * 60 + 30:  # after 16:30
        return "closed"
    if 15 * 60 + 51 <= minutes <= 16 * 60:  # 15:51–16:00
        return "force_close"
    if 15 * 60 + 31 <= minutes <= 15 * 60 + 50:  # 15:31–15:50
        return "manage_only"
    if 9 * 60 <= minutes <= 15 * 60 + 30:  # 09:00–15:30
        return "ok"
    return "closed"  # 16:01–16:30 falls through to closed


# --------------------------------------------------------------------------- #
# State helpers
# --------------------------------------------------------------------------- #
def load_positions() -> dict:
    if not POSITIONS_PATH.exists():
        return {}
    try:
        return json.loads(POSITIONS_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def save_positions(positions: dict) -> None:
    """Atomic write: serialise to a temp file, then os.replace into place."""
    POSITIONS_TMP.write_text(json.dumps(positions, indent=2), encoding="utf-8")
    os.replace(POSITIONS_TMP, POSITIONS_PATH)


def load_rules() -> dict:
    with RULES_PATH.open(encoding="utf-8") as handle:
        return json.load(handle)


def log_decision(event: str, symbol: str, detail: str) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    entry = {
        "ts": datetime.now(ET).isoformat(),
        "event": str(event),
        "symbol": str(symbol),
        "detail": str(detail),
    }
    with DECISION_LOG.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry) + "\n")


def log_crash(exc: Exception) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(ET).isoformat(timespec="seconds")
    with CRASH_LOG.open("a", encoding="utf-8") as handle:
        handle.write(f"[{stamp}] {type(exc).__name__}: {exc}\n")
        handle.write(traceback.format_exc())
        handle.write("\n")


# --------------------------------------------------------------------------- #
# trades.csv helpers
# --------------------------------------------------------------------------- #
def _read_trades() -> pd.DataFrame:
    if not TRADES_CSV.exists():
        return pd.DataFrame()
    try:
        return pd.read_csv(TRADES_CSV)
    except (pd.errors.EmptyDataError, OSError):
        return pd.DataFrame()


def _row_is_today(timestamp: str) -> bool:
    try:
        ts = datetime.fromisoformat(str(timestamp))
    except ValueError:
        return False
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=ET)
    return ts.astimezone(ET).date() == datetime.now(ET).date()


def count_todays_buys() -> int:
    """Count BUY rows recorded in trades.csv for the current ET date."""
    df = _read_trades()
    if df.empty or "side" not in df.columns:
        return 0
    count = 0
    for _, row in df.iterrows():
        if str(row.get("side", "")).upper() != "BUY":
            continue
        if _row_is_today(row.get("timestamp", "")):
            count += 1
    return count


def _get_last_fill_price(symbol: str) -> float | None:
    """Latest filled BUY price for `symbol` recorded today, or None if absent."""
    df = _read_trades()
    if df.empty or "symbol" not in df.columns:
        return None
    fill: float | None = None
    for _, row in df.iterrows():
        if str(row.get("symbol", "")).upper() != symbol.upper():
            continue
        if str(row.get("side", "")).upper() != "BUY":
            continue
        if str(row.get("status", "")).strip() and str(row.get("status")) != "Filled":
            continue
        if not _row_is_today(row.get("timestamp", "")):
            continue
        try:
            fill = float(row.get("fill_price"))
        except (TypeError, ValueError):
            continue
    return fill


def _append_reconciled_buy(symbol: str, qty: int, avg_cost: float) -> None:
    """Reconcile-adopted fills must still land in trades.csv — it feeds
    count_todays_buys, today_decisions.py and the experiment cohorts.
    (2026-07-02: AAPB filled after trade.py timed out; reconcile healed the
    book but the fill stayed invisible to every report.)"""
    write_header = not TRADES_CSV.exists()
    with TRADES_CSV.open("a", newline="") as f:
        writer = csv.writer(f)
        if write_header:
            writer.writerow(["timestamp", "symbol", "side", "size", "fill_price",
                             "order_id", "status", "limit_price"])
        writer.writerow([datetime.now(ET).isoformat(), symbol, "BUY", int(qty),
                         float(avg_cost), "", "Filled(reconciled)", ""])


# --------------------------------------------------------------------------- #
# Pending-entry intents — written before trade.py runs, so a fill that the
# entry path lost track of (timeout/crash) can be adopted by reconcile with
# the SAME stop the strategy+judge approved, not an accidental default.
# --------------------------------------------------------------------------- #
def _load_pending_entries() -> dict:
    try:
        data = json.loads(PENDING_ENTRIES.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    # only today's intents count — stale ones must not set tomorrow's stops
    return {s: v for s, v in data.items() if _row_is_today(v.get("ts", ""))}


def _save_pending_entries(entries: dict) -> None:
    try:
        PENDING_ENTRIES.write_text(json.dumps(entries), encoding="utf-8")
    except OSError:
        pass


def _note_pending_entry(symbol: str, size: int, stop_price: float) -> None:
    entries = _load_pending_entries()
    entries[symbol] = {"size": int(size), "stop_price": float(stop_price),
                       "ts": datetime.now(ET).isoformat()}
    _save_pending_entries(entries)


def _clear_pending_entry(symbol: str) -> None:
    entries = _load_pending_entries()
    if symbol in entries:
        del entries[symbol]
        _save_pending_entries(entries)


# --------------------------------------------------------------------------- #
# Order helpers
# --------------------------------------------------------------------------- #
def _spawn_trade(symbol: str, side: str, qty: int, limit_price: float) -> int:
    """Run trade.py as a subprocess. Returns its exit code (0 == filled)."""
    cmd = [
        sys.executable,
        str(TRADE_SCRIPT),
        "--symbol",
        symbol,
        "--side",
        side,
        "--size",
        str(int(qty)),
        "--limit-price",
        f"{float(limit_price):.2f}",
    ]
    proc = subprocess.run(cmd, cwd=str(ROOT), check=False)
    return proc.returncode


def _cancel_order(ibkr: IBKRClient, order_id) -> None:
    """Cancel a resting order by id.

    A fresh process (even with the same clientId) does not recognise an Order
    rebuilt from a bare orderId — ib_async logs "Unknown orderId" and the cancel
    is dropped. Resolve the live order object via reqAllOpenOrders first, then
    cancel that, so cancels work across cron invocations.
    """
    if order_id is None:
        return
    try:
        target = int(order_id)
    except (TypeError, ValueError):
        return
    try:
        for trade in ibkr.ib.reqAllOpenOrders():
            if trade.order.orderId == target:
                ibkr.ib.cancelOrder(trade.order)
                ibkr.ib.sleep(1)
                return
    except Exception:  # noqa: BLE001 - cancel is best-effort
        pass


def _update_stop(ibkr: IBKRClient, symbol: str, pos: dict, new_stop: float) -> None:
    """Replace the protective stop for the current qty.

    Places the new stop *before* cancelling the old one — if placement raises
    (API hiccup, transient disconnect), the old stop is left live rather than
    leaving the position unprotected until the next cron tick.
    """
    old_order_id = pos.get("stop_order_id")
    trade = ibkr.place_stop_order(
        symbol, "SELL", int(pos["qty"]), round(float(new_stop), 2)
    )
    pos["stop_order_id"] = int(trade.order.orderId)
    pos["stop_perm_id"] = int(trade.order.permId)
    _cancel_order(ibkr, old_order_id)


# --------------------------------------------------------------------------- #
# Cycle stages
# --------------------------------------------------------------------------- #
def reconcile(ibkr: IBKRClient, json_positions: dict) -> dict:
    """§10 - bring open_positions.json into agreement with the broker."""
    broker_positions = {
        pos.contract.symbol: pos
        for pos in ibkr.ib.positions()
        if int(pos.position) != 0
    }

    # Account-wide resting orders (survives a client reconnect). Used to avoid
    # stacking a second protective stop on a position we re-adopt — e.g. when a
    # prior cycle placed entry+stop but crashed before saving json.
    existing_stops: dict[str, list] = {}
    for trade in ibkr.ib.reqAllOpenOrders():
        order = trade.order
        if order.action == "SELL" and order.orderType in ("STP", "STP LMT"):
            existing_stops.setdefault(trade.contract.symbol, []).append(trade)

    # In broker but not tracked: adopt + place a protective stop.
    pending = _load_pending_entries()
    for symbol, broker_pos in broker_positions.items():
        if symbol in json_positions:
            continue
        qty = int(broker_pos.position)
        avg_cost = float(broker_pos.avgCost)
        # Prefer the stop the strategy+judge approved at entry time (pending
        # intent) over the 1% default — the default is for positions we know
        # nothing about; a lost fill is not one of those. Sanity: intent stop
        # must sit below cost, else fall back.
        intent = pending.get(symbol)
        if intent and 0 < float(intent.get("stop_price", 0)) < avg_cost:
            stop_price = round(float(intent["stop_price"]), 2)
            stop_src = "pending-entry intent"
        else:
            stop_price = round(avg_cost * 0.99, 2)
            stop_src = "default 1%"
        # Cancel any pre-existing stops for this symbol first so we end up with
        # exactly one stop sized to the actual position. Cancel the live order
        # objects from reqAllOpenOrders directly (a rebuilt Order is not honoured).
        for stale in existing_stops.get(symbol, []):
            try:
                ibkr.ib.cancelOrder(stale.order)
                ibkr.ib.sleep(1)
            except Exception:  # noqa: BLE001 - cancel is best-effort
                pass
        stop_trade = ibkr.place_stop_order(symbol, "SELL", qty, stop_price)
        json_positions[symbol] = {
            "symbol": symbol,
            "qty": qty,
            "avg_entry_price": float(avg_cost),
            "stop_price": float(stop_price),
            "stop_order_id": int(stop_trade.order.orderId),
            "stop_perm_id": int(stop_trade.order.permId),
            "sector": str(get_sector(symbol)),
            "entry_time": datetime.now(ET).isoformat(),
            "adds_count": 0,
            "initial_risk_per_share": float(avg_cost - stop_price),
            "partial_done": False,
            "breakeven_done": False,
        }
        _append_reconciled_buy(symbol, qty, avg_cost)
        _clear_pending_entry(symbol)
        log_decision(
            "reconcile_added",
            symbol,
            f"broker has {qty} shares not in json — added + stop placed "
            f"@{stop_price} ({stop_src}); fill recorded to trades.csv",
        )
        notify("⚠ Reconcile", f"{symbol}: found {qty} untracked shares — "
                              f"stop placed @{stop_price} ({stop_src})")

    # Tracked but not on broker: cancel its protective stop FIRST, then drop it.
    # The stop is GTC — leaving it live with no position means a later trigger
    # opens a naked short. Also sweep any other resting SELL stops on the symbol
    # (e.g. left behind by a manual close or force_close.py).
    for symbol in list(json_positions.keys()):
        if symbol not in broker_positions:
            _cancel_order(ibkr, json_positions[symbol].get("stop_order_id"))
            for stale in existing_stops.get(symbol, []):
                try:
                    ibkr.ib.cancelOrder(stale.order)
                    ibkr.ib.sleep(1)
                except Exception:  # noqa: BLE001 - cancel is best-effort
                    pass
            del json_positions[symbol]
            log_decision(
                "reconcile_removed",
                symbol,
                "json had position not on broker — stop cancelled + removed",
            )

    # Quantity mismatch: trust the broker.
    for symbol, broker_pos in broker_positions.items():
        if symbol not in json_positions:
            continue
        json_qty = int(json_positions[symbol]["qty"])
        broker_qty = int(broker_pos.position)
        if abs(json_qty - broker_qty) > 0:
            json_positions[symbol]["qty"] = broker_qty
            notify(
                "⚠ Reconcile qty mismatch",
                f"{symbol}: json={json_qty} broker={broker_qty} — corrected",
            )

    save_positions(json_positions)
    return broker_positions


def check_regime(ibkr: IBKRClient) -> bool:
    """§Fix4 - block new entries when SPY trades below its 200-day SMA."""
    spy_sma = _sma200("SPY")
    if spy_sma is None:
        log_decision("regime", "SPY", "SMA200 unavailable — blocking new entries")
        notify_changed("regime", "Regime", "SPY SMA200 unavailable, skipping new entries")
        return False
    try:
        spy_price = ibkr.get_snapshot_price("SPY")
    except ValueError as exc:
        # Transient market-data gap (delayed/demo feed). Don't crash the whole
        # cycle — that would also skip stop-out detection and position
        # management. Treat regime as unknown and block only new entries.
        log_decision("regime", "SPY", f"snapshot unavailable, blocking entries: {exc}")
        notify_changed("regime", "Regime", "SPY price unavailable, skipping new entries")
        return False
    if spy_price < spy_sma:
        log_decision(
            "regime",
            "SPY",
            f"SPY {spy_price:.2f} below SMA200 {spy_sma:.2f}",
        )
        notify_changed("regime", "Regime", "SPY below SMA200, skipping new entries")
        return False
    return True


def check_stop_outs(ibkr: IBKRClient, json_positions: dict) -> None:
    """Detect filled protective stops by matching execution orderId/permId."""
    fills = ibkr.ib.fills()
    for symbol, pos in list(json_positions.items()):
        # Match on permId (globally unique) + SELL side. orderId is per-connection
        # and collides with the entry order placed by trade.py on another client,
        # which would otherwise look like an instant stop-out at the entry price.
        stop_perm_id = pos.get("stop_perm_id")
        if not stop_perm_id:
            continue
        stop_fills = [
            f
            for f in fills
            if f.execution.permId == stop_perm_id and f.execution.side == "SLD"
        ]
        if not stop_fills:
            continue
        fill_price = float(stop_fills[0].execution.price)
        pnl = (fill_price - float(pos["avg_entry_price"])) * int(pos["qty"])
        notify("STOP OUT", f"{symbol}: stopped at {fill_price:.2f}, P&L {pnl:+.2f}")
        log_decision("stop_out", symbol, f"stop filled at {fill_price}")
        del json_positions[symbol]

    save_positions(json_positions)


def manage_positions(
    ibkr: IBKRClient, json_positions: dict, prices: dict[str, float]
) -> None:
    """Trailing stops, one-time partial profit, breakeven, post-BE swing trail."""
    rules = load_rules()
    exit_rules = rules["exit"]
    partial_trigger = float(exit_rules["partial_profit_trigger_R"])
    partial_frac = float(exit_rules["partial_profit_fraction"])
    be_trigger = float(exit_rules["breakeven_trigger_R"])
    # Backtest (2026-06) showed the partial + breakeven + swing-trail combo CLIPS
    # the right-tail winners that carry this momentum strategy (expectancy −0.73R
    # vs +0.03R for ride-to-close). Both default True for back-compat; rules.json
    # disables them so the full position rides to force_close on the initial stop.
    partial_enabled = bool(exit_rules.get("partial_profit_enabled", True))
    be_enabled = bool(exit_rules.get("breakeven_enabled", True))

    for symbol, pos in list(json_positions.items()):
        current_price = prices.get(symbol)
        if current_price is None:  # no quote this tick — broker stop still protects
            continue
        entry = float(pos["avg_entry_price"])
        stop = float(pos["stop_price"])
        initial_risk = float(pos.get("initial_risk_per_share", entry - stop))
        qty = int(pos["qty"])

        if initial_risk <= 0:
            continue

        current_r = (current_price - entry) / initial_risk

        # Partial profit at trigger R (one-time). Only mutate our book if the
        # sell actually filled (exit code 0) — otherwise qty/stop would drift
        # from the broker until the next reconcile.
        if partial_enabled and current_r >= partial_trigger and not pos.get("partial_done"):
            partial_qty = max(1, int(qty * partial_frac))
            partial_qty = min(partial_qty, qty)
            ret = _spawn_trade(symbol, "SELL", partial_qty, current_price * 0.998)
            if ret != 0:
                log_decision(
                    "partial_failed", symbol, f"trade.py exited {ret} — book unchanged"
                )
                continue
            pos["qty"] = qty - partial_qty
            pos["partial_done"] = True
            # Resize the broker-side stop to the reduced qty immediately — it must
            # never protect more shares than are actually held (§9/§10).
            _update_stop(ibkr, symbol, pos, stop)
            notify(
                "PARTIAL PROFIT",
                f"{symbol}: sold {partial_qty} @ ~{current_price:.2f} "
                f"({current_r:.2f}R)",
            )
            log_decision(
                "partial_profit", symbol, f"{current_r:.2f}R — sold {partial_qty}"
            )
            qty = pos["qty"]

        # Move stop to breakeven at trigger R (one-time, only upward).
        if be_enabled and current_r >= be_trigger and not pos.get("breakeven_done"):
            new_stop = entry
            if new_stop > stop:
                _update_stop(ibkr, symbol, pos, new_stop)
                pos["stop_price"] = float(new_stop)
                stop = float(new_stop)
                pos["breakeven_done"] = True
                notify("BREAKEVEN", f"{symbol}: stop moved to {new_stop:.2f}")
                log_decision("breakeven", symbol, f"stop moved to entry {new_stop:.2f}")
            else:
                pos["breakeven_done"] = True

        # Post-breakeven: trail to recent 5m swing low (only upward).
        if pos.get("breakeven_done"):
            bars = ibkr.get_5m_bars(symbol, lookback=10)
            if not bars.empty:
                swing_low = float(bars["low"].tail(2).min())
                if swing_low > float(pos["stop_price"]):
                    _update_stop(ibkr, symbol, pos, swing_low)
                    pos["stop_price"] = swing_low
                    log_decision("trail_stop", symbol, f"trailing stop → {swing_low:.2f}")

    save_positions(json_positions)


def pyramid_check(
    ibkr: IBKRClient, json_positions: dict, prices: dict[str, float]
) -> None:
    """§9 - add to winners when enabled, keeping combined risk bounded."""
    rules = load_rules()
    pyramiding = rules.get("pyramiding", {})
    if not pyramiding.get("enabled"):
        return

    risk_cfg = rules["risk"]
    max_adds = int(pyramiding.get("max_adds", 1))
    trigger_r = float(pyramiding.get("trigger_R", 1.0))
    portfolio_val = float(risk_cfg["portfolio_value_usd"])
    max_risk_pct = float(risk_cfg["max_risk_per_trade_pct"]) / 100
    slippage = float(risk_cfg["entry_slippage_cap_pct"]) / 100
    risk_budget = portfolio_val * max_risk_pct
    max_risk = portfolio_val * max_risk_pct

    for symbol, pos in json_positions.items():
        if pos.get("adds_count", 0) >= max_adds:
            continue
        current_price = prices.get(symbol)
        if current_price is None:
            continue
        entry = float(pos["avg_entry_price"])
        initial_risk = float(pos.get("initial_risk_per_share", 1.0))
        current_r = (current_price - entry) / initial_risk if initial_risk > 0 else 0.0
        if current_r < trigger_r:
            continue

        stop_price = float(pos["stop_price"])
        risk_per_share = current_price - stop_price
        if risk_per_share <= 0:
            continue
        add_qty = int(risk_budget / risk_per_share)
        if add_qty <= 0:
            continue

        total_qty = int(pos["qty"]) + add_qty
        total_risk = risk_per_share * total_qty
        if total_risk > max_risk * 2:  # allow up to 2x risk for a pyramid add
            log_decision(
                "pyramid_skipped",
                symbol,
                f"combined risk {total_risk:.0f} would exceed limit",
            )
            continue

        limit_p = round(current_price * (1 + slippage), 2)
        ret = _spawn_trade(symbol, "BUY", add_qty, limit_p)
        if ret != 0:
            log_decision("pyramid_failed", symbol, f"trade.py exited {ret}")
            continue

        new_total_qty = int(pos["qty"]) + add_qty
        new_avg = (entry * int(pos["qty"]) + current_price * add_qty) / new_total_qty
        pos["avg_entry_price"] = round(float(new_avg), 4)
        pos["qty"] = new_total_qty
        pos["adds_count"] = int(pos.get("adds_count", 0)) + 1
        pos["initial_risk_per_share"] = float(new_avg - stop_price)

        _update_stop(ibkr, symbol, pos, pos["stop_price"])

        notify("PYRAMID", f"{symbol}: added {add_qty} @ ~{current_price:.2f}, avg {new_avg:.2f}")
        log_decision("pyramid", symbol, f"added {add_qty}, avg_entry now {new_avg:.2f}")

    save_positions(json_positions)


def force_close_all(ibkr: IBKRClient, json_positions: dict) -> None:
    """§8 - cancel protective stops and flatten everything at market for EOD."""
    notify(
        "EOD FORCE CLOSE",
        "Cycle initiating EOD close — cancelling stops + market close all",
    )
    for symbol, pos in list(json_positions.items()):
        _cancel_order(ibkr, pos.get("stop_order_id"))
        qty = int(pos["qty"])
        try:
            ibkr.ib.placeOrder(
                Stock(symbol, "SMART", "USD"), MarketOrder("SELL", qty, tif="DAY")
            )
            ibkr.ib.sleep(1)
        except Exception as exc:  # noqa: BLE001 - one failure must not block the rest
            log_decision("force_close_error", symbol, f"market close failed: {exc}")
    ibkr.ib.sleep(5)
    json_positions.clear()
    save_positions({})
    log_decision("force_close_cycle", "ALL", "EOD force close completed by cycle")


def entry_scan(
    ibkr: IBKRClient,
    json_positions: dict,
    broker_positions: dict,
    regime_ok: bool,
) -> None:
    """§Fix2/3/5 - scan the watchlist for new entries within all limits."""
    rules = load_rules()
    risk_cfg = rules["risk"]

    # Skip the whole scan before the strategy's earliest-entry time — otherwise
    # every 09:00–10:05 tick evaluates the full watchlist just to log
    # outside_time_window per candidate (1,200+ noise rows in the first week).
    earliest = datetime.strptime(
        rules["time_filter"]["earliest_entry_et"], "%H:%M"
    ).time()
    if datetime.now(ET).time() < earliest:
        return

    today_buys = count_todays_buys()
    max_daily = int(risk_cfg["max_trades_per_day"])
    if today_buys >= max_daily:
        log_decision("skipped_all", "DAILY_LIMIT", f"{today_buys}/{max_daily} reached")
        return

    max_concurrent = int(risk_cfg["max_concurrent_positions"])
    if len(json_positions) >= max_concurrent:
        log_decision(
            "skipped_all", "MAX_POSITIONS", f"{len(json_positions)}/{max_concurrent}"
        )
        return

    if not WATCHLIST_PATH.exists():
        log_decision("skipped_all", "WATCHLIST", "watchlist.txt missing")
        return
    candidates = [t.strip() for t in WATCHLIST_PATH.read_text().splitlines() if t.strip()]
    gap_syms: set[str] = set(candidates)

    # EXPERIMENT A: fold in AI news-catalyst names (news_watchlist.py). Kept in a
    # SEPARATE file so the 30-min prefilter overwrite of watchlist.txt never
    # clobbers them. Additive only — these names still clear the same D1/I2 gates.
    # Logged (tag NEWS) so AI-sourced picks can be A/B'd against gap-sourced ones.
    news_wl = ROOT / "news_watchlist.txt"
    news_syms: set[str] = set()
    if news_wl.exists():
        news_syms = {t.strip() for t in news_wl.read_text().splitlines() if t.strip()}
        news_added = [t for t in news_syms if t not in candidates]
        if news_added:
            candidates += news_added
            log_decision("news_candidates", "NEWS", ",".join(news_added))

    broker_syms = set(broker_positions.keys())

    sector_counts: dict[str, int] = {}
    for pos in json_positions.values():
        sec = pos.get("sector", "Unknown")
        sector_counts[sec] = sector_counts.get(sec, 0) + 1
    max_per_sector = int(risk_cfg["max_positions_per_sector"])

    portfolio_val = float(risk_cfg["portfolio_value_usd"])
    max_risk_pct = float(risk_cfg["max_risk_per_trade_pct"]) / 100
    max_trade_usd = float(risk_cfg["max_trade_size_usd"])
    slippage = float(risk_cfg["entry_slippage_cap_pct"]) / 100
    # Base per-trade notional cap (rule sizing); amplified ceiling is what the AI
    # judge may push up to in live mode. Total-exposure cap bounds the aggregate.
    max_position_pct = float(risk_cfg.get("max_position_pct", 0.10))
    max_position_pct_amp = float(risk_cfg.get("max_position_pct_amplified", 0.15))
    total_exposure_cap = portfolio_val * float(risk_cfg.get("total_exposure_pct", 1.0))
    exposure = sum(
        float(p.get("qty", 0)) * float(p.get("avg_entry_price", 0))
        for p in json_positions.values()
    )

    new_entries = 0
    for symbol in candidates:
        if len(json_positions) + new_entries >= max_concurrent:
            break
        if symbol in broker_syms:
            log_decision("skipped", symbol, "already_holding_broker")
            continue

        sector = get_sector(symbol)
        if sector_counts.get(sector, 0) >= max_per_sector:
            log_decision("skipped", symbol, f"sector_limit: {sector} at {max_per_sector}")
            continue

        existing = [{"symbol": s, "qty": p["qty"]} for s, p in json_positions.items()]
        result = evaluate(
            symbol, ibkr, existing_positions=existing, rules=rules, regime_ok=regime_ok
        )
        if not result["pass"]:
            continue  # evaluate() already logged the reason

        snapshot_price = float(result["price"])
        stop_price = float(result["stop_price"])

        risk_per_share = snapshot_price - stop_price
        if risk_per_share <= 0:
            log_decision("skipped", symbol, "invalid_stop: risk_per_share <= 0")
            continue

        risk_budget = portfolio_val * max_risk_pct
        size_by_risk = int(risk_budget / risk_per_share)
        size_by_pct = int((portfolio_val * max_position_pct) / snapshot_price)
        size_by_max = int(max_trade_usd / snapshot_price)
        size = min(size_by_risk, size_by_pct, size_by_max)
        if size <= 0:
            log_decision("skipped", symbol, "size_zero")
            continue

        # --- LLM overlay vetting layer (docs/LLM_OVERLAY_SPEC.md) ----------- #
        # Fires only when rules.json llm_overlay.enabled. dry-run: logs a verdict
        # but does not change the trade. live: may veto (reject) or shrink size,
        # never amplify. Any failure here logs and proceeds rule-only — this
        # layer can never break the trading path.
        overlay = rules.get("llm_overlay", {})
        if overlay.get("enabled"):
            try:
                import llm_overlay

                candidate = {
                    "symbol": symbol,
                    "sector": sector,
                    "price": snapshot_price,
                    "gap_pct": result.get("gap_pct"),
                    "prior_close": result.get("prior_close"),
                    "today_open": result.get("today_open"),
                    "today_hod": result.get("today_hod"),
                    "ext_above_open_pct": result.get("ext_above_open_pct"),
                    "stop_price": stop_price,
                    "rvol": result.get("rvol"),
                    "above_sma200": True,  # D2 gate passed → prior close above SMA200
                    "spy_regime": "above_sma200" if regime_ok else "below_sma200",
                    "tech": result.get("tech"),  # multi-day extension/momentum context
                }
                verdict = llm_overlay.vet(candidate)
                if overlay.get("mode") == "live":
                    if verdict.get("verdict") == "reject":
                        log_decision("llm_reject", symbol, verdict.get("primary_reason", ""))
                        continue
                    # (a) AI-adjusted stop within bounds → recompute risk-based size.
                    stop_mult = float(verdict.get("stop_multiplier", 1.0) or 1.0)
                    rule_risk = snapshot_price - stop_price
                    adj_stop = round(snapshot_price - rule_risk * stop_mult, 2)
                    if 0 < adj_stop < snapshot_price:
                        stop_price = adj_stop
                        risk_per_share = snapshot_price - stop_price
                        size_by_risk = int(risk_budget / risk_per_share)
                    # (b) Bidirectional size multiplier, then re-cap to hard ceilings
                    #     (amplified notional % and absolute $ cap) — clamp guarantees
                    #     mult is already inside [min_size_mult, max_amplify_size_mult].
                    size_mult = float(verdict.get("size_multiplier", 1.0) or 1.0)
                    base = min(size_by_risk, size_by_pct, size_by_max)
                    hard = int((portfolio_val * max_position_pct_amp) / snapshot_price)
                    size = min(int(base * size_mult), hard, int(max_trade_usd / snapshot_price))
                    if size <= 0:
                        log_decision("llm_reject", symbol, "size rounded to 0")
                        continue
                    if size_mult != 1.0 or stop_mult != 1.0:
                        log_decision(
                            "llm_live",
                            symbol,
                            f"size x{size_mult}->{size} stop x{stop_mult}@{stop_price} "
                            f":: {verdict.get('primary_reason', '')}",
                        )
                else:  # dry-run: record only, trade per rules
                    log_decision(
                        "llm_dryrun",
                        symbol,
                        f"{verdict.get('verdict')} conf={verdict.get('confidence')} "
                        f"x{verdict.get('size_multiplier')} :: {verdict.get('primary_reason', '')}",
                    )
            except Exception as exc:  # noqa: BLE001 - overlay must never break trading
                log_decision("llm_error", symbol, f"overlay failed, rule-only: {exc}")

        # Total-exposure cap: never let aggregate notional exceed the ceiling.
        # Trim the order to whatever room remains; skip if none.
        if exposure + size * snapshot_price > total_exposure_cap:
            room_shares = int((total_exposure_cap - exposure) / snapshot_price)
            if room_shares <= 0:
                log_decision("skipped", symbol, "total_exposure_cap reached")
                continue
            log_decision("exposure_trim", symbol, f"{size}->{room_shares} (exposure cap)")
            size = room_shares

        limit_price = round(snapshot_price * (1 + slippage), 2)

        # Intent written BEFORE the order: if trade.py loses track of the fill,
        # reconcile adopts it with this stop instead of the blind 1% default.
        _note_pending_entry(symbol, size, stop_price)
        ret = _spawn_trade(symbol, "BUY", size, limit_price)
        if ret != 0:
            log_decision("trade_failed", symbol, f"trade.py exited {ret}")
            continue

        fill_price = _get_last_fill_price(symbol)
        if fill_price is None:
            log_decision("trade_unclear", symbol, "could not confirm fill from trades.csv")
            continue

        stop_trade = ibkr.place_stop_order(symbol, "SELL", size, stop_price)
        stop_order_id = int(stop_trade.order.orderId)
        stop_perm_id = int(stop_trade.order.permId)

        # A/B tag for Experiment A: which scanner sourced this name. "news+gap"
        # kept separate from pure "news" — names the gap scanner would have
        # traded anyway must not inflate the news arm.
        if symbol in news_syms and symbol in gap_syms:
            source = "news+gap"
        elif symbol in news_syms:
            source = "news"
        else:
            source = "gap"

        initial_risk = fill_price - stop_price
        json_positions[symbol] = {
            "symbol": symbol,
            "qty": int(size),
            "avg_entry_price": float(fill_price),
            "stop_price": float(stop_price),
            "stop_order_id": int(stop_order_id),
            "stop_perm_id": int(stop_perm_id),
            "sector": str(result["sector"]),
            "entry_time": datetime.now(ET).isoformat(),
            "adds_count": 0,
            "initial_risk_per_share": float(initial_risk),
            "partial_done": False,
            "breakeven_done": False,
            "source": source,
        }
        save_positions(json_positions)
        _clear_pending_entry(symbol)  # booked cleanly — intent no longer needed
        # today_decisions.py counts event=="entry" — without this line every
        # clean fill was invisible to the funnel report (found 2026-07-02)
        log_decision("entry", symbol,
                     f"filled {int(size)}@{fill_price}, stop @{stop_price} ({source})")

        sector_counts[sector] = sector_counts.get(sector, 0) + 1
        exposure += int(size) * float(fill_price)
        new_entries += 1

        notify("ENTRY", f"{symbol} BUY {size} @ {fill_price:.2f}, stop {stop_price:.2f} [{source}]")
        log_decision(
            "entry",
            symbol,
            f"filled {size}@{fill_price:.2f} stop={stop_price:.2f} source={source}",
        )


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def run_cycle(ibkr: IBKRClient, status: str) -> None:
    json_positions = load_positions()

    # 2. Reconcile broker vs json (also yields current broker positions).
    broker_positions = reconcile(ibkr, json_positions)

    # 3. Regime check (gates new entries only).
    regime_ok = check_regime(ibkr)

    # 4. Stop-out detection (orderId match, not quantity).
    check_stop_outs(ibkr, json_positions)

    # 5+6. Manage open positions, then pyramid — share one snapshot price per
    # symbol for this tick instead of each stage polling IBKR separately.
    # Per-symbol try/except: one dead quote must not crash the whole cycle
    # (that would skip stop-out detection and management for every OTHER
    # position too — same failure class as the check_regime SPY fix). A symbol
    # without a price is simply skipped this tick; its broker-side stop stays live.
    prices: dict[str, float] = {}
    for symbol in json_positions:
        try:
            prices[symbol] = ibkr.get_snapshot_price(symbol)
        except Exception as exc:  # noqa: BLE001 - skip management for this tick
            log_decision("price_unavailable", symbol, f"skipping manage this tick: {exc}")
    manage_positions(ibkr, json_positions, prices)
    pyramid_check(ibkr, json_positions, prices)

    # 8. EOD force close.
    if status == "force_close":
        force_close_all(ibkr, json_positions)
        ibkr.disconnect()
        sys.exit(0)

    # 9. Manage-only window: management already done, stop here.
    if status == "manage_only":
        save_positions(json_positions)
        ibkr.disconnect()
        sys.exit(0)

    # 10. Entry scan (only in the active window, and only if regime allows).
    if status == "ok" and regime_ok:
        entry_scan(ibkr, json_positions, broker_positions, regime_ok)

    # 11. Final save.
    save_positions(json_positions)


def main() -> None:
    load_dotenv(ROOT / ".env")
    safety_lock()

    status = time_gate()
    if status in ("weekend", "too_early", "closed"):
        print(f"[{status}] exiting early")
        sys.exit(0)

    host = os.environ.get("IBKR_HOST", "127.0.0.1")
    port = int(os.environ["IBKR_PORT"])
    client_id = int(os.environ.get("IBKR_CLIENT_ID", "1"))

    ibkr: IBKRClient | None = None
    try:
        ibkr = IBKRClient(host=host, port=port, client_id=client_id)
        run_cycle(ibkr, status)
        ibkr.disconnect()
    except SystemExit:
        if ibkr is not None:
            try:
                ibkr.disconnect()
            except Exception:  # noqa: BLE001
                pass
        raise
    except Exception as exc:  # noqa: BLE001 - last-resort crash handler
        log_crash(exc)
        notify("Cycle CRASHED", f"{type(exc).__name__}: {exc}")
        if ibkr is not None:
            try:
                ibkr.disconnect()
            except Exception:  # noqa: BLE001
                pass
        sys.exit(1)


if __name__ == "__main__":
    main()
