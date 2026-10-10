#!/usr/bin/env python3
"""Daily thesis-state watcher → Telegram on change only.

Runs the AI-memory thesis monitor, compares each ticker's verdict state
(EARLY/MID/LATE) against the last run stored in state.json, and pushes a
Telegram message ONLY when something changed (state flip, or the manual
DRAM/HBM spot-price trend turning down). Quiet when nothing moved.

Reuses the trading bot's src/notify.py sender and its .env credentials.
Meant to be run once a day from cron.

Usage:
    python thesis_monitor/notify_changes.py          # run + notify on change
    python thesis_monitor/notify_changes.py --dry     # print only, never send
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent                      # trading-bot/ (has src/notify.py + .env)
sys.path.insert(0, str(HERE))           # import monitor
sys.path.insert(0, str(ROOT))           # import src.notify

# Load .env so TELEGRAM_* are available, exactly like telegram_bot.py does.
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except Exception:
    pass

import monitor as M  # noqa: E402
import news_scan as NS  # noqa: E402  (EXPERIMENT B)
from src.notify import notify  # noqa: E402

import os
NEWS_SCAN_ON = os.environ.get("THESIS_NEWS_SCAN", "").strip() in ("1", "true", "yes")

ET = ZoneInfo("America/New_York")
STATE_FILE = HERE / "state.json"
EMOJI = {"EARLY": "🟢", "MID": "🟡", "LATE": "🔴"}
DOWN_WORDS = ("down", "rolling over", "falling", "翻黑", "下跌")


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(state: dict) -> None:
    try:
        STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2),
                              encoding="utf-8")
    except Exception as exc:
        print(f"[notify_changes] could not save state: {exc}", file=sys.stderr)


def main() -> int:
    dry = "--dry" in sys.argv[1:]
    tickers = [a.upper() for a in sys.argv[1:] if not a.startswith("-")] or M.DEFAULT_WATCHLIST
    now = datetime.now(ET)
    prev = load_state()
    new_state: dict = {}
    changes: list[str] = []

    for tk in tickers:
        try:
            m = M.fetch(tk)
            state, _read, _notes = M.verdict(m)
        except Exception as exc:
            print(f"[{tk}] fetch failed: {exc}", file=sys.stderr)
            if tk in prev:                       # keep last known state on transient error
                new_state[tk] = prev[tk]
            continue
        rev = m.get("eps_rev_y1")
        revtxt = f", +1y估 {rev:+.0f}%" if rev is not None else ""
        new_state[tk] = {"state": state, "price": m.get("price"), "rev_y1": rev}

        # EXPERIMENT B: optional AI consensus-drift read (env THESIS_NEWS_SCAN=1).
        # Parallel signal — never changes `state`; only adds its own notifications.
        ai = None
        if NEWS_SCAN_ON:
            try:
                ai = NS.scan(tk)
            except Exception:
                ai = None
        if ai:
            new_state[tk]["ai_dir"] = ai.get("direction")
            new_state[tk]["ai_catch"] = bool(ai.get("catching_up"))
            prev_tk = prev.get(tk) or {}
            had_ai = "ai_dir" in prev_tk  # was news scan already recorded before?
            if had_ai and ai.get("catching_up") and not prev_tk.get("ai_catch"):
                changes.append(f"⚠ {tk}: AI 判定 Street 已追上 — {ai.get('summary','')}")
            elif prev_tk.get("ai_dir") and ai.get("direction") != prev_tk.get("ai_dir"):
                changes.append(f"{tk}: AI 共識 {prev_tk.get('ai_dir')}→{ai.get('direction')} — {ai.get('summary','')}")

        # snapshot for the drift log (same file monitor.py writes); skip on dry
        if not dry:
            try:
                M.HIST.mkdir(exist_ok=True)
                with (M.HIST / f"{tk}.jsonl").open("a", encoding="utf-8") as f:
                    f.write(json.dumps({"ts": now.isoformat(), **m}, ensure_ascii=False) + "\n")
            except Exception:
                pass

        old = (prev.get(tk) or {}).get("state")
        if old and old != state:
            ainote = f"  · {NS.render(ai)}" if ai else ""
            changes.append(f"{tk}: {EMOJI.get(old,'')}{old} → {EMOJI.get(state,'')}{state}"
                           f"  (${m.get('price')}{revtxt}){ainote}")

    # manual cycle: alert when spot-price trend turns down (top signal)
    cyc = M.load_cycle() or {}
    spot = str(cyc.get("dram_hbm_spot_trend", "")).lower()
    spot_down = any(w in spot for w in DOWN_WORDS)
    prev_spot_down = bool((prev.get("_cycle") or {}).get("spot_down"))
    new_state["_cycle"] = {"spot_down": spot_down, "spot": spot}
    if spot_down and not prev_spot_down:
        changes.append("⚠ DRAM/HBM 現貨價翻 down — 循環見頂訊號,優先於估計動能,考慮減碼")

    first_run = not any(k != "_cycle" for k in prev)
    stamp = now.strftime("%Y-%m-%d %H:%M ET")

    if first_run:
        body = "\n".join(f"{tk}: {EMOJI.get(v['state'],'')}{v['state']} (${v.get('price')})"
                         for tk, v in new_state.items() if tk != "_cycle")
        title = f"🧠 記憶體 thesis 監控上線 — {stamp}"
        msg = f"基準狀態:\n{body}\n(之後只在狀態變化時才通知)"
    elif changes:
        title = f"🧠 記憶體 thesis 狀態變化 — {stamp}"
        msg = "\n".join(changes) + \
              "\n\n估計續上修=thesis活著;轉MID=共識追上(管理);LATE/現貨翻黑=想出場。"
    else:
        print(f"[{stamp}] no state changes — staying quiet.")
        if not dry:
            save_state(new_state)
        return 0

    print(title)
    print(msg)
    if not dry:
        notify(title, msg)
        save_state(new_state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
