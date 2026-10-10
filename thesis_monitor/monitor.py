#!/usr/bin/env python3
"""AI-memory thesis monitor — tracks whether the Street has caught up yet.

This is NOT a trading bot. It answers two questions for the thesis
"AI memory demand is structurally underestimated, the Street reacts too slowly":

  1. Is the Street still revising estimates UP?  (up  = you're still early)
                                                 (flat/down = you're getting late)
  2. Where are we in the cycle / how crowded is the trade now?
     (cheap + estimates rising + not everyone bullish = EARLY, the sweet spot)
     (near highs + everyone strong_buy + estimates plateauing = LATE, watch the exit)

It pulls free data from yfinance (valuation, analyst estimates + their revision
history, ratings, price targets, momentum) and folds in a small hand-maintained
cycle file (DRAM/HBM spot price trend, hyperscaler capex direction) that no free
API gives you. Each run snapshots to history/<ticker>.jsonl so you can WATCH the
Street close the gap over time — that drift is the whole signal.

Usage:
    python thesis_monitor/monitor.py            # default watchlist
    python thesis_monitor/monitor.py MU WDC STX # custom tickers
    python thesis_monitor/monitor.py --news     # + EXPERIMENT B: AI consensus-drift read
    python thesis_monitor/monitor.py --insider  # + EXPERIMENT C: SEC Form 4 insider read
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import yfinance as yf

ET = ZoneInfo("America/New_York")
HERE = Path(__file__).resolve().parent
HIST = HERE / "history"
CYCLE_FILE = HERE / "cycle_data.json"

DEFAULT_WATCHLIST = ["MU", "000660.KS", "NVDA", "WDC", "STX"]  # 記憶體三雄 + NVDA需求源 + 儲存


# ───────────────────────── data fetch ─────────────────────────

def _pct(new: float, old: float) -> float | None:
    if old in (None, 0) or new is None:
        return None
    return (new - old) / abs(old) * 100.0


def _rsi(closes, period: int = 14) -> float | None:
    if closes is None or len(closes) < period + 1:
        return None
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [d if d > 0 else 0.0 for d in deltas]
    losses = [-d if d < 0 else 0.0 for d in deltas]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(deltas)):  # Wilder smoothing
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def fetch(ticker: str) -> dict:
    t = yf.Ticker(ticker)
    info = t.info or {}
    price = info.get("currentPrice") or info.get("regularMarketPrice")
    hi = info.get("fiftyTwoWeekHigh")
    dma200 = info.get("twoHundredDayAverage")

    # estimate-revision history: the core "is the Street catching up" signal
    eps_rev_y1 = eps_rev_y0 = None   # current vs 90 days ago, %
    eps_mom_y1 = None                # current vs 7 days ago, %  (recent scramble)
    try:
        tr = t.eps_trend
        if tr is not None and len(tr):
            if "0y" in tr.index:
                r = tr.loc["0y"]
                eps_rev_y0 = _pct(r["current"], r["90daysAgo"])
            if "+1y" in tr.index:
                r = tr.loc["+1y"]
                eps_rev_y1 = _pct(r["current"], r["90daysAgo"])
                eps_mom_y1 = _pct(r["current"], r["7daysAgo"])
    except Exception:
        pass

    up30 = down30 = None
    try:
        rv = t.eps_revisions
        if rv is not None and "+1y" in rv.index:
            up30 = int(rv.loc["+1y"].get("upLast30days") or 0)
            down30 = int(rv.loc["+1y"].get("downLast30days") or 0)
    except Exception:
        pass

    rsi = None
    try:
        h = t.history(period="6mo", interval="1d")
        if h is not None and len(h):
            rsi = _rsi(list(h["Close"]))
    except Exception:
        pass

    tgt = info.get("targetMeanPrice")
    return {
        "ticker": ticker,
        "price": price,
        "fwd_pe": info.get("forwardPE"),
        "ttm_pe": info.get("trailingPE"),
        "pct_from_high": _pct(price, hi) if (price and hi) else None,
        "vs_200dma": _pct(price, dma200) if (price and dma200) else None,
        "rsi": rsi,
        "rec_key": info.get("recommendationKey"),
        "rec_mean": info.get("recommendationMean"),
        "n_analysts": info.get("numberOfAnalystOpinions"),
        "tgt_mean": tgt,
        "tgt_upside": _pct(tgt, price) if (price and tgt) else None,
        "eps_rev_y0": eps_rev_y0,
        "eps_rev_y1": eps_rev_y1,
        "eps_mom_y1": eps_mom_y1,
        "up30": up30,
        "down30": down30,
    }


# ───────────────────────── verdict ─────────────────────────

def verdict(m: dict) -> tuple[str, str, list[str]]:
    """Return (state, human_read, notes). state ∈ {EARLY, MID, LATE}."""
    notes: list[str] = []

    # 1) thesis intact?  estimates still being revised UP, hard, no downgrades
    rev = m["eps_rev_y1"]
    thesis_intact = rev is not None and rev > 5 and (m["down30"] or 0) <= (m["up30"] or 0)
    if rev is not None:
        notes.append(f"+1y EPS 估計 90天內 {rev:+.0f}%  (7天 {m['eps_mom_y1']:+.0f}%)"
                     if m["eps_mom_y1"] is not None else f"+1y EPS 估計 90天內 {rev:+.0f}%")
        if rev > 5 and (m["eps_mom_y1"] or 0) <= 1:
            notes.append("⚠ 上修動能在『近期』放緩 → Street 可能快追平,你的『早』在縮短")

    # 2) crowded? consensus already strong-buy AND price near the high
    crowded = (m["rec_mean"] is not None and m["rec_mean"] < 1.8) and \
              (m["pct_from_high"] is not None and m["pct_from_high"] > -12)
    if m["rec_key"]:
        notes.append(f"分析師 {m['rec_key']} (mean {m['rec_mean']}, {m['n_analysts']}人)"
                     + ("  ← 共識已很擠,你不再孤獨" if crowded else ""))

    # 3) froth / extension
    if m["pct_from_high"] is not None:
        tag = "  ← 貼著高點" if m["pct_from_high"] > -8 else ""
        notes.append(f"距52週高 {m['pct_from_high']:+.0f}%{tag}"
                     + (f", RSI {m['rsi']:.0f}" + ("(過熱)" if m["rsi"] and m["rsi"] > 70 else "")
                        if m["rsi"] else ""))

    # 4) cyclical caveat — memory forward P/E is often on PEAK earnings
    if m["fwd_pe"] is not None:
        cheap = m["fwd_pe"] < 12
        notes.append(f"forward P/E {m['fwd_pe']:.1f}"
                     + ("  (看似便宜,但記憶體可能是『高峰獲利』的低本益比 → 別當安全)" if cheap else ""))

    if not thesis_intact:
        state = "LATE"
        read = "🔴 LATE / EXIT-WATCH — 估計上修動能轉弱或翻負,循環可能在轉,準備減碼/跑"
    elif crowded:
        state = "MID"
        read = "🟡 MID — 你早期是對的,但共識正在追上(strong_buy + 貼高點),edge 正在被吃掉:控 size、設好出場條件"
    else:
        state = "EARLY"
        read = "🟢 EARLY — 估計還在上修、市場還沒全擠進來,thesis 還站得住(但仍是 cyclical,別 all-in)"
    return state, read, notes


def load_cycle() -> dict | None:
    if not CYCLE_FILE.exists():
        return None
    try:
        return json.loads(CYCLE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None


def render_cycle(c: dict | None) -> list[str]:
    if not c:
        return ["  (沒有 cycle_data.json — 手動填 DRAM/HBM 現貨價趨勢與 hyperscaler capex 方向,"
                "這是免費 API 給不了、但最關鍵的領先指標)"]
    out = []
    updated = c.get("last_updated", "?")
    out.append(f"  最後更新: {updated}")
    for k, label in [("dram_hbm_spot_trend", "DRAM/HBM 現貨價趨勢"),
                     ("hyperscaler_capex", "Hyperscaler capex 指引"),
                     ("memory_makers_capex", "三雄 capex/擴產")]:
        if k in c:
            out.append(f"  {label}: {c[k]}")
    if c.get("notes"):
        out.append(f"  備註: {c['notes']}")
    # turn-warnings encoded from manual data
    if str(c.get("dram_hbm_spot_trend", "")).lower() in ("down", "rolling over", "falling"):
        out.append("  🔴 現貨價在翻 → 循環見頂訊號,優先於估計動能")
    return out


# ───────────────────────── main ─────────────────────────

def main() -> int:
    argv = sys.argv[1:]
    want_news = "--news" in argv       # EXPERIMENT B: fold in AI consensus-drift read
    want_insider = "--insider" in argv  # EXPERIMENT C: fold in SEC Form 4 insider read
    if want_news:  # news_scan needs FINNHUB/ANTHROPIC keys from the project .env
        try:
            from dotenv import load_dotenv
            load_dotenv(HERE.parent / ".env")
        except Exception:
            pass
    tickers = [a.upper() for a in argv if not a.startswith("-")] or DEFAULT_WATCHLIST
    now = datetime.now(ET)
    stamp = now.strftime("%Y-%m-%d %H:%M ET")
    HIST.mkdir(exist_ok=True)

    print(f"\n══════ AI-Memory Thesis Monitor — {stamp} ══════")
    print("thesis: 『AI 記憶體需求被結構性低估,Street 反應太慢』 → 估計還在上修=你還早")

    for tk in tickers:
        try:
            m = fetch(tk)
        except Exception as exc:
            print(f"\n[{tk}] 抓取失敗: {exc}")
            continue
        _state, read, notes = verdict(m)
        print(f"\n┌─ {tk}  ${m['price']}")
        for n in notes:
            print(f"│  {n}")
        if m["tgt_upside"] is not None:
            print(f"│  分析師目標均價 ${m['tgt_mean']:.0f}  (還有 {m['tgt_upside']:+.0f}%)")
        if want_news:  # EXPERIMENT B: parallel AI news signal (does not change verdict)
            try:
                import news_scan
                line = news_scan.render(news_scan.scan(tk))
                if line:
                    print(f"│  {line}")
            except Exception:
                pass
        if want_insider:  # EXPERIMENT C: parallel insider read (does not change verdict)
            try:
                import insider_scan
                ins = insider_scan.scan(tk)
                line = insider_scan.render(ins)
                if line:
                    print(f"│  {line}")
                if ins:
                    m["insider"] = insider_scan.compact(ins)  # goes into the snapshot below
            except Exception:
                pass
        print(f"└▶ {read}")

        # snapshot for drift-watching
        snap = {"ts": now.isoformat(), **m}
        with (HIST / f"{tk}.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(snap, ensure_ascii=False) + "\n")

    print("\n── 循環領先指標 (手動,cycle_data.json) ──")
    for line in render_cycle(load_cycle()):
        print(line)
    print("\n提醒: 一次數據不能證明 skill,要看『下一次你還在不在對的那邊』。"
          " 估計上修=thesis 活著;共識全擠進來+現貨價翻=該想出場。\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
