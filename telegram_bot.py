#!/usr/bin/env python3
"""On-demand Telegram command listener (read-only) — serves the V2 thesis system.

A long-running process (kept alive by systemd unit ibkr-telegram-bot.service)
that long-polls getUpdates and replies to a fixed set of read-only commands.
It places NO orders and runs NO arbitrary shell — every command maps to a
hard-coded read-only handler, and messages are ignored unless they come from
the configured TELEGRAM_CHAT_ID.

2026-07-07: all commands migrated from V1 (IBKR paper bot, retired to silent
sample-collection) to V2 (thesis_monitor/ virtual book). V1 data remains
reachable only via /experiment (實驗A的累積數據).

Commands:
  /positions /pos /持倉   → V2 virtual-book holdings, live P&L (yfinance)
  /pnl      /損益         → book vs SPY same-period benchmark
  /today    /今日         → today's V2 fills + book line
  /status   /狀態         → engine health: evidence freshness, falsifiers, last run
  /summary  /摘要         → latest evidence per ticker + falsifier proximity
  /experiment /exp /實驗  → V1 experiment-A cumulative stats (legacy)
  /help     /start        → this list
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent
TM_DIR = PROJECT_ROOT / "thesis_monitor"
BOOK_FILE = TM_DIR / "portfolio.json"
TRADES_FILE = TM_DIR / "trades.jsonl"
HIST_FILE = TM_DIR / "portfolio_history.jsonl"
EVIDENCE_DIR = TM_DIR / "evidence"
THESES_FILE = TM_DIR / "theses.yaml"
ET = ZoneInfo("America/New_York")
START_CASH = 100_000.0

load_dotenv(PROJECT_ROOT / ".env")

from src.notify import notify  # noqa: E402  (after load_dotenv)

BASE = f"https://api.telegram.org/bot{os.environ.get('TELEGRAM_BOT_TOKEN', '')}"
CHAT_ID = str(os.environ.get("TELEGRAM_CHAT_ID", ""))

HELP = (
    "🤖 V2 thesis bot 指令\n"
    "/positions（/pos /持倉）— 虛擬帳本持倉、即時損益\n"
    "/pnl（/損益）— 帳本 vs SPY 同期\n"
    "/today（/今日）— 今日成交 + 帳本\n"
    "/status（/狀態）— 引擎健康：evidence 新鮮度、falsifier、最後執行\n"
    "/summary（/摘要）— 最新 evidence 各票狀態\n"
    "/experiment（/exp /實驗）— V1 實驗A累積數據（舊系統）\n"
    "/help — 指令列表"
)


# --- V2 data access ---------------------------------------------------------- #
def _book() -> dict | None:
    try:
        return json.loads(BOOK_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def _live_price(tk: str) -> float | None:
    try:
        import yfinance as yf

        t = yf.Ticker(tk)
        # prepost minute bars first so pre/after-hours moves show up;
        # daily close as fallback (minute data is spottier, e.g. non-US listings)
        try:
            h = t.history(period="1d", interval="1m", prepost=True)
            if len(h):
                return float(h["Close"].iloc[-1])
        except Exception:  # noqa: BLE001
            pass
        h = t.history(period="2d")
        return float(h["Close"].iloc[-1]) if len(h) else None
    except Exception:  # noqa: BLE001
        return None


def _mark(book: dict) -> tuple[float, list]:
    """Best-effort live mark-to-market. Falls back to the book's last_prices
    (updated by the nightly executor) and finally avg_cost."""
    rows = []
    eq = book["cash"]
    for tk, p in book["positions"].items():
        px = _live_price(tk) or book["last_prices"].get(tk) or p["avg_cost"]
        val = p["shares"] * px
        eq += val
        pnl = (px / p["avg_cost"] - 1) * 100
        rows.append((tk, p, px, val, pnl))
    return eq, rows


def _active_theses() -> list[dict]:
    import yaml

    return [t for t in yaml.safe_load(THESES_FILE.read_text(encoding="utf-8")) or []
            if t.get("status") == "active"]


def _latest_evidence(thesis_id: str) -> dict | None:
    f = EVIDENCE_DIR / f"{thesis_id}.jsonl"
    if not f.exists():
        return None
    try:
        return json.loads(f.read_text(encoding="utf-8").strip().splitlines()[-1])
    except Exception:  # noqa: BLE001
        return None


def _trades(day: str | None = None) -> list[dict]:
    if not TRADES_FILE.exists():
        return []
    out = []
    for ln in TRADES_FILE.read_text(encoding="utf-8").strip().splitlines():
        try:
            t = json.loads(ln)
        except Exception:  # noqa: BLE001
            continue
        if day is None or t.get("ts", "")[:10] == day:
            out.append(t)
    return out


# --- handlers ----------------------------------------------------------------- #
def cmd_positions() -> None:
    book = _book()
    if book is None:
        notify("📊 V2 持倉", "讀不到 thesis_monitor/portfolio.json")
        return
    eq, rows = _mark(book)
    if not rows:
        notify("📊 V2 持倉", f"目前空手\n現金 ${book['cash']:,.0f}")
        return
    lines = []
    total_cost = 0.0
    for tk, p, px, val, pnl in rows:
        cost = p["shares"] * p["avg_cost"]
        total_cost += cost
        lines.append(f"{tk} {p['shares']:g}股（{p.get('opened', '?')} 起）")
        lines.append(f"  買入 {p['avg_cost']:.2f}｜現價 {px:.2f}")
        lines.append(f"  市值 ${cost:,.0f} → ${val:,.0f}（{val - cost:+,.0f}$，{pnl:+.1f}%）")
    pos_pnl = (eq - book["cash"]) - total_cost
    lines.append(f"持倉合計 ${total_cost:,.0f} → ${eq - book['cash']:,.0f}（{pos_pnl:+,.0f}$）")
    lines.append(f"現金 ${book['cash']:,.0f}｜總值 ${eq:,.0f}"
                 f"（{eq - START_CASH:+,.0f}$，{(eq / START_CASH - 1) * 100:+.2f}%）")
    notify("📊 V2 持倉", "\n".join(lines))


def cmd_pnl() -> None:
    book = _book()
    if book is None:
        notify("📈 V2 損益", "讀不到 thesis_monitor/portfolio.json")
        return
    eq, _ = _mark(book)
    lines = [f"帳本 ${eq:,.0f}（{eq - START_CASH:+,.0f}$，{(eq / START_CASH - 1) * 100:+.2f}%"
             f" 自 {book.get('start_date')}）"]
    spy_now = _live_price("SPY")
    if spy_now and book.get("spy_start_price"):
        spy_val = START_CASH * spy_now / book["spy_start_price"]
        lines.append(f"SPY 同期 ${spy_val:,.0f}（{spy_val - START_CASH:+,.0f}$，"
                     f"{(spy_val / START_CASH - 1) * 100:+.2f}%）")
        lines.append(f"vs SPY：{eq - spy_val:+,.0f}$")
    else:
        lines.append("（SPY 即時報價暫不可用）")
    notify("📈 V2 損益", "\n".join(lines))


def cmd_today() -> None:
    day = datetime.now(ET).strftime("%Y-%m-%d")
    fills = _trades(day)
    lines = [f"⚙ {t['side']} {t['ticker']} {t['shares']:g}股 @ {t['price']:.2f}"
             f"（${t['shares'] * t['price']:,.0f}）— {t['machine_reason']}"
             for t in fills] or ["今日無成交（無狀態轉換＝正確地什麼都不做）"]
    book = _book()
    if book is not None:
        eq, _ = _mark(book)
        lines.append(f"帳本 ${eq:,.0f}（{eq - START_CASH:+,.0f}$）｜現金 ${book['cash']:,.0f}"
                     f"｜持倉 {len(book['positions'])} 檔")
    notify("💰 V2 今日", "\n".join(lines))


def cmd_status() -> None:
    now = datetime.now(ET)
    today = now.strftime("%Y-%m-%d")
    lines = [f"🩺 V2 狀態 {now.strftime('%Y-%m-%d %H:%M ET')}"]
    try:
        theses = _active_theses()
    except Exception as exc:  # noqa: BLE001
        theses = []
        lines.append(f"⚠ theses.yaml 讀取失敗: {exc}")
    for th in theses:
        ev = _latest_evidence(th["id"])
        if ev is None:
            lines.append(f"⚠ {th['id']}: 無 evidence 檔")
            continue
        d = ev.get("ts", "?")[:10]
        fresh = d == today
        lines.append(f"{'✅' if fresh else '⚠'} {th['id']}: evidence {d}"
                     + ("" if fresh else "（非今日；美股休市日屬正常）"))
        if ev.get("health", {}).get("entries_blocked"):
            lines.append("⛔ 新進場已鎖（資料健康檢查未過）")
        prox = ev.get("falsifier_proximity", {})
        trig = [k for k, v in prox.items() if v.get("status") == "triggered"]
        warn = [k for k, v in prox.items() if v.get("status") == "warning"]
        if trig:
            lines.append("🔴 falsifier 觸發: " + ", ".join(trig))
        if warn:
            lines.append("🟡 falsifier 警告: " + ", ".join(warn))
    book = _book()
    if book is not None:
        if book.get("falsified"):
            lines.append("☠ 已終結: " + ", ".join(f"{k}({v})" for k, v in book["falsified"].items()))
        lines.append(f"持倉 {len(book['positions'])} 檔｜現金 ${book['cash']:,.0f}")
    try:
        last = json.loads(HIST_FILE.read_text(encoding="utf-8").strip().splitlines()[-1])
        lines.append(f"最後執行: {last['ts'][:16]}｜equity ${last['equity']:,.0f}")
    except Exception:  # noqa: BLE001
        lines.append("最後執行: 無紀錄")
    notify("🩺 V2 狀態", "\n".join(lines))


def cmd_summary() -> None:
    lines: list[str] = []
    try:
        theses = _active_theses()
    except Exception as exc:  # noqa: BLE001
        notify("📊 V2 摘要", f"theses.yaml 讀取失敗: {exc}")
        return
    for th in theses:
        ev = _latest_evidence(th["id"])
        if ev is None:
            lines.append(f"▌{th['id']}: 無 evidence 紀錄")
            continue
        lines.append(f"▌{th['id']} — {ev.get('ts', '?')[:10]}")
        for tk, d in ev.get("tickers", {}).items():
            state = d.get("state", "?")
            conf = "✓" if d.get("confirmed") else "未滿3日"
            dist = d.get("pct_from_high")
            lines.append(f"{tk}: {state}({conf})"
                         + (f" 距高 {dist:+.0f}%" if dist is not None else ""))
        for fid, p in ev.get("falsifier_proximity", {}).items():
            icon = {"ok": "🟢", "warning": "🟡", "triggered": "🔴"}.get(p.get("status"), "❔")
            lines.append(f"{icon} {fid}: {p.get('why', '')}")
    book = _book()
    if book is not None:
        eq, _ = _mark(book)
        lines.append(f"帳本 ${eq:,.0f}｜持倉 {len(book['positions'])} 檔")
    notify("📊 V2 摘要", "\n".join(lines) or "無資料")


def cmd_experiment() -> None:
    """V1 legacy: cumulative experiment stats from logs/experiment_daily.csv."""
    import pandas as pd
    import compute_experiment as ce

    csv_path = PROJECT_ROOT / "logs" / "experiment_daily.csv"
    if not csv_path.exists():
        notify("🧪 實驗(V1)", "尚無實驗數據（第一份日報在收盤後 21:40 UTC 產生）")
        return
    df = pd.read_csv(csv_path)
    day = datetime.now(ET).date().isoformat()
    today_rows = df[df["date"] == day].to_dict("records")
    notify("🧪 實驗(V1)", ce.summarize(df, today_rows, day))


def cmd_help() -> None:
    notify("🤖 指令", HELP)


COMMANDS = {
    "/positions": cmd_positions, "/pos": cmd_positions, "/持倉": cmd_positions,
    "/倉位": cmd_positions, "/holdings": cmd_positions,
    "/status": cmd_status, "/狀態": cmd_status,
    "/summary": cmd_summary, "/摘要": cmd_summary,
    "/experiment": cmd_experiment, "/exp": cmd_experiment, "/實驗": cmd_experiment,
    "/pnl": cmd_pnl, "/損益": cmd_pnl,
    "/today": cmd_today, "/今日": cmd_today,
    "/help": cmd_help, "/start": cmd_help,
}


def handle(text: str) -> None:
    token = text.split()[0].split("@")[0].lower() if text.split() else ""
    # match Chinese aliases case-insensitively too (lower() is a no-op for them)
    fn = COMMANDS.get(token) or COMMANDS.get(text.split()[0].split("@")[0] if text.split() else "")
    if fn is None:
        notify("🤖 未知指令", f"不認得「{text[:50]}」\n\n{HELP}")
        return
    try:
        fn()
    except Exception as exc:  # noqa: BLE001 - a handler error must not kill the bot
        notify("⚠️ 指令出錯", f"{token}: {type(exc).__name__}: {exc}")


# --- long-poll loop --------------------------------------------------------- #
def _get_updates(offset, timeout: int):
    import requests

    params = {"timeout": timeout}
    if offset is not None:
        params["offset"] = offset
    r = requests.get(f"{BASE}/getUpdates", params=params, timeout=timeout + 15)
    r.raise_for_status()
    return r.json().get("result", [])


def main() -> int:
    if not os.environ.get("TELEGRAM_BOT_TOKEN") or not CHAT_ID:
        print("[telegram_bot] missing TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID", file=sys.stderr)
        return 1

    # Drain any messages queued while the bot was down so we don't replay them.
    try:
        pending = _get_updates(None, 0)
        offset = (pending[-1]["update_id"] + 1) if pending else None
    except Exception:  # noqa: BLE001
        offset = None

    print(f"[telegram_bot] listening (chat={CHAT_ID}) …")
    notify("🤖 V2 指令機器人上線", "指令已全面改讀 V2 虛擬帳本\n/positions /pnl /status /summary /help")

    while True:
        try:
            for u in _get_updates(offset, 30):
                offset = u["update_id"] + 1
                msg = u.get("message") or u.get("edited_message")
                if not msg:
                    continue
                if str(msg.get("chat", {}).get("id")) != CHAT_ID:
                    continue  # boundary check: only the owner's chat
                text = (msg.get("text") or "").strip()
                if text.startswith("/"):
                    handle(text)
        except Exception as exc:  # noqa: BLE001 - keep the listener alive
            print(f"[telegram_bot] loop error: {exc}", file=sys.stderr)
            time.sleep(5)


if __name__ == "__main__":
    raise SystemExit(main())
