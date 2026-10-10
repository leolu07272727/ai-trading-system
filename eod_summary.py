#!/usr/bin/env python3
"""End-of-day activity summary → Telegram.

Complements compute_perf.py's cumulative "Daily Summary" by reporting TODAY's
pipeline activity: rule-gate funnel (what blocked entries), AI judge verdicts,
fills, and the EOD position state. Pure read-only; best-effort; never crashes.
"""
from __future__ import annotations

import csv
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from src.notify import notify

ROOT = Path(__file__).resolve().parent
LOG_DIR = ROOT / "logs"
DECISION_LOG = LOG_DIR / "decision_log.jsonl"
OVERLAY_LOG = LOG_DIR / "llm_overlay.jsonl"
POSITIONS = ROOT / "open_positions.json"
TRADES_CSV = ROOT / "trades.csv"
ET = ZoneInfo("America/New_York")


def _today() -> str:
    return datetime.now(ET).date().isoformat()


def _iter_jsonl(path: Path, today: str):
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except Exception:
            continue
        if str(d.get("ts", "")).startswith(today):
            yield d


def summarize_funnel(today: str) -> str:
    evals = 0
    signals: list[str] = []
    blocked: Counter = Counter()
    symbols: set = set()
    for d in _iter_jsonl(DECISION_LOG, today):
        if "pass" not in d:
            continue  # skip the event/detail records
        evals += 1
        symbols.add(d.get("symbol"))
        if d.get("pass"):
            signals.append(str(d.get("symbol")))
        else:
            reasons = d.get("reasons") or []
            gate = str(reasons[0]).split(":")[0] if reasons else "unknown"
            blocked[gate] += 1
    if not evals:
        return "規則掃描: 今日無評估紀錄(可能未開盤/watchlist 缺)"
    top = "、".join(f"{g}×{c}" for g, c in blocked.most_common(4))
    sig = f"{len(signals)}（{', '.join(sorted(set(signals)))}）" if signals else "0"
    return (
        f"規則掃描: {evals} 次評估 / {len(symbols)} 檔\n"
        f"進場訊號(全關通過): {sig}\n"
        f"最常卡關: {top or '—'}"
    )


def _fmt_verdict(v: dict) -> str:
    """symbol=verdict, annotating any AI size/stop adjustment away from 1.0×."""
    s = f"{v.get('symbol')}={v.get('verdict')}"
    extra = []
    for label, key in (("倉x", "size_multiplier"), ("停x", "stop_multiplier")):
        val = v.get(key)
        try:
            if val is not None and float(val) != 1.0:
                extra.append(f"{label}{val}")
        except (TypeError, ValueError):
            pass
    return s + (f"（{','.join(extra)}）" if extra else "")


def summarize_overlay(today: str) -> str:
    verdicts = list(_iter_jsonl(OVERLAY_LOG, today))
    if not verdicts:
        return "AI判官: 今日無判決(規則層未送出候選)"
    counts = Counter(v.get("verdict") for v in verdicts)
    emoji = {"approve": "✅", "reject": "🚫", "approve_reduced": "⚠️"}
    parts = "、".join(f"{emoji.get(k, '•')}{k}×{n}" for k, n in counts.items())
    # Count how many trades the AI actually nudged (size or stop off 1.0×).
    adj = sum(
        1
        for v in verdicts
        if _fmt_verdict(v).endswith("）")
    )
    syms = "、".join(_fmt_verdict(v) for v in verdicts[-5:])
    head = f"AI判官: {len(verdicts)} 判決（{parts}）"
    if adj:
        head += f" · 調整 {adj} 筆"
    return f"{head}\n  最近: {syms}"


def summarize_fills(today: str) -> str:
    if not TRADES_CSV.exists():
        return "今日成交: 無"
    rows = []
    try:
        with TRADES_CSV.open(newline="") as f:
            for r in csv.DictReader(f):
                if str(r.get("timestamp", "")).startswith(today):
                    rows.append(r)
    except Exception:
        return "今日成交: (讀取失敗)"
    if not rows:
        return "今日成交: 無"
    desc = "；".join(
        f"{r['symbol']} {r['side']} {r['size']}@{r['fill_price']}({r.get('status', '')})"
        for r in rows[:8]
    )
    return f"今日成交: {len(rows)} 筆 — {desc}"


def summarize_positions() -> str:
    try:
        data = json.loads(POSITIONS.read_text()) if POSITIONS.exists() else {}
    except Exception:
        data = {}
    if not data:
        return "收盤持倉: 無(已全平/空手)"
    items = "、".join(
        f"{s}×{(p.get('qty') if isinstance(p, dict) else p)}" for s, p in data.items()
    )
    return f"收盤持倉: {len(data)} 檔 — {items}"


def main() -> int:
    load_dotenv()
    today = _today()
    weekday = datetime.now(ET).strftime("%a")
    body = "\n".join(
        [
            summarize_funnel(today),
            summarize_overlay(today),
            summarize_fills(today),
            summarize_positions(),
        ]
    )
    notify(f"📊 收盤摘要 {today} ({weekday})", body)
    print(body)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
