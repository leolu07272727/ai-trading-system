#!/usr/bin/env python3
"""Ex-ante decision log — V2 Phase 1 (BOT_V2_PROPOSAL.md §2.3, §3)

Every human decision gets logged BEFORE it is acted on, with the reason written
down ex-ante and the latest evidence snapshot attached — the raw material for
the 90-day ex-post review ("方向對但理由錯 = 運氣,誠實記錄").

This tool never places orders. Phase 1 execution is the human at the IBKR
paper account; this is the paper trail that makes that auditable.

Usage:
    python thesis_monitor/decide.py --thesis ai-memory-2026 --action enter \
        --ticker MU --shares 10 --price 1032 --reason "EARLY 確認 + 距高18% + ..."

    # 期初對帳(既有倉位接入,不計入 promotion gate 循環數):
    python thesis_monitor/decide.py --thesis ai-memory-2026 --action retro \
        --ticker MU --price 993 --reason "thesis 起源交易,2026-06 前後進場"

Actions: enter / add / trim / exit / hold / retro
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

ET = ZoneInfo("America/New_York")
HERE = Path(__file__).resolve().parent
DECISIONS = HERE / "decisions.jsonl"
EVIDENCE_DIR = HERE / "evidence"
THESES_FILE = HERE / "theses.yaml"

ACTIONS = ["enter", "add", "trim", "exit", "hold", "retro"]
MIN_REASON_LEN = 20  # 「覺得會漲」不是 ex-ante 理由


def latest_evidence(thesis_id: str) -> dict | None:
    f = EVIDENCE_DIR / f"{thesis_id}.jsonl"
    if not f.exists():
        return None
    lines = f.read_text(encoding="utf-8").strip().splitlines()
    return json.loads(lines[-1]) if lines else None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--thesis", required=True)
    ap.add_argument("--action", required=True, choices=ACTIONS)
    ap.add_argument("--ticker", required=True)
    ap.add_argument("--reason", required=True,
                    help=f"ex-ante 理由,≥{MIN_REASON_LEN} 字,寫給 90 天後對答案的自己")
    ap.add_argument("--shares", type=float, default=None)
    ap.add_argument("--price", type=float, default=None)
    a = ap.parse_args()

    theses = {t["id"]: t for t in yaml.safe_load(THESES_FILE.read_text(encoding="utf-8")) or []}
    th = theses.get(a.thesis)
    if not th:
        print(f"✗ thesis '{a.thesis}' 不在 theses.yaml,先寫 thesis 再做決策(§1 第 1 條)")
        return 1
    if th["status"] != "active" and a.action not in ("exit", "retro"):
        print(f"✗ thesis 狀態是 {th['status']},只允許 exit/retro")
        return 1
    if len(a.reason.strip()) < MIN_REASON_LEN:
        print(f"✗ 理由太短({len(a.reason.strip())} 字 < {MIN_REASON_LEN})。"
              "寫不出理由的單,90 天後也對不了答案——這正是要擋的行為。")
        return 1

    ev = latest_evidence(a.thesis)
    ev_age_h = None
    if ev:
        ev_age_h = (datetime.now(ET) - datetime.fromisoformat(ev["ts"])).total_seconds() / 3600
    if a.action in ("enter", "add") and (ev is None or ev_age_h > 30):
        print("✗ 沒有 30 小時內的 evidence 快照。先跑 evidence.py 再進場——"
              "沒看今天的反方證據就下單,就是系統要防的那件事。")
        return 1
    if a.action in ("enter", "add") and ev and ev.get("health", {}).get("entries_blocked"):
        print("✗ evidence 顯示【禁止新進場】(cycle_data 過期或缺失),先修資料再談進場。")
        return 1

    now = datetime.now(ET)
    rec = {
        "ts": now.isoformat(),
        "thesis_id": a.thesis,
        "ticker": a.ticker.upper(),
        "action": a.action,
        "shares": a.shares,
        "price": a.price,
        "ex_ante_reason": a.reason.strip(),
        "retro": a.action == "retro",
        "counts_for_gate": a.action != "retro",  # §4: retro 不計入循環數
        "evidence_ts": ev["ts"] if ev else None,
        "evidence_suggestions": ev.get("suggestions", []) if ev else None,
        "evidence_against_at_decision": (
            {tk: v.get("evidence_against", []) for tk, v in ev.get("tickers", {}).items()}
            if ev else None),
        "review_due": (now + timedelta(days=90)).strftime("%Y-%m-%d"),
        "ex_post": None,  # 90 天後回填: {verdict: thesis對/運氣/錯, note}
    }
    with DECISIONS.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    print(f"✓ 已記錄 {a.action.upper()} {rec['ticker']}"
          + (f" {a.shares:g}股" if a.shares else "")
          + (f" @{a.price:g}" if a.price else ""))
    print(f"  ex-ante: {rec['ex_ante_reason']}")
    if ev:
        flat_against = [e for v in rec["evidence_against_at_decision"].values() for e in v]
        if flat_against and a.action in ("enter", "add"):
            print("  ⚔ 你下單當下的反方證據(已存檔,90 天後一起對答案):")
            for e in flat_against[:6]:
                print(f"    - {e}")
    print(f"  📅 ex-post 回顧日: {rec['review_due']}"
          + ("(retro 不計入 promotion gate)" if rec["retro"] else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
