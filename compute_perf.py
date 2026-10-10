#!/usr/bin/env python3
"""Performance analytics for the IBKR paper-trading bot (SPEC §13).

Pure analytics: reads trades.csv, pairs trades FIFO, computes metrics,
renders a static Bootstrap dashboard and sends a Telegram daily summary.
Does NOT connect to IBKR.
"""
from __future__ import annotations

import csv
import json
import os
from collections import deque
from dataclasses import dataclass
from datetime import datetime, date
from pathlib import Path
from zoneinfo import ZoneInfo

from src.notify import notify

ROOT = Path(__file__).resolve().parent
TRADES_CSV = ROOT / "trades.csv"
OPEN_POSITIONS_JSON = ROOT / "open_positions.json"
DASHBOARD_DIR = ROOT / "dashboard"
DASHBOARD_HTML = DASHBOARD_DIR / "index.html"

SAMPLE_TARGET = 50
ASSUMED_RISK_PCT = 0.01
TZ = ZoneInfo("America/New_York")
BOOTSTRAP_CSS = "https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css"


def load_env() -> None:
    """Minimal .env loader (KEY=VALUE lines) from the project root."""
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def parse_ts(value: str) -> datetime:
    dt = datetime.fromisoformat(value.strip())
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    return dt


@dataclass
class Fill:
    timestamp: datetime
    symbol: str
    side: str
    size: float
    fill_price: float
    order_id: str
    limit_price: float | None


@dataclass
class Pair:
    symbol: str
    buy: Fill
    sell: Fill
    size: float
    pnl_usd: float
    hold_minutes: float
    risk_usd: float
    r_multiple: float | None


def read_trades(path: Path) -> list[Fill]:
    if not path.exists():
        return []
    fills: list[Fill] = []
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            status = (row.get("status") or "").strip().lower()
            if status and status not in ("filled", "fill", "done"):
                continue
            try:
                fill_price = float(row["fill_price"])
                size = float(row["size"])
            except (KeyError, ValueError, TypeError):
                continue
            limit_raw = (row.get("limit_price") or "").strip()
            try:
                limit_price = float(limit_raw) if limit_raw else None
            except ValueError:
                limit_price = None
            fills.append(
                Fill(
                    timestamp=parse_ts(row["timestamp"]),
                    symbol=(row.get("symbol") or "").strip().upper(),
                    side=(row.get("side") or "").strip().upper(),
                    size=size,
                    fill_price=fill_price,
                    order_id=(row.get("order_id") or "").strip(),
                    limit_price=limit_price,
                )
            )
    fills.sort(key=lambda f: f.timestamp)
    return fills


def load_open_positions() -> dict:
    if not OPEN_POSITIONS_JSON.exists():
        return {}
    try:
        data = json.loads(OPEN_POSITIONS_JSON.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    if isinstance(data, list):
        return {str(p.get("symbol", i)): p for i, p in enumerate(data)}
    if isinstance(data, dict):
        return data
    return {}


def stop_for(positions: dict, symbol: str) -> float | None:
    pos = positions.get(symbol)
    if not isinstance(pos, dict):
        return None
    for key in ("stop", "stop_price", "stopPrice"):
        if key in pos:
            try:
                return float(pos[key])
            except (ValueError, TypeError):
                return None
    return None


def pair_trades(fills: list[Fill], positions: dict) -> list[Pair]:
    """Match BUY fills to SELL fills per symbol, chronological FIFO."""
    open_buys: dict[str, deque[Fill]] = {}
    pairs: list[Pair] = []
    for fill in fills:
        if fill.side == "BUY":
            open_buys.setdefault(fill.symbol, deque()).append(fill)
        elif fill.side == "SELL":
            queue = open_buys.get(fill.symbol)
            if not queue:
                continue
            buy = queue.popleft()
            size = min(buy.size, fill.size)
            pnl = (fill.fill_price - buy.fill_price) * size
            hold = (fill.timestamp - buy.timestamp).total_seconds() / 60.0

            stop = stop_for(positions, fill.symbol)
            if stop is not None and stop > 0:
                risk = abs(buy.fill_price - stop) * size
            else:
                risk = buy.fill_price * ASSUMED_RISK_PCT * size
            r_mult = pnl / risk if risk > 0 else None

            pairs.append(
                Pair(
                    symbol=fill.symbol,
                    buy=buy,
                    sell=fill,
                    size=size,
                    pnl_usd=pnl,
                    hold_minutes=hold,
                    risk_usd=risk,
                    r_multiple=r_mult,
                )
            )
    return pairs


def aggregate(pairs: list[Pair]) -> dict:
    total = len(pairs)
    wins = [p for p in pairs if p.pnl_usd > 0]
    losses = [p for p in pairs if p.pnl_usd < 0]
    gross = sum(p.pnl_usd for p in pairs)
    sum_wins = sum(p.pnl_usd for p in wins)
    sum_losses = sum(p.pnl_usd for p in losses)
    profit_factor = (sum_wins / abs(sum_losses)) if sum_losses else None
    return {
        "total_trades": total,
        "win_rate": (len(wins) / total) if total else 0.0,
        "gross_pnl": gross,
        "max_win": max((p.pnl_usd for p in pairs), default=0.0),
        "max_loss": min((p.pnl_usd for p in pairs), default=0.0),
        "profit_factor": profit_factor,
        "sample_target": SAMPLE_TARGET,
        "wins": len(wins),
        "losses": len(losses),
    }


R_BUCKETS = [
    ("<-2R", lambda r: r < -2),
    ("-2 to -1R", lambda r: -2 <= r < -1),
    ("-1 to 0R", lambda r: -1 <= r < 0),
    ("0 to 1R", lambda r: 0 <= r < 1),
    ("1 to 2R", lambda r: 1 <= r < 2),
    (">2R", lambda r: r >= 2),
]


def r_distribution(pairs: list[Pair]) -> list[tuple[str, int]]:
    counts = [0] * len(R_BUCKETS)
    for p in pairs:
        if p.r_multiple is None:
            continue
        for i, (_, test) in enumerate(R_BUCKETS):
            if test(p.r_multiple):
                counts[i] += 1
                break
    return [(label, counts[i]) for i, (label, _) in enumerate(R_BUCKETS)]


def esc(value) -> str:
    s = "" if value is None else str(value)
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def fmt_r(r: float | None) -> str:
    return f"{r:+.2f}R" if r is not None else "—"


def render_positions(positions: dict) -> str:
    if not positions:
        return '<p class="text-muted">No open positions</p>'
    rows = []
    for symbol, pos in positions.items():
        if not isinstance(pos, dict):
            continue
        entry = pos.get("avg_entry", pos.get("entry", pos.get("fill_price", "—")))
        unreal = pos.get("unrealized_pnl", pos.get("pnl", "—"))
        stop = pos.get("stop", pos.get("stop_price", "—"))
        rows.append(
            f"<tr><td>{esc(symbol)}</td><td>{esc(entry)}</td>"
            f"<td>{esc(unreal)}</td><td>{esc(stop)}</td>"
            f"<td><span class='badge bg-warning text-dark'>open, unrealized</span></td></tr>"
        )
    return f"""
    <table class="table table-sm table-bordered">
      <thead class="table-light"><tr>
        <th>Symbol</th><th>Avg Entry</th><th>Current P&amp;L</th><th>Stop Price</th><th>Status</th>
      </tr></thead>
      <tbody>{''.join(rows)}</tbody>
    </table>
    <p class="text-muted small">Live prices not available in static report — see Telegram snapshots</p>
    """


def render_today(pairs: list[Pair], today: date) -> str:
    todays = [p for p in pairs if p.sell.timestamp.astimezone(TZ).date() == today]
    if not todays:
        return '<p class="text-muted">No closed trades today</p>'
    rows = []
    for p in todays:
        cls = "table-success" if p.pnl_usd > 0 else ("table-danger" if p.pnl_usd < 0 else "")
        rows.append(
            f"<tr class='{cls}'><td>{esc(p.symbol)}</td>"
            f"<td>{p.pnl_usd:+.2f}</td><td>{fmt_r(p.r_multiple)}</td></tr>"
        )
    return f"""
    <table class="table table-sm table-bordered">
      <thead class="table-light"><tr><th>Symbol</th><th>P&amp;L ($)</th><th>R</th></tr></thead>
      <tbody>{''.join(rows)}</tbody>
    </table>
    """


def render_r_dist(dist: list[tuple[str, int]]) -> str:
    rows = []
    for label, count in dist:
        bar = "█" * count
        rows.append(
            f"<tr><td>{esc(label)}</td><td><code>{bar}</code></td><td>{count}</td></tr>"
        )
    return f"""
    <table class="table table-sm table-bordered">
      <thead class="table-light"><tr><th>Bucket</th><th>Distribution</th><th>Count</th></tr></thead>
      <tbody>{''.join(rows)}</tbody>
    </table>
    """


def render_recent(pairs: list[Pair]) -> str:
    recent = pairs[-20:][::-1]
    if not recent:
        return '<p class="text-muted">No closed trades yet</p>'
    rows = []
    for p in recent:
        cls = "table-success" if p.pnl_usd > 0 else ("table-danger" if p.pnl_usd < 0 else "")
        ts = p.sell.timestamp.astimezone(TZ).strftime("%Y-%m-%d %H:%M")
        rows.append(
            f"<tr class='{cls}'>"
            f"<td>{esc(ts)}</td><td>{esc(p.symbol)}</td><td>SELL</td>"
            f"<td>{p.size:g}</td><td>{p.sell.fill_price:.4f}</td>"
            f"<td>{p.pnl_usd:+.2f}</td><td>{fmt_r(p.r_multiple)}</td></tr>"
        )
    return f"""
    <table class="table table-sm table-bordered">
      <thead class="table-light"><tr>
        <th>Timestamp</th><th>Symbol</th><th>Side</th><th>Size</th>
        <th>Fill</th><th>P&amp;L ($)</th><th>R</th>
      </tr></thead>
      <tbody>{''.join(rows)}</tbody>
    </table>
    """


def render_sample(total: int) -> str:
    if total < SAMPLE_TARGET:
        banner = (
            '<div class="alert alert-danger" role="alert">'
            "⚠ Insufficient sample — do not judge edge</div>"
        )
    else:
        banner = (
            '<div class="alert alert-success" role="alert">'
            "✓ Minimum sample reached</div>"
        )
    return f'<p>Sample progress: {total} / {SAMPLE_TARGET} trades</p>{banner}'


def render_html(stats: dict, pairs: list[Pair], positions: dict,
                dist: list[tuple[str, int]], generated: datetime) -> str:
    pf = stats["profit_factor"]
    pf_str = f"{pf:.2f}" if pf is not None else "—"
    today = generated.astimezone(TZ).date()
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Trading Bot Performance</title>
  <link href="{BOOTSTRAP_CSS}" rel="stylesheet">
</head>
<body class="bg-light">
<div class="container py-4">
  <h1 class="mb-1">Trading Bot Performance</h1>
  <p class="text-muted">Generated {esc(generated.astimezone(TZ).strftime('%Y-%m-%d %H:%M %Z'))}</p>

  <div class="row g-3 mb-4">
    <div class="col"><div class="card text-center"><div class="card-body">
      <div class="h4 mb-0">{stats['total_trades']}</div><small>Closed Trades</small></div></div></div>
    <div class="col"><div class="card text-center"><div class="card-body">
      <div class="h4 mb-0">{stats['win_rate']:.0%}</div><small>Win Rate</small></div></div></div>
    <div class="col"><div class="card text-center"><div class="card-body">
      <div class="h4 mb-0">{stats['gross_pnl']:+.2f}</div><small>Gross P&amp;L</small></div></div></div>
    <div class="col"><div class="card text-center"><div class="card-body">
      <div class="h4 mb-0">{pf_str}</div><small>Profit Factor</small></div></div></div>
    <div class="col"><div class="card text-center"><div class="card-body">
      <div class="h4 mb-0 text-success">{stats['max_win']:+.2f}</div><small>Max Win</small></div></div></div>
    <div class="col"><div class="card text-center"><div class="card-body">
      <div class="h4 mb-0 text-danger">{stats['max_loss']:+.2f}</div><small>Max Loss</small></div></div></div>
  </div>

  <h2 class="h4">Current Positions</h2>
  {render_positions(positions)}

  <h2 class="h4 mt-4">Today's Realized P&amp;L</h2>
  {render_today(pairs, today)}

  <h2 class="h4 mt-4">R Distribution</h2>
  {render_r_dist(dist)}

  <h2 class="h4 mt-4">Recent 20 Trades</h2>
  {render_recent(pairs)}

  <h2 class="h4 mt-4">Sample Progress</h2>
  {render_sample(stats['total_trades'])}
</div>
</body>
</html>
"""


def main() -> None:
    load_env()
    positions = load_open_positions()
    fills = read_trades(TRADES_CSV)
    pairs = pair_trades(fills, positions)
    stats = aggregate(pairs)
    dist = r_distribution(pairs)
    generated = datetime.now(TZ)

    DASHBOARD_DIR.mkdir(parents=True, exist_ok=True)
    DASHBOARD_HTML.write_text(
        render_html(stats, pairs, positions, dist, generated), encoding="utf-8"
    )

    summary = (
        f"{stats['total_trades']} trades, "
        f"{stats['win_rate']:.0%} WR, P&L: {stats['gross_pnl']:+.2f}"
    )
    notify("Daily Summary", summary)

    pf = stats["profit_factor"]
    pf_str = f"{pf:.2f}" if pf is not None else "n/a"
    print("=== Performance Summary ===")
    print(f"Closed trades : {stats['total_trades']}")
    print(f"Win rate      : {stats['win_rate']:.1%} ({stats['wins']}W / {stats['losses']}L)")
    print(f"Gross P&L     : {stats['gross_pnl']:+.2f}")
    print(f"Max win       : {stats['max_win']:+.2f}")
    print(f"Max loss      : {stats['max_loss']:+.2f}")
    print(f"Profit factor : {pf_str}")
    print(f"Sample        : {stats['total_trades']} / {SAMPLE_TARGET}")
    print(f"Dashboard     : {DASHBOARD_HTML}")


if __name__ == "__main__":
    main()
