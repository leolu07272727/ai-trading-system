#!/usr/bin/env python3
"""Summarise today's entry decisions from logs/decision_log.jsonl.

Answers "did the bot buy anything, and if not, why?" at a glance. Two log
schemas are merged in that file:
  - strategy.py gate checks: {ts, symbol, pass, reasons:[...], price, gap_pct}
  - cycle.py events:         {ts, event, symbol, detail}

Usage:
    python scripts/today_decisions.py            # today (ET)
    python scripts/today_decisions.py 2026-06-26 # a specific date
"""
from __future__ import annotations

import collections
import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
LOG = Path(__file__).resolve().parent.parent / "logs" / "decision_log.jsonl"


def main() -> int:
    day = sys.argv[1] if len(sys.argv) > 1 else datetime.now(ET).strftime("%Y-%m-%d")
    if not LOG.exists():
        print(f"no decision log at {LOG}")
        return 1

    entries, events = 0, []
    gate_reasons = collections.Counter()
    per_symbol_last = {}          # symbol -> last rejection reason of the day
    passes = []                   # symbols that passed all gates

    for line in LOG.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not str(rec.get("ts", "")).startswith(day):
            continue

        if "event" in rec:                       # cycle.py event row
            ev = rec["event"]
            # reconcile_added = a real fill the entry path lost track of
            # (e.g. 2026-07-02 AAPB) — it is a buy, count it as one
            if ev in ("entry", "reconcile_added"):
                entries += 1
            events.append((rec.get("symbol", "?"), ev, rec.get("detail", "")))
        elif "pass" in rec:                      # strategy.py gate row
            sym = rec.get("symbol", "?")
            if rec["pass"]:
                passes.append(sym)
            else:
                for r in rec.get("reasons", []) or ["(no reason)"]:
                    gate_reasons[r] += 1
                    per_symbol_last[sym] = r

    print(f"\n=== decisions for {day} (ET) ===")
    print(f"  BUYS today: {entries}")
    print(f"  candidates that passed ALL gates: {len(set(passes))} "
          f"{sorted(set(passes)) if passes else ''}")
    print("\n  why candidates were rejected (gate → count):")
    if gate_reasons:
        for reason, n in gate_reasons.most_common():
            print(f"    {n:4d}  {reason}")
    else:
        print("    (no gate evaluations logged — likely outside entry window all day)")

    notable = [e for e in events if e[1] in ("llm_reject", "llm_live", "trade_failed",
                                             "trade_unclear", "exposure_trim", "entry")]
    if notable:
        print("\n  notable events:")
        for sym, ev, detail in notable:
            print(f"    {ev:14s} {sym:8s} {detail}")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
