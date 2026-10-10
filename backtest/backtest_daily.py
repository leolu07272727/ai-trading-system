#!/usr/bin/env python3
"""Tier-1 daily-bar backtest for 'Trend Join Long' — exit-model comparison.

Tests the four DAILY gates computable from free daily bars:
  Regime SPY>SMA200 (prior day) · D1 open>prior high · D2 prior close>SMA200 ·
  D3 gap>=3% · min price. The three INTRADAY gates (I1/I2/I3) are NOT modelled
  (yfinance serves ~60 days of minute bars → years-deep intraday = paid "Tier 2").
Because I1/I2/I3 are filters, the real bot trades a SUBSET of these signals.

Entry = signal-day open (real bot enters intraday on strength → slightly higher).
Strategy is intraday: every trade is a same-day round trip (flat by EOD), so
"exit design" reduces to (a) stop WIDTH and (b) whether you CLIP winners with a
partial/breakeven. The three models isolate exactly that:

  current : 1% stop + partial 1/3 at +0.75R, remainder protected at breakeven.
            (mirrors the live exit rules; conservative path: if both the stop and
            the +0.75R target sit inside the day's range, assume STOP hit first.)
  ride    : 1% stop, full position to the close (no partial, no clip).
  atr     : stop = entry - 1.5*ATR14, full to the close (wider, vol-scaled stop).

1R = the model's own risk-per-share, so expectancy_R is sizing-normalised and
comparable across models (each risks 1% of equity per 1R in the portfolio sim).
Cost = round-trip bps, applied as a return drag before the R conversion.

CAVEATS (all OPTIMISTIC): survivorship bias (today's members → past), entry at
open, omitted intraday filters. Read as "which exit is least bad / is there a
tail to capture", not as truth.
"""
from __future__ import annotations

import argparse
import json
import pickle
import sys
from pathlib import Path

import pandas as pd
import yfinance as yf

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.sp500_tickers import SP500_TICKERS, get_sector  # noqa: E402

HERE = Path(__file__).resolve().parent
DATA_DIR = HERE / "data"
RESULTS_DIR = HERE / "results"
RULES_PATH = PROJECT_ROOT / "config" / "rules.json"

BATCH_SIZE = 50
MODELS = ["current", "ride", "atr"]


# --------------------------------------------------------------------------- #
# Data
# --------------------------------------------------------------------------- #
def _normalise(df: pd.DataFrame) -> pd.DataFrame | None:
    if df is None or df.empty:
        return None
    df = df.rename(columns={c: c.capitalize()
                            for c in ["open", "high", "low", "close", "volume"]})
    need = ["Open", "High", "Low", "Close", "Volume"]
    if not all(c in df.columns for c in need):
        return None
    out = df[need].dropna()
    return out if len(out) > 220 else None


def download_history(tickers, period, cache, refresh) -> dict[str, pd.DataFrame]:
    if cache.exists() and not refresh:
        print(f"[data] loading cache {cache.name}")
        with cache.open("rb") as fh:
            return pickle.load(fh)
    print(f"[data] downloading {len(tickers)} tickers, period={period} …")
    out: dict[str, pd.DataFrame] = {}
    for i in range(0, len(tickers), BATCH_SIZE):
        batch = tickers[i:i + BATCH_SIZE]
        try:
            raw = yf.download(batch, period=period, interval="1d",
                              group_by="ticker", auto_adjust=True,
                              threads=5, progress=False)
        except Exception as exc:  # noqa: BLE001
            print(f"[data] batch fail: {exc}", file=sys.stderr)
            continue
        for t in batch:
            try:
                sub = raw[t] if len(batch) > 1 else raw
            except (KeyError, TypeError):
                continue
            norm = _normalise(sub)
            if norm is not None:
                out[t] = norm
    cache.parent.mkdir(parents=True, exist_ok=True)
    with cache.open("wb") as fh:
        pickle.dump(out, fh)
    print(f"[data] cached {len(out)} usable tickers → {cache.name}")
    return out


# --------------------------------------------------------------------------- #
# Signals
# --------------------------------------------------------------------------- #
def regime_series(spy: pd.DataFrame) -> pd.Series:
    sma = spy["Close"].rolling(200).mean()
    return spy["Close"].shift(1) > sma.shift(1)


def _atr14_prev(df: pd.DataFrame) -> pd.Series:
    pc = df["Close"].shift(1)
    tr = pd.concat([df["High"] - df["Low"],
                    (df["High"] - pc).abs(),
                    (df["Low"] - pc).abs()], axis=1).max(axis=1)
    return tr.rolling(14).mean().shift(1)


def signal_events(symbol, df, regime, p) -> list[dict]:
    prior_close = df["Close"].shift(1)
    prior_high = df["High"].shift(1)
    sma200_prev = df["Close"].rolling(200).mean().shift(1)
    atr_prev = _atr14_prev(df)
    gap = (df["Open"] - prior_close) / prior_close * 100.0
    sig = ((df["Open"] > prior_high) & (prior_close > sma200_prev)
           & (gap >= p["gap_pct"]) & (prior_close >= p["min_price"]))
    sig = sig & regime.reindex(df.index).fillna(False)

    sector = get_sector(symbol)
    events = []
    for dt in df.index[sig.fillna(False)]:
        entry = float(df.at[dt, "Open"])
        if entry <= 0:
            continue
        events.append({
            "date": dt.date().isoformat(), "symbol": symbol, "sector": sector,
            "gap_pct": round(float(gap.loc[dt]), 2), "entry": entry,
            "high": float(df.at[dt, "High"]), "low": float(df.at[dt, "Low"]),
            "close": float(df.at[dt, "Close"]),
            "atr_prev": float(atr_prev.loc[dt]) if pd.notna(atr_prev.loc[dt]) else 0.0,
        })
    return events


# --------------------------------------------------------------------------- #
# Exit models  →  net R per trade
# --------------------------------------------------------------------------- #
def exit_R(ev, model, cost_pct, atr_mult, partial_frac, partial_R) -> float:
    entry, H, L, C = ev["entry"], ev["high"], ev["low"], ev["close"]

    if model == "ride":
        risk_pct = 1.0
        ret_pct = -1.0 if L <= entry * 0.99 else (C / entry - 1) * 100
        return (ret_pct - cost_pct) / risk_pct

    if model == "atr":
        atr = ev["atr_prev"]
        rps = atr_mult * atr if atr > 0 else entry * 0.01
        stop = entry - rps
        risk_pct = rps / entry * 100
        ret_pct = -risk_pct if L <= stop else (C / entry - 1) * 100
        return (ret_pct - cost_pct) / risk_pct

    # current: 1% stop + partial 1/3 at +0.75R, remainder breakeven-protected
    risk_pct = 1.0
    stop = entry * 0.99
    t1 = entry * (1 + partial_R * 0.01)           # +0.75R price (0.75%)
    hit_s, hit_t1 = L <= stop, H >= t1
    if not hit_t1:
        ret_pct = -1.0 if hit_s else (C / entry - 1) * 100
    elif hit_s:                                   # ambiguous → assume stop first
        ret_pct = -1.0
    else:
        rem = 1 - partial_frac
        r_rem = 0.0 if L <= entry else (C / entry - 1) * 100
        ret_pct = partial_frac * (partial_R) + rem * r_rem
    return (ret_pct - cost_pct) / risk_pct


# --------------------------------------------------------------------------- #
# Stats
# --------------------------------------------------------------------------- #
def per_signal_stats(rs: list[float]) -> dict:
    n = len(rs)
    if not n:
        return {"n_trades": 0}
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    gw, gl = sum(wins), abs(sum(losses))
    return {
        "n_trades": n,
        "win_rate_pct": round(100 * len(wins) / n, 1),
        "avg_win_R": round(gw / len(wins), 3) if wins else 0.0,
        "avg_loss_R": round(sum(losses) / len(losses), 3) if losses else 0.0,
        "expectancy_R": round(sum(rs) / n, 4),
        "profit_factor": round(gw / gl, 3) if gl else float("inf"),
        "total_R": round(sum(rs), 1),
        "top20_R": round(sum(sorted(rs, reverse=True)[:20]), 1),
    }


def portfolio_curve(trades, max_pos, max_sec, risk_pct) -> dict:
    by_day: dict[str, list] = {}
    for t in trades:
        by_day.setdefault(t["date"], []).append(t)
    equity = peak = 1.0
    max_dd = 0.0
    win_days = n_days = 0
    for day in sorted(by_day):
        cands = sorted(by_day[day], key=lambda t: t["gap_pct"], reverse=True)
        sec: dict[str, int] = {}
        taken = []
        for c in cands:
            if len(taken) >= max_pos:
                break
            if sec.get(c["sector"], 0) >= max_sec:
                continue
            sec[c["sector"]] = sec.get(c["sector"], 0) + 1
            taken.append(c)
        if not taken:
            continue
        day_R = sum(t["R"] for t in taken)
        equity *= (1 + (risk_pct / 100) * day_R)
        n_days += 1
        win_days += day_R > 0
        peak = max(peak, equity)
        max_dd = max(max_dd, (peak - equity) / peak)
    years = n_days / 252 if n_days else 0
    cagr = (equity ** (1 / years) - 1) * 100 if years > 0 and equity > 0 else 0.0
    return {
        "total_return_pct": round((equity - 1) * 100, 1),
        "CAGR_pct": round(cagr, 1),
        "max_drawdown_pct": round(max_dd * 100, 1),
        "winning_days_pct": round(100 * win_days / n_days, 1) if n_days else 0.0,
        "final_equity_x": round(equity, 3),
    }


# --------------------------------------------------------------------------- #
def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--period", default="5y")
    ap.add_argument("--cost-bps", type=float, default=10.0)
    ap.add_argument("--atr-mult", type=float, default=1.5)
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--max-pos", type=int, default=0, help="override max concurrent positions")
    ap.add_argument("--max-sec", type=int, default=0, help="override max positions per sector")
    args = ap.parse_args(argv)

    rules = json.loads(RULES_PATH.read_text())
    p = {"gap_pct": float(rules["daily_filters"]["D3_min_gap_pct_from_prior_close"]),
         "min_price": float(rules["universe_filters"]["min_price_usd"])}
    risk_pct = float(rules["risk"]["max_risk_per_trade_pct"])
    max_pos = args.max_pos or int(rules["risk"]["max_concurrent_positions"])
    max_sec = args.max_sec or int(rules["risk"]["max_positions_per_sector"])
    partial_frac = float(rules["exit"]["partial_profit_fraction"])
    partial_R = float(rules["exit"]["partial_profit_trigger_R"])
    cost_pct = args.cost_bps / 100

    tickers = SP500_TICKERS[:args.limit] if args.limit else SP500_TICKERS
    cache = DATA_DIR / f"daily_{args.period}.pkl"
    data = download_history(list(tickers) + ["SPY"], args.period, cache, args.refresh)
    if "SPY" not in data:
        print("FATAL: no SPY", file=sys.stderr)
        return 1
    regime = regime_series(data["SPY"])

    events: list[dict] = []
    for sym, df in data.items():
        if sym != "SPY":
            events.extend(signal_events(sym, df, regime, p))
    span = ""
    if events:
        ds = sorted(e["date"] for e in events)
        span = f"{ds[0]} → {ds[-1]}"

    per_model = {}
    for m in MODELS:
        trades = [{**e, "R": exit_R(e, m, cost_pct, args.atr_mult,
                                    partial_frac, partial_R)} for e in events]
        per_model[m] = {
            "per_signal": per_signal_stats([t["R"] for t in trades]),
            "portfolio": portfolio_curve(trades, max_pos, max_sec, risk_pct),
        }

    print("\n" + "=" * 66)
    print("  EXIT-MODEL COMPARISON — Trend Join Long (daily gates only)")
    print("=" * 66)
    print(f"  {len(data)-1} S&P 500 names · {args.period} ({span}) · {len(events)} signals")
    print(f"  cost {args.cost_bps:.0f}bps · ATR mult {args.atr_mult} · NOT modelled: I1/I2/I3")
    print("-" * 66)
    hdr = f"  {'metric':16}" + "".join(f"{m:>14}" for m in MODELS)
    print(hdr)
    keys = ["n_trades", "win_rate_pct", "avg_win_R", "avg_loss_R",
            "expectancy_R", "profit_factor", "total_R", "top20_R"]
    for k in keys:
        row = f"  {k:16}" + "".join(f"{per_model[m]['per_signal'][k]:>14}" for m in MODELS)
        print(row)
    print("-" * 66)
    print(f"  PORTFOLIO (<= {max_pos} pos/day, <= {max_sec}/sector, {risk_pct:.0f}% risk):")
    for k in ["total_return_pct", "CAGR_pct", "max_drawdown_pct",
              "winning_days_pct", "final_equity_x"]:
        row = f"  {k:16}" + "".join(f"{per_model[m]['portfolio'][k]:>14}" for m in MODELS)
        print(row)
    print("-" * 66)
    print("  ⚠ optimistic caveats: survivorship bias, entry-at-open, no I1/I2/I3.")
    print("  'current' uses conservative stop-first when stop & +0.75R both in range.")
    print("=" * 66 + "\n")

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    (RESULTS_DIR / "exit_comparison.json").write_text(json.dumps(
        {"params": {**p, "cost_bps": args.cost_bps, "atr_mult": args.atr_mult,
                    "period": args.period, "span": span, "n_signals": len(events)},
         "models": per_model}, indent=2))
    print(f"[out] exit_comparison.json → {RESULTS_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
