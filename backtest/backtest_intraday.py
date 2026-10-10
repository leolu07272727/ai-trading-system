#!/usr/bin/env python3
"""Tier-2 intraday backtest — does intraday strength add edge over entering the
daily D1/D2/D3 signal at the open?

Reuses the daily signal engine (regime + D1/D2/D3 + min price) from
backtest_daily, then for each signal-day pulls 1-minute bars from Alpaca (free
IEX feed) to reconstruct a realistic intraday entry + exit.

Cohorts share ONE minute-bar exit engine, so the ENTRY rule is isolated:
  open : enter at the 09:30 open  (baseline = the Tier-1 'no intraday filter').
  i2   : enter at the first HOD breakout inside the entry window [10:05,15:30];
         days that never break their morning high are SKIPPED (the I2 filter).

Exit (both cohorts): a 1% stop measured ONLY on bars AFTER entry — minute data
fixes the daily backtest's look-back-stop bias (a daily low that printed BEFORE
the entry could never really have stopped you). If never stopped, exit at the
force-close bar (15:51 ET). 1R = 1%, so expectancy_R is sizing-normalised.

Built-in discipline: in-sample vs out-of-sample (last 365d held out) + an SPY
buy-and-hold benchmark over the same window + a printed kill-criterion verdict.

DATA CAVEATS: free IEX feed ≈ regular hours only (premarket ~empty → I1 NOT
testable here) and IEX-only volume (~2-3% of consolidated → I3/RVOL needs paid
data to be meaningful). Survivorship bias inherited from the daily universe.
Read as 'is there a signal worth paying to confirm', not as truth.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent
sys.path.insert(0, str(PROJECT_ROOT))

from backtest.backtest_daily import (  # noqa: E402
    DATA_DIR, RULES_PATH, download_history, per_signal_stats,
    portfolio_curve, regime_series, signal_events,
)
from src.sp500_tickers import SP500_TICKERS  # noqa: E402

try:
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env")
except Exception:  # noqa: BLE001
    pass

ET = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")
ALPACA_BARS = "https://data.alpaca.markets/v2/stocks/{sym}/bars"
MIN_DIR = DATA_DIR / "minute"

RESULTS_DIR = HERE / "results"

# A liquid, high-beta slice where this gap-and-go strategy actually fires
# (megacap + semis + momentum names). Override with --tickers or --limit.
DEFAULT_UNIVERSE = [
    "NVDA", "AMD", "AVGO", "MU", "AMAT", "LRCX", "KLAC", "MRVL", "ON", "TER",
    "ANET", "SMCI", "TSLA", "META", "AMZN", "GOOGL", "NFLX", "CRM", "ADBE", "QCOM",
]

EARLIEST = "10:05"
LATEST = "15:30"
FORCE_CLOSE = "15:51"


# --------------------------------------------------------------------------- #
# Alpaca minute data (free IEX feed), cached per symbol-day
# --------------------------------------------------------------------------- #
def _alpaca_headers() -> dict:
    k = os.environ.get("ALPACA_API_KEY_ID")
    s = os.environ.get("ALPACA_API_SECRET_KEY")
    if not k or not s:
        raise RuntimeError("ALPACA_API_KEY_ID / ALPACA_API_SECRET_KEY not in env")
    return {"APCA-API-KEY-ID": k, "APCA-API-SECRET-KEY": s}


def fetch_minute_day(symbol: str, day_iso: str) -> list[dict] | None:
    """1-min bars for one ET day → [{et:'HH:MM',o,h,l,c,v}]. Cached. None on
    request failure (so we don't poison the cache with a transient error)."""
    cache = MIN_DIR / f"{symbol}_{day_iso}.json"
    if cache.exists():
        return json.loads(cache.read_text())
    d = date.fromisoformat(day_iso)
    start = datetime(d.year, d.month, d.day, 4, 0, tzinfo=ET).astimezone(UTC)
    end = datetime(d.year, d.month, d.day, 20, 0, tzinfo=ET).astimezone(UTC)
    bars: list[dict] = []
    token = None
    ok = False
    for _ in range(12):
        params = {
            "timeframe": "1Min",
            "start": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "feed": "iex", "limit": 10000, "sort": "asc",
        }
        if token:
            params["page_token"] = token
        try:
            r = requests.get(ALPACA_BARS.format(sym=symbol), params=params,
                             headers=_alpaca_headers(), timeout=25)
        except Exception:  # noqa: BLE001
            return None
        if r.status_code == 429:        # rate-limited → wait and retry
            time.sleep(2.0)
            continue
        if r.status_code != 200:
            return None
        ok = True
        j = r.json()
        bars.extend(j.get("bars") or [])
        token = j.get("next_page_token")
        if not token:
            break
    if not ok:
        return None
    out = []
    for b in bars:
        t_utc = datetime.strptime(b["t"][:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=UTC)
        t_et = t_utc.astimezone(ET)
        out.append({"et": t_et.strftime("%H:%M"), "o": b["o"], "h": b["h"],
                    "l": b["l"], "c": b["c"], "v": b["v"]})
    MIN_DIR.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(out))
    time.sleep(0.34)                    # ~180 req/min, under the 200/min free cap
    return out


# --------------------------------------------------------------------------- #
# Intraday reconstruction
# --------------------------------------------------------------------------- #
def _session(bars: list[dict]) -> list[dict]:
    return [b for b in bars if "09:30" <= b["et"] < "16:00"]


def _exit_R(sess: list[dict], entry_idx: int, entry_px: float, cost_pct: float) -> float:
    """1% stop on bars AFTER entry; else exit at the force-close bar close."""
    stop = entry_px * 0.99
    fc_px = None
    for b in sess[entry_idx + 1:]:
        if b["et"] > FORCE_CLOSE:
            break
        if b["l"] <= stop:
            return (-1.0 - cost_pct) / 1.0
        fc_px = b["c"]
    if fc_px is None:
        fc_px = sess[entry_idx]["c"]
    return ((fc_px / entry_px - 1) * 100 - cost_pct) / 1.0


def trade_open(sess: list[dict], cost_pct: float) -> dict | None:
    if len(sess) < 10:
        return None
    entry_px = sess[0]["o"]
    if entry_px <= 0:
        return None
    return {"entered": True, "entry_px": entry_px,
            "R": _exit_R(sess, 0, entry_px, cost_pct)}


def trade_i2(sess: list[dict], cost_pct: float) -> dict | None:
    """Enter at first HOD breakout inside [EARLIEST, LATEST]. None if no session;
    {'entered': False} if the day never broke its morning high (filtered out)."""
    if len(sess) < 10:
        return None
    running_hod = None
    for i, b in enumerate(sess):
        if running_hod is not None and EARLIEST <= b["et"] <= LATEST:
            if b["h"] > running_hod:               # breakout to a new HOD
                entry_px = running_hod              # fill ≈ the level broken
                if entry_px <= 0:
                    return {"entered": False}
                return {"entered": True, "entry_px": entry_px,
                        "entry_time": b["et"],
                        "R": _exit_R(sess, i, entry_px, cost_pct)}
        running_hod = b["h"] if running_hod is None else max(running_hod, b["h"])
    return {"entered": False}


# --------------------------------------------------------------------------- #
# Benchmark + reporting
# --------------------------------------------------------------------------- #
def spy_buy_hold(spy_df, start_iso: str, end_iso: str) -> dict:
    s = spy_df.loc[(spy_df.index >= start_iso) & (spy_df.index <= end_iso), "Close"]
    if len(s) < 2:
        return {}
    eq = (s / s.iloc[0])
    peak = eq.cummax()
    max_dd = float(((peak - eq) / peak).max() * 100)
    total = float((eq.iloc[-1] - 1) * 100)
    years = len(s) / 252
    cagr = float((eq.iloc[-1] ** (1 / years) - 1) * 100) if years > 0 else 0.0
    return {"total_return_pct": round(total, 1), "CAGR_pct": round(cagr, 1),
            "max_drawdown_pct": round(max_dd, 1)}


def _summarise(trades: list[dict], max_pos, max_sec, risk_pct) -> dict:
    return {"per_signal": per_signal_stats([t["R"] for t in trades]),
            "portfolio": portfolio_curve(trades, max_pos, max_sec, risk_pct)}


def _print_block(title, stats):
    ps, pf = stats["per_signal"], stats["portfolio"]
    print(f"  {title}")
    print(f"    n={ps.get('n_trades',0)}  win%={ps.get('win_rate_pct',0)}  "
          f"exp_R={ps.get('expectancy_R',0)}  PF={ps.get('profit_factor',0)}  "
          f"totR={ps.get('total_R',0)}")
    print(f"    portfolio: total={pf.get('total_return_pct',0)}%  "
          f"CAGR={pf.get('CAGR_pct',0)}%  maxDD={pf.get('max_drawdown_pct',0)}%")


# --------------------------------------------------------------------------- #
def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--period", default="5y", help="daily cache period (reuses daily_{period}.pkl)")
    ap.add_argument("--cost-bps", type=float, default=10.0)
    ap.add_argument("--tickers", default="", help="comma list; default = liquid slice")
    ap.add_argument("--limit", type=int, default=0, help="use first N of SP500 instead of the slice")
    ap.add_argument("--start", default="", help="ignore signals before this ISO date")
    ap.add_argument("--oos-days", type=int, default=365, help="hold out the last N days")
    ap.add_argument("--max-signals", type=int, default=0, help="cap fetched signal-days (cost guard)")
    args = ap.parse_args(argv)

    rules = json.loads(RULES_PATH.read_text())
    p = {"gap_pct": float(rules["daily_filters"]["D3_min_gap_pct_from_prior_close"]),
         "min_price": float(rules["universe_filters"]["min_price_usd"])}
    risk_pct = float(rules["risk"]["max_risk_per_trade_pct"])
    max_pos = int(rules["risk"]["max_concurrent_positions"])
    max_sec = int(rules["risk"]["max_positions_per_sector"])
    cost_pct = args.cost_bps / 100

    if args.tickers:
        universe = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
    elif args.limit:
        universe = list(SP500_TICKERS[:args.limit])
    else:
        universe = DEFAULT_UNIVERSE

    cache = DATA_DIR / f"daily_{args.period}.pkl"
    data = download_history(list(universe) + ["SPY"], args.period, cache, False)
    if "SPY" not in data:
        print("FATAL: no SPY in daily cache", file=sys.stderr)
        return 1
    regime = regime_series(data["SPY"])

    events = []
    for sym in universe:
        if sym in data:
            events.extend(signal_events(sym, data[sym], regime, p))
    if args.start:
        events = [e for e in events if e["date"] >= args.start]
    events.sort(key=lambda e: e["date"])
    if args.max_signals and len(events) > args.max_signals:
        print(f"[cap] {len(events)} signals → keeping the most recent {args.max_signals}")
        events = events[-args.max_signals:]
    if not events:
        print("No signals for this universe/period.", file=sys.stderr)
        return 1

    print(f"[run] {len(universe)} names · {len(events)} signal-days "
          f"({events[0]['date']} → {events[-1]['date']}) · fetching minute bars…")

    open_tr, i2_tr = [], []
    no_data = no_break = 0
    for n, e in enumerate(events, 1):
        bars = fetch_minute_day(e["symbol"], e["date"])
        if bars is None:
            no_data += 1
            continue
        sess = _session(bars)
        ot = trade_open(sess, cost_pct)
        it = trade_i2(sess, cost_pct)
        if ot is None or it is None:
            no_data += 1
            continue
        base = {"date": e["date"], "sector": e["sector"], "gap_pct": e["gap_pct"]}
        open_tr.append({**base, "R": ot["R"]})
        if it.get("entered"):
            i2_tr.append({**base, "R": it["R"]})
        else:
            no_break += 1
        if n % 25 == 0:
            print(f"  …{n}/{len(events)} (no_data={no_data}, no_breakout={no_break})")

    if not open_tr:
        print("FATAL: no usable minute data fetched.", file=sys.stderr)
        return 1

    last = open_tr[-1]["date"]
    cutoff = (date.fromisoformat(last) - timedelta(days=args.oos_days)).isoformat()
    first = open_tr[0]["date"]

    def split(trs):
        return ([t for t in trs if t["date"] < cutoff],
                [t for t in trs if t["date"] >= cutoff])
    open_is, open_oos = split(open_tr)
    i2_is, i2_oos = split(i2_tr)

    spy = spy_buy_hold(data["SPY"], first, last)
    spy_oos = spy_buy_hold(data["SPY"], cutoff, last)

    print("\n" + "=" * 70)
    print("  TIER-2 INTRADAY BACKTEST — I2 (HOD-breakout entry) vs OPEN entry")
    print("=" * 70)
    print(f"  universe {len(universe)} · usable signals {len(open_tr)} "
          f"· I2 entered {len(i2_tr)} (skipped {no_break} no-breakout, "
          f"{no_data} no-data)")
    print(f"  span {first} → {last} · IS/OOS cut {cutoff} · cost {args.cost_bps:.0f}bps")
    print("-" * 70)
    print(" FULL SAMPLE")
    _print_block("OPEN entry (baseline, no intraday filter):",
                 _summarise(open_tr, max_pos, max_sec, risk_pct))
    _print_block("I2   entry (intraday HOD breakout):",
                 _summarise(i2_tr, max_pos, max_sec, risk_pct))
    print("-" * 70)
    print(f" IN-SAMPLE ({first} → {cutoff})")
    _print_block("OPEN:", _summarise(open_is, max_pos, max_sec, risk_pct))
    _print_block("I2  :", _summarise(i2_is, max_pos, max_sec, risk_pct))
    print(f" OUT-OF-SAMPLE ({cutoff} → {last})  ← the honest test")
    _print_block("OPEN:", _summarise(open_oos, max_pos, max_sec, risk_pct))
    _print_block("I2  :", _summarise(i2_oos, max_pos, max_sec, risk_pct))
    print("-" * 70)
    print(f"  SPY buy&hold  full: {spy}")
    print(f"  SPY buy&hold  OOS : {spy_oos}")
    print("-" * 70)

    # kill-criterion verdict
    e_open = per_signal_stats([t["R"] for t in open_tr]).get("expectancy_R", 0)
    e_i2 = per_signal_stats([t["R"] for t in i2_tr]).get("expectancy_R", 0)
    e_i2_oos = per_signal_stats([t["R"] for t in i2_oos]).get("expectancy_R", 0)
    pf_i2 = _summarise(i2_tr, max_pos, max_sec, risk_pct)["portfolio"]
    lift = e_i2 > e_open + 0.02
    oos_ok = e_i2_oos > 0.02
    beats_spy = pf_i2.get("total_return_pct", 0) > (spy.get("total_return_pct", 0) or 0)
    verdict = "INVESTIGATE FURTHER" if (lift and oos_ok and beats_spy) else "KILL — no edge"
    print(f"  KILL-CRITERION: I2 lift over open={lift} (exp {e_open}→{e_i2}); "
          f"OOS I2 exp_R={e_i2_oos} (>0.02? {oos_ok}); beats SPY total={beats_spy}")
    print(f"  >>> VERDICT: {verdict}")
    print("  (I1 premarket-high & I3 consolidated-RVOL need PAID data — only worth")
    print("   it if the verdict above is INVESTIGATE.)")
    print("=" * 70 + "\n")

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    (RESULTS_DIR / "intraday_comparison.json").write_text(json.dumps({
        "params": {**p, "cost_bps": args.cost_bps, "span": [first, last],
                   "oos_cut": cutoff, "universe": universe,
                   "n_usable": len(open_tr), "n_i2_entered": len(i2_tr),
                   "no_breakout": no_break, "no_data": no_data},
        "full": {"open": _summarise(open_tr, max_pos, max_sec, risk_pct),
                 "i2": _summarise(i2_tr, max_pos, max_sec, risk_pct)},
        "in_sample": {"open": _summarise(open_is, max_pos, max_sec, risk_pct),
                      "i2": _summarise(i2_is, max_pos, max_sec, risk_pct)},
        "out_of_sample": {"open": _summarise(open_oos, max_pos, max_sec, risk_pct),
                          "i2": _summarise(i2_oos, max_pos, max_sec, risk_pct)},
        "spy": {"full": spy, "oos": spy_oos},
        "verdict": verdict,
    }, indent=2))
    print(f"[out] intraday_comparison.json → {RESULTS_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
