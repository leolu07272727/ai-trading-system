#!/usr/bin/env python3
"""Virtual-book Execution Engine — V2 Phase 2 (BOT_V2_PROPOSAL.md §2.3, virtual variant)

Runs the thesis rules against today's evidence record and executes into a
VIRTUAL portfolio — no IBKR, no broker, no orders anywhere. Fills happen at the
price already captured in evidence/<thesis>.jsonl. At monthly frequency,
slippage is noise; what we're measuring is judgment, not microstructure.

Why virtual instead of the IBKR paper account: that account belongs to V1,
whose force_close flattens ALL positions daily at 15:51 ET — a V2 swing
position wouldn't survive its first close (iron rule #8).

Authority split (proposal §2.3): in the REAL book, entries need a human ack
and exits are automatic. In this machine-only virtual book everything is
automatic — its purpose is to build the machine's track record so Phase 3 can
compare it against Leo's decide.py log. Human and machine books never mix.

Rules executed (mirrors theses.yaml, damping from evidence.py respected):
  EXIT  falsifier triggered (immediate, no damping) or confirmed LATE
  TRIM  confirmed MID and price within 5% of 52w high  → sell half
  ENTRY confirmed EARLY, >10% off high, no position, entries not blocked
  ADD   confirmed EARLY/MID, >15% off high, no falsifier warning, below slice
  COOLDOWN 14 calendar days (≈10 trading days) per ticker after any action,
  falsifier exits excepted.

Sizing: slice = max_weight / n_tickers of equity; entry fills 60% of slice,
add fills to 100%; per-ticker hard cap 15% of equity.

State: portfolio.json (book) / trades.jsonl (fills) / portfolio_history.jsonl
(daily mark-to-market incl. SPY benchmark from the same start date).

Usage (run AFTER evidence.py each day):
    python thesis_monitor/evidence.py && python thesis_monitor/executor.py
    ... executor.py --notify     # + Telegram daily summary (cron uses this)
"""
from __future__ import annotations

import json
import math
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

ET = ZoneInfo("America/New_York")
HERE = Path(__file__).resolve().parent
THESES_FILE = HERE / "theses.yaml"
EVIDENCE_DIR = HERE / "evidence"
BOOK_FILE = HERE / "portfolio.json"
TRADES_FILE = HERE / "trades.jsonl"
HIST_FILE = HERE / "portfolio_history.jsonl"

START_CASH = 100_000.0
TICKER_CAP = 0.15          # single-ticker max share of equity
ENTRY_FILL = 0.60          # entry fills this fraction of the slice
COOLDOWN_DAYS = 14         # calendar ≈ 10 trading days

# set by main() when --notify: every fill also pushes an immediate Telegram
# alert (the 22:15 daily summary alone made Leo find out about entries a day late)
NOTIFY_TRADES = False


# ───────────────────────── book ─────────────────────────

def load_book() -> dict:
    if BOOK_FILE.exists():
        return json.loads(BOOK_FILE.read_text(encoding="utf-8"))
    return {"start_date": None, "cash": START_CASH, "positions": {},
            "cooldowns": {}, "spy_start_price": None, "last_prices": {},
            "falsified": {}}


def save_book(book: dict) -> None:
    BOOK_FILE.write_text(json.dumps(book, ensure_ascii=False, indent=2), encoding="utf-8")


def equity(book: dict) -> float:
    total = book["cash"]
    for tk, pos in book["positions"].items():
        px = book["last_prices"].get(tk) or pos["avg_cost"]
        total += pos["shares"] * px
    return total


def spy_price() -> float | None:
    try:
        import yfinance as yf
        h = yf.Ticker("SPY").history(period="2d")
        return float(h["Close"].iloc[-1]) if len(h) else None
    except Exception:
        return None


# ───────────────────────── fills ─────────────────────────

def fill(book: dict, ts: str, thesis_id: str, tk: str, side: str, shares: float,
         price: float, reason: str) -> None:
    if shares <= 0 or price <= 0:
        return
    if side == "BUY":
        cost = shares * price
        if cost > book["cash"]:
            shares = math.floor(book["cash"] / price)
            cost = shares * price
            if shares <= 0:
                return
        pos = book["positions"].get(tk, {"shares": 0.0, "avg_cost": 0.0,
                                         "thesis_id": thesis_id, "opened": ts[:10]})
        new_shares = pos["shares"] + shares
        pos["avg_cost"] = (pos["avg_cost"] * pos["shares"] + cost) / new_shares
        pos["shares"] = new_shares
        book["positions"][tk] = pos
        book["cash"] -= cost
    else:  # SELL
        pos = book["positions"].get(tk)
        if not pos:
            return
        shares = min(shares, pos["shares"])
        book["cash"] += shares * price
        pos["shares"] -= shares
        if pos["shares"] < 1e-9:
            del book["positions"][tk]
    book["cooldowns"][tk] = ts[:10]
    rec = {"ts": ts, "thesis_id": thesis_id, "ticker": tk, "side": side,
           "shares": shares, "price": price, "machine_reason": reason}
    with TRADES_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print(f"  ⚙ {side} {tk} {shares:g} @ {price:.2f} — {reason}")
    if NOTIFY_TRADES:
        send_notify(f"⚙️ V2 下單 {side} {tk}",
                    [f"{side} {tk} {shares:g} 股 @ {price:.2f}",
                     reason, f"thesis: {thesis_id}"])


def in_cooldown(book: dict, tk: str, today: str) -> bool:
    last = book["cooldowns"].get(tk)
    if not last:
        return False
    return (datetime.strptime(today, "%Y-%m-%d")
            - datetime.strptime(last, "%Y-%m-%d")).days < COOLDOWN_DAYS


# ───────────────────────── rules ─────────────────────────

def run_thesis(book: dict, th: dict, ev: dict, today: str) -> None:
    ts = datetime.now(ET).isoformat()
    prox = ev.get("falsifier_proximity", {})
    triggered = [f"{k}({v['why']})" for k, v in prox.items() if v["status"] == "triggered"]
    warning = any(v["status"] == "warning" for v in prox.values())
    blocked = ev.get("health", {}).get("entries_blocked", False)

    # refresh valuation prices from today's evidence FIRST — a falsifier exit
    # must fill at today's price, not yesterday's leftover
    for tk in th["tickers"]:
        d = ev.get("tickers", {}).get(tk)
        if d and d.get("price"):
            book["last_prices"][tk] = d["price"]

    # death registry: falsifier 觸發過的 thesis 不得再進場,即使 falsifier 隔天翻回 ok。
    # 機器不能寫 theses.yaml(鐵律),所以死亡狀態記在自己的帳本;復活=人工重審
    # thesis 後,手動從 portfolio.json 刪掉 falsified 標記(這個動作本身就留下紀錄)。
    if th["id"] in book.get("falsified", {}):
        print(f"  ☠ {th['id']} 已於 {book['falsified'][th['id']]} 被 falsifier 終結,"
              "不再交易(復活需人工重審+刪 portfolio.json 的 falsified 標記)")
        return

    eq = equity(book)
    slice_w = th["max_weight"] / max(len(th["tickers"]), 1)

    # 1) falsifier exit: immediate, no damping, no cooldown, closes the whole thesis
    if triggered:
        for tk in list(book["positions"]):
            if book["positions"][tk]["thesis_id"] == th["id"]:
                px = book["last_prices"].get(tk) or book["positions"][tk]["avg_cost"]
                fill(book, ts, th["id"], tk, "SELL", book["positions"][tk]["shares"], px,
                     f"falsifier 觸發: {'; '.join(triggered)}")
        book.setdefault("falsified", {})[th["id"]] = today
        return

    for tk in th["tickers"]:
        d = ev.get("tickers", {}).get(tk)
        if not d or d.get("price") in (None, 0):
            continue
        px = d["price"]
        book["last_prices"][tk] = px
        state, confirmed, suspect = d["state"], d["confirmed"], d.get("suspect")
        dist = d.get("pct_from_high")
        pos = book["positions"].get(tk)
        held_val = pos["shares"] * px if pos else 0.0

        if suspect or not confirmed:
            continue                       # damping / dirty data: no transitions today
        if in_cooldown(book, tk, today):
            continue

        if state == "LATE" and pos:
            fill(book, ts, th["id"], tk, "SELL", pos["shares"], px, "confirmed LATE → exit")
        elif state == "MID" and pos and dist is not None and dist > -5:
            fill(book, ts, th["id"], tk, "SELL", pos["shares"] * 0.5, px,
                 f"confirmed MID + 距高 {dist:+.0f}% → trim 50%")
        elif state == "EARLY" and not pos and dist is not None and dist < -10:
            if blocked:
                print(f"  ⛔ {tk} 符合 entry 但禁止新進場(資料健康檢查未過)")
                continue
            target = min(slice_w * ENTRY_FILL, TICKER_CAP) * eq
            fill(book, ts, th["id"], tk, "BUY", math.floor(target / px), px,
                 f"confirmed EARLY + 距高 {dist:+.0f}% → entry({ENTRY_FILL:.0%} slice)")
        elif (state in ("EARLY", "MID") and pos and dist is not None and dist < -15
              and not warning and not blocked):
            target = min(slice_w, TICKER_CAP) * eq
            if held_val < target * 0.95:
                fill(book, ts, th["id"], tk, "BUY", math.floor((target - held_val) / px), px,
                     f"confirmed {state} + 回檔 {dist:+.0f}% → add 至 slice")


# ───────────────────────── main ─────────────────────────

def send_notify(title: str, lines: list[str]) -> None:
    """Best-effort Telegram push via the project's src/notify.py. Never raises —
    a dead Telegram must not stop the book from being marked to market."""
    try:
        sys.path.insert(0, str(HERE.parent))
        from dotenv import load_dotenv
        load_dotenv(HERE.parent / ".env")
        from src.notify import notify
        notify(title, "\n".join(lines))
    except Exception as exc:
        print(f"(Telegram 通知失敗,不影響帳本: {exc})")


def main() -> int:
    global NOTIFY_TRADES
    want_notify = "--notify" in sys.argv[1:]
    NOTIFY_TRADES = want_notify
    now = datetime.now(ET)
    today = now.strftime("%Y-%m-%d")
    theses = [t for t in yaml.safe_load(THESES_FILE.read_text(encoding="utf-8")) or []
              if t.get("status") == "active"]
    book = load_book()
    if book["start_date"] is None:
        book["start_date"] = today
        book["spy_start_price"] = spy_price()
        print(f"— 虛擬帳本開帳: ${START_CASH:,.0f},SPY 基準 {book['spy_start_price']}")

    print(f"\n══════ Virtual Executor — {now.strftime('%Y-%m-%d %H:%M ET')} ══════")
    acted_or_checked = 0
    trades_at_start = (len(TRADES_FILE.read_text(encoding="utf-8").strip().splitlines())
                       if TRADES_FILE.exists() else 0)
    alerts: list[str] = []
    for th in theses:
        f = EVIDENCE_DIR / f"{th['id']}.jsonl"
        if not f.exists():
            print(f"▌{th['id']}: 沒有 evidence 紀錄,先跑 evidence.py")
            continue
        ev = json.loads(f.read_text(encoding="utf-8").strip().splitlines()[-1])
        if ev["ts"][:10] != today:
            print(f"▌{th['id']}: evidence 是 {ev['ts'][:10]} 的,不是今天 → 跳過(先跑 evidence.py)")
            alerts.append(f"⚠ {th['id']}: 沒有今天的 evidence,機器空轉(檢查 evidence cron)")
            continue
        for fid, p in ev.get("falsifier_proximity", {}).items():
            if p["status"] in ("warning", "triggered"):
                alerts.append(f"{'🔴' if p['status']=='triggered' else '🟡'} {fid}: {p['why']}")
        if ev.get("health", {}).get("entries_blocked"):
            alerts.append("⛔ 資料健康檢查未過(cycle_data 過期?),新進場已鎖")
        print(f"▌{th['id']}")
        trades_before = TRADES_FILE.stat().st_size if TRADES_FILE.exists() else 0
        run_thesis(book, th, ev, today)
        acted_or_checked += 1
        trades_after = TRADES_FILE.stat().st_size if TRADES_FILE.exists() else 0
        if trades_after == trades_before:
            print("  (規則掃過,今日無成交)")

    # mark-to-market + benchmark
    eq = equity(book)
    spy_now = spy_price()
    spy_val = (START_CASH * spy_now / book["spy_start_price"]
               if spy_now and book.get("spy_start_price") else None)
    save_book(book)
    snap = {"ts": now.isoformat(), "equity": round(eq, 2), "cash": round(book["cash"], 2),
            "positions": {tk: {"shares": p["shares"], "avg_cost": round(p["avg_cost"], 2),
                               "last": book["last_prices"].get(tk)}
                          for tk, p in book["positions"].items()},
            "spy_benchmark": round(spy_val, 2) if spy_val else None}
    with HIST_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(snap, ensure_ascii=False) + "\n")

    print(f"\n帳本: equity ${eq:,.0f}  cash ${book['cash']:,.0f}  持倉 {len(book['positions'])} 檔"
          + (f"  |  SPY 同期 ${spy_val:,.0f}" if spy_val else ""))
    for tk, p in book["positions"].items():
        last = book["last_prices"].get(tk) or p["avg_cost"]
        pnl = (last / p["avg_cost"] - 1) * 100
        print(f"  {tk}: {p['shares']:g} 股 @ 成本 {p['avg_cost']:.2f},現價 {last:.2f} ({pnl:+.1f}%)")
    if acted_or_checked == 0:
        print("(今天沒有任何 thesis 可執行——先跑 evidence.py)")

    if want_notify:
        all_lines = (TRADES_FILE.read_text(encoding="utf-8").strip().splitlines()
                     if TRADES_FILE.exists() else [])
        new_trades = [json.loads(x) for x in all_lines[trades_at_start:]]
        lines = ([f"⚙ {t['side']} {t['ticker']} {t['shares']:g}@{t['price']:.2f} — {t['machine_reason']}"
                  for t in new_trades] or ["今日無成交(無狀態轉換=正確地什麼都不做)"])
        lines.append(f"帳本 ${eq:,.0f} | 現金 ${book['cash']:,.0f} | 持倉 {len(book['positions'])} 檔"
                     + (f" | SPY 同期 ${spy_val:,.0f}" if spy_val else ""))
        lines += alerts
        send_notify(f"⚙️ V2 虛擬帳本 — {today}", lines)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
