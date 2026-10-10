#!/usr/bin/env python3
"""Daily experiment report — turns the logs the bot already writes into answers.

Three jobs (run after the close, cron 21:40 UTC):
  1. SHADOW TRACKING: every candidate that cleared the D-layer but was blocked
     later (intraday gate, AI judge, size/exposure caps) gets a hypothetical
     outcome — buy at the evaluation snapshot price, exit at today's close.
     Turns 0-3 real fills/day into 10-30 samples/day and quantifies whether
     each gate actually earns its keep. (No stop modelled: shadow = ride to
     close, so shadow returns are the NO-STOP variant of the live exit.)
  2. A/B TABLE: news-sourced vs gap-sourced candidates, cumulative.
  3. JUDGE SCORECARD: what did the AI judge's verdicts cost or save?

Reads logs/decision_log.jsonl, logs/llm_overlay.jsonl, logs/news_watchlist.jsonl.
Appends per-candidate rows to logs/experiment_daily.csv (idempotent per date:
rerunning a date replaces that date's rows). Pushes a Telegram summary.
Read-only with respect to trading: never touches orders, positions, or rules.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
LOG_DIR = ROOT / "logs"
DECISION_LOG = LOG_DIR / "decision_log.jsonl"
OVERLAY_LOG = LOG_DIR / "llm_overlay.jsonl"
NEWS_PICKS_LOG = LOG_DIR / "news_watchlist.jsonl"
OUT_CSV = LOG_DIR / "experiment_daily.csv"
ET = ZoneInfo("America/New_York")

CSV_COLUMNS = [
    "date", "symbol", "cohort", "source", "eval_price", "close",
    "ret_pct", "verdict", "confidence", "slippage_pct", "reason",
]

ENTRY_RE = re.compile(
    r"filled (\d+)@([\d.]+) stop=([\d.]+)(?: source=(\S+))?"
)


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _on_date(entries: list[dict], day: str) -> list[dict]:
    return [e for e in entries if str(e.get("ts", "")).startswith(day)]


def news_symbols_for(day: str) -> set[str]:
    """Tickers the AI news scan surfaced on `day` (source attribution for
    shadow rows — filled rows carry their own more precise tag)."""
    syms: set[str] = set()
    for rec in _read_jsonl(NEWS_PICKS_LOG):
        if rec.get("date") == day:
            for pick in rec.get("picks", []):
                tk = str(pick.get("ticker", "")).strip().upper()
                if tk:
                    syms.add(tk)
    return syms


# --------------------------------------------------------------------------- #
# Cohort extraction
# --------------------------------------------------------------------------- #
def build_rows(day: str) -> list[dict]:
    """One row per (symbol, cohort) for `day`, eval-priced and dedup'd to the
    first qualifying record. Cohorts:
      filled           — real entry (event: entry)
      llm_rejected     — cleared every rule gate, judge vetoed
      intraday_blocked — cleared the D-layer, failed I2/I2R/I3
      passed_skipped   — rules PASS but no fill (size 0 / exposure / limits)
    """
    decisions = _on_date(_read_jsonl(DECISION_LOG), day)
    overlay = _on_date(_read_jsonl(OVERLAY_LOG), day)
    news_syms = news_symbols_for(day)

    verdicts: dict[str, dict] = {}
    for rec in overlay:  # last verdict of the day wins
        verdicts[str(rec.get("symbol"))] = rec

    pass_price: dict[str, float] = {}
    for rec in decisions:
        if rec.get("pass") is True and rec.get("price") and rec["symbol"] not in pass_price:
            pass_price[rec["symbol"]] = float(rec["price"])

    rows: dict[tuple[str, str], dict] = {}

    def add(symbol: str, cohort: str, eval_price, reason: str, source: str | None = None,
            slippage_pct=None) -> None:
        key = (symbol, cohort)
        if key in rows or eval_price is None:
            return
        v = verdicts.get(symbol, {})
        rows[key] = {
            "date": day,
            "symbol": symbol,
            "cohort": cohort,
            "source": source or ("news" if symbol in news_syms else "gap"),
            "eval_price": round(float(eval_price), 4),
            "close": None,
            "ret_pct": None,
            "verdict": v.get("verdict"),
            "confidence": v.get("confidence"),
            "slippage_pct": slippage_pct,
            "reason": reason[:120],
        }

    for rec in decisions:
        symbol = str(rec.get("symbol", ""))
        event = rec.get("event")
        if event == "entry":
            m = ENTRY_RE.search(rec.get("detail", ""))
            if not m:
                continue
            fill = float(m.group(2))
            src = m.group(4)
            snap = pass_price.get(symbol)
            slip = round((fill - snap) / snap * 100.0, 3) if snap else None
            add(symbol, "filled", fill, rec.get("detail", ""), source=src,
                slippage_pct=slip)
        elif event == "llm_reject":
            add(symbol, "llm_rejected", pass_price.get(symbol), rec.get("detail", ""))
        elif event is None and rec.get("pass") is False:
            reasons = rec.get("reasons") or [""]
            first = str(reasons[0])
            if first.startswith(("I2", "I3")) and rec.get("price"):
                add(symbol, "intraday_blocked", rec["price"], first)

    # rules PASS but never became a fill nor an llm_reject → skipped by caps
    for symbol, price in pass_price.items():
        if (symbol, "filled") not in rows and (symbol, "llm_rejected") not in rows:
            add(symbol, "passed_skipped", price, "passed rules, no fill (caps/size)")

    return list(rows.values())


# --------------------------------------------------------------------------- #
# Outcomes
# --------------------------------------------------------------------------- #
def fill_closes(rows: list[dict], day: str) -> None:
    symbols = sorted({r["symbol"] for r in rows})
    if not symbols:
        return
    try:
        data = yf.download(
            tickers=symbols, period="5d", interval="1d",
            group_by="ticker", auto_adjust=True, progress=False, threads=5,
        )
    except Exception as exc:  # noqa: BLE001 - report still goes out without closes
        print(f"[experiment] close download failed: {exc}", file=sys.stderr)
        return
    target = pd.Timestamp(day)
    for r in rows:
        try:
            frame = data[r["symbol"]] if len(symbols) > 1 else data
            closes = frame["Close"].dropna()
            match = closes[closes.index.normalize() == target]
            if match.empty:
                continue
            close = float(match.iloc[-1])
            r["close"] = round(close, 4)
            r["ret_pct"] = round((close - r["eval_price"]) / r["eval_price"] * 100.0, 3)
        except Exception:  # noqa: BLE001 - per-symbol best-effort
            continue


def write_csv(rows: list[dict], day: str) -> pd.DataFrame:
    new = pd.DataFrame(rows, columns=CSV_COLUMNS)
    if OUT_CSV.exists():
        try:
            old = pd.read_csv(OUT_CSV)
            old = old[old["date"] != day]  # idempotent rerun for the same date
            combined = pd.concat([old, new], ignore_index=True)
        except Exception:  # noqa: BLE001 - never lose today's rows to a bad file
            combined = new
    else:
        combined = new
    combined.to_csv(OUT_CSV, index=False)
    return combined


# --------------------------------------------------------------------------- #
# Summary
# --------------------------------------------------------------------------- #
def _fmt_group(df: pd.DataFrame) -> str:
    lines = []
    for key, grp in df.groupby(df.index):
        rets = grp["ret_pct"].dropna()
        if rets.empty:
            lines.append(f"  {key}: n={len(grp)} (無收盤資料)")
            continue
        win = (rets > 0).mean() * 100
        lines.append(
            f"  {key}: n={len(grp)} 均{rets.mean():+.2f}% 勝率{win:.0f}%"
        )
    return "\n".join(lines) if lines else "  (無)"


def summarize(all_df: pd.DataFrame, today_rows: list[dict], day: str) -> str:
    today = pd.DataFrame(today_rows, columns=CSV_COLUMNS)
    parts = [f"📊 實驗日報 {day}"]

    if today.empty:
        parts.append("今日無任何通過 D 層的候選（安靜日，屬正常）。")
    else:
        counts = today["cohort"].value_counts().to_dict()
        parts.append(
            "今日樣本: "
            + " / ".join(f"{k} {v}" for k, v in counts.items())
        )
        rets = today.set_index("cohort")
        parts.append("今日各關卡假設報酬(評估價→收盤,無停損):")
        parts.append(_fmt_group(rets))
        slips = today["slippage_pct"].dropna()
        if not slips.empty:
            parts.append(f"進場滑價: 均{slips.mean():+.3f}% (n={len(slips)})")

    if not all_df.empty:
        parts.append("— 累積 —")
        parts.append(f"cohort 表 (共{len(all_df)}筆):")
        parts.append(_fmt_group(all_df.set_index("cohort")))
        traded = all_df[all_df["cohort"].isin(["filled", "llm_rejected",
                                               "intraday_blocked", "passed_skipped"])]
        parts.append("A/B by source:")
        parts.append(_fmt_group(traded.set_index("source")))
        rej = all_df[all_df["cohort"] == "llm_rejected"]["ret_pct"].dropna()
        fil = all_df[all_df["cohort"] == "filled"]["ret_pct"].dropna()
        if len(rej) >= 3 and len(fil) >= 3:
            verdict = "有價值" if rej.mean() < fil.mean() else "存疑"
            parts.append(
                f"判官記分: 否決者均{rej.mean():+.2f}% vs 成交者均{fil.mean():+.2f}%"
                f" → 否決{verdict}"
            )
    return "\n".join(parts)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Daily experiment report.")
    parser.add_argument("--date", default=datetime.now(ET).date().isoformat(),
                        help="ET date YYYY-MM-DD (default today)")
    parser.add_argument("--no-notify", action="store_true", help="Skip Telegram push.")
    args = parser.parse_args(argv)
    load_dotenv(ROOT / ".env")

    rows = build_rows(args.date)
    fill_closes(rows, args.date)
    all_df = write_csv(rows, args.date)
    message = summarize(all_df, rows, args.date)
    print(message)

    if not args.no_notify:
        try:
            from src.notify import notify
            notify("🧪 實驗日報", message)
        except Exception as exc:  # noqa: BLE001 - report must not crash on notify
            print(f"[experiment] notify failed: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
