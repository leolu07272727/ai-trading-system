"""Entry-signal evaluation for the Trend Join Long strategy.

evaluate() applies the regime, holding, time, daily and intraday filters in a
fixed order, returning early on the first failure. Every call appends one JSON
line to logs/decision_log.jsonl regardless of the outcome.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf

from src.ibkr_client import IBKRClient
from src.sp500_tickers import get_sector

ET = ZoneInfo("America/New_York")

_ROOT = Path(__file__).resolve().parent
_CONFIG_PATH = _ROOT / "config" / "rules.json"
_LOG_PATH = _ROOT / "logs" / "decision_log.jsonl"
_DAILY_CACHE_DIR = _ROOT / "logs" / ".daily_cache"


def _load_rules() -> dict:
    with _CONFIG_PATH.open(encoding="utf-8") as handle:
        return json.load(handle)


def _daily_cache_get(key: str):
    """Same-ET-date file cache for daily-bar derived values.

    cycle.py is a fresh process every cron tick, so an in-memory cache would be
    useless — this cuts the repeated per-tick yfinance downloads (SMA200, prior
    day OHLC) that were hitting rate limits. Values derived from completed daily
    bars (and today's open) don't change intraday. Failures are never cached.
    """
    path = _DAILY_CACHE_DIR / f"{key}.json"
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("date") == datetime.now(ET).date().isoformat():
                return data.get("value")
    except Exception:  # noqa: BLE001 - cache is best-effort
        pass
    return None


def _daily_cache_put(key: str, value) -> None:
    try:
        _DAILY_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        payload = {"date": datetime.now(ET).date().isoformat(), "value": value}
        path = _DAILY_CACHE_DIR / f"{key}.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        tmp.replace(path)
    except Exception:  # noqa: BLE001 - cache is best-effort
        pass


def _yf_column(df: pd.DataFrame, name: str, symbol: str) -> pd.Series:
    """Return a single OHLCV column as a Series, flattening yfinance MultiIndex."""
    if isinstance(df.columns, pd.MultiIndex):
        if (name, symbol) in df.columns:
            return df[(name, symbol)]
        return df.xs(name, axis=1, level=0).iloc[:, 0]
    return df[name]


def _sma200(symbol: str) -> float | None:
    cached = _daily_cache_get(f"sma200_{symbol}")
    if cached is not None:
        return float(cached)
    df = yf.download(
        symbol, period="1y", interval="1d", auto_adjust=True, progress=False
    )
    if df is None or df.empty:
        return None
    close = _yf_column(df, "Close", symbol).dropna()
    if len(close) < 200:
        return None
    sma = float(close.tail(200).mean())
    _daily_cache_put(f"sma200_{symbol}", sma)
    return sma


def _to_et(series: pd.Series) -> pd.Series:
    ts = pd.to_datetime(series)
    if ts.dt.tz is None:
        return ts.dt.tz_localize(ET)
    return ts.dt.tz_convert(ET)


def evaluate(
    symbol: str,
    ibkr_client: IBKRClient,
    existing_positions: list[dict] | None = None,
    rules: dict | None = None,
    regime_ok: bool | None = None,
) -> dict:
    """Evaluate one symbol against rules.json.

    rules: pass the already-parsed rules.json when scanning many symbols in one
    cycle to skip re-reading the file each call.
    regime_ok: pass the cycle's already-computed SPY-regime result to skip
    redoing that check (a yfinance download + IBKR snapshot) per candidate.
    Leave both None for standalone use (e.g. bot.py's single-symbol check).
    """
    if rules is None:
        rules = _load_rules()
    test_mode = rules.get("test_mode", {})
    relax = bool(test_mode.get("relax_filters", False))

    result: dict = {
        "symbol": symbol,
        "pass": False,
        "reasons": [],
        "price": None,
        "sector": get_sector(symbol),
        "stop_price": None,
        "gap_pct": None,
    }

    def finalize(passed: bool, reasons: list[str]) -> dict:
        result["pass"] = passed
        result["reasons"] = reasons
        _append_log(symbol, result)
        return result

    # 1. REGIME FILTER (§Fix4)
    regime = rules.get("regime_filter", {})
    if regime.get("enabled"):
        if regime_ok is not None:
            if not regime_ok:
                return finalize(False, ["regime: SPY below SMA200 (precomputed)"])
        else:
            spy_sma = _sma200("SPY")
            spy_price = ibkr_client.get_snapshot_price("SPY")
            if spy_sma is None:
                return finalize(False, ["regime: SPY SMA200 unavailable"])
            if spy_price < spy_sma:
                return finalize(False, ["regime: SPY below SMA200"])

    # 2. ALREADY HOLDING
    if existing_positions:
        for pos in existing_positions:
            if pos.get("symbol") == symbol and float(pos.get("qty", 0)) > 0:
                return finalize(False, ["already_holding"])

    # 3. TIME WINDOW
    time_filter = rules.get("time_filter", {})
    now_et = datetime.now(ET).time()
    earliest = datetime.strptime(time_filter["earliest_entry_et"], "%H:%M").time()
    latest = datetime.strptime(time_filter["latest_entry_et"], "%H:%M").time()
    if not (earliest <= now_et <= latest):
        return finalize(False, ["outside_time_window"])

    # 4. DAILY FILTERS (yfinance daily, cached per ET day — the time filter above
    # guarantees we only get here ≥ earliest_entry, so today's open is final).
    daily = rules.get("daily_filters", {})
    day_data = _daily_cache_get(f"daily_{symbol}")
    if day_data is None:
        day_df = yf.download(
            symbol, period="5d", interval="1d", auto_adjust=True, progress=False
        )
        if day_df is None or len(day_df) < 2:
            return finalize(False, ["daily_data_unavailable"])

        opens = _yf_column(day_df, "Open", symbol)
        highs = _yf_column(day_df, "High", symbol)
        closes = _yf_column(day_df, "Close", symbol)
        day_data = {
            "today_open": float(opens.iloc[-1]),
            "prior_high": float(highs.iloc[-2]),
            "prior_close": float(closes.iloc[-2]),
        }
        _daily_cache_put(f"daily_{symbol}", day_data)

    today_open = float(day_data["today_open"])
    prior_high = float(day_data["prior_high"])
    prior_close = float(day_data["prior_close"])

    gap_pct = (today_open - prior_close) / prior_close * 100.0
    result["gap_pct"] = float(gap_pct)

    if daily.get("D1_above_prior_day_high") and not (today_open > prior_high):
        return finalize(False, ["D1: today open below prior day high"])

    if daily.get("D2_prior_close_above_sma200"):
        sym_sma = _sma200(symbol)
        if sym_sma is None:
            return finalize(False, ["D2: SMA200 unavailable"])
        if not (prior_close > sym_sma):
            return finalize(False, ["D2: prior close below SMA200"])

    gap_threshold = daily.get("D3_min_gap_pct_from_prior_close", 0.0)
    if relax and "relaxed_min_gap_pct" in test_mode:
        gap_threshold = test_mode["relaxed_min_gap_pct"]
    if gap_pct < gap_threshold:
        return finalize(False, [f"D3: gap {gap_pct:.2f}% below {gap_threshold}%"])

    # 5. INTRADAY FILTERS (IBKR real-time, §Fix1)
    intraday = rules.get("intraday_filters", {})
    snapshot_price = float(ibkr_client.get_snapshot_price(symbol))
    result["price"] = snapshot_price

    bars_5m = ibkr_client.get_5m_bars(symbol, lookback=78)
    if bars_5m.empty:
        return finalize(False, ["intraday_bars_unavailable"])

    bar_times = _to_et(bars_5m["date"])
    today_date = datetime.now(ET).date()
    today_mask = bar_times.dt.date == today_date
    today_bars = bars_5m[today_mask]
    if today_bars.empty:
        return finalize(False, ["no_intraday_bars_today"])

    # (I1 premarket-high filter removed 2026-07: get_5m_bars uses useRTH=True so
    # premarket bars were never returned — the filter was dead code since day one
    # and was never validated by any backtest.)

    # I2: above today's high of day so far.
    # Exclude the still-forming current bar — reqHistoricalData(endDateTime="")
    # returns the partial bar as the last row, and its high already captures the
    # latest tick, so including it makes "price > HOD" structurally unsatisfiable.
    prior_highs = today_bars["high"].iloc[:-1]
    today_hod = float(prior_highs.max()) if len(prior_highs) else float("inf")
    skip_i2 = relax and bool(test_mode.get("relaxed_skip_I2", False))
    if intraday.get("I2_above_today_hod") and not skip_i2 and not (snapshot_price > today_hod):
        return finalize(False, ["I2: below today HOD"])
    # Experiment-mode replacement for I2's "still strong today" intent: the
    # snapshot must at least sit above today's open (green on the day). Keeps a
    # momentum sanity check without racing the 5-minute cron sampling for a
    # fresh HOD — which made I2 near-unpassable live, and which the Tier-2
    # backtest showed was a NEGATIVE contribution anyway.
    if skip_i2 and test_mode.get("relaxed_require_above_open", True):
        if not (snapshot_price > today_open):
            return finalize(False, ["I2R: not above today open"])

    # I3: RVOL — today's cumulative volume vs the average cumulative volume AT
    # THE SAME TIME OF DAY over the lookback. The old definition divided by the
    # average FULL-day volume, which made RVOL≥2 structurally unreachable before
    # the afternoon (partial day vs whole days) and contradicted the morning
    # entry window. Only candidates that already cleared I2 pay for the extra
    # multi-day bar fetch, so the cost is rare.
    today_volume = float(today_bars["volume"].sum())
    lookback_days = int(intraday.get("I3_rvol_lookback_days", 14))
    now_time = datetime.now(ET).time()
    avg_cum_volume = _avg_cum_volume_at_time(ibkr_client, symbol, lookback_days, now_time)
    rvol_min = float(intraday.get("I3_rvol_min", 0.0))
    if relax and "relaxed_rvol_min" in test_mode:
        rvol_min = float(test_mode["relaxed_rvol_min"])
    if avg_cum_volume and avg_cum_volume > 0:
        rvol = today_volume / avg_cum_volume
        if rvol < rvol_min:
            return finalize(False, [f"I3: RVOL {rvol:.2f} below min {rvol_min}"])
    else:
        return finalize(False, ["I3: avg volume unavailable"])

    # 6. INITIAL STOP
    today_low = float(today_bars["low"].min())
    if rules.get("exit", {}).get("initial_stop_rule") == "lod_minus_1pct":
        result["stop_price"] = float(round(today_low * 0.99, 2))

    # 7. SECTOR already set.
    # 8. CONTEXT FOR THE LLM JUDGE — populate the rich fields the overlay
    #    surfaces so the judge can also catch over-extension / parabolic chases,
    #    not just news/earnings. Best-effort; this PASS path is rare (≤ a few a
    #    day), so the extra daily download in _daily_technicals is cheap.
    result["rvol"] = float(rvol)
    result["prior_close"] = float(prior_close)
    result["today_open"] = float(today_open)
    result["today_hod"] = None if today_hod == float("inf") else float(today_hod)
    result["today_low"] = float(today_low)
    result["ext_above_open_pct"] = (
        round((snapshot_price / today_open - 1) * 100.0, 2) if today_open else None
    )
    result["tech"] = _daily_technicals(symbol)
    return finalize(True, ["all_filters_passed"])


def _avg_cum_volume_at_time(
    ibkr_client: IBKRClient, symbol: str, lookback_days: int, cutoff
) -> float | None:
    """Average cumulative RTH volume up to `cutoff` (ET time-of-day) over the
    last `lookback_days` completed sessions, from IBKR 5m bars. None on failure
    or if fewer than 5 prior sessions are available.

    Bar-count padding uses a 7/5 calendar-to-trading-day ratio (+4 for holidays)
    because IBKR durationStr "N D" spans CALENDAR days — a naive lookback_days+2
    pad only fetched ~10 sessions when 14 were requested."""
    try:
        pad_days = lookback_days * 7 // 5 + 4
        bars = ibkr_client.get_5m_bars(symbol, lookback=78 * pad_days)
        if bars.empty:
            return None
        times = _to_et(bars["date"])
        today = datetime.now(ET).date()
        frame = pd.DataFrame(
            {
                "day": times.dt.date,
                "time": times.dt.time,
                "volume": bars["volume"].astype(float),
            }
        )
        prior = frame[(frame["day"] < today) & (frame["time"] <= cutoff)]
        if prior.empty:
            return None
        per_day = prior.groupby("day")["volume"].sum().tail(lookback_days)
        if len(per_day) < 5:
            return None
        return float(per_day.mean())
    except Exception:  # noqa: BLE001 - caller treats None as "unavailable"
        return None


def _daily_technicals(symbol: str) -> dict:
    """Best-effort multi-day technical context for the LLM judge.

    Computes recent momentum / extension / volatility from ~6mo of daily bars so
    the judge can flag over-extended or parabolic chases (its biggest blind spot
    — the rule engine only confirms the gap/breakout, not how stretched it is).
    Returns {} on any failure; the judge simply gets less context. Only called on
    a fully-passing candidate, so the extra download is rare.
    """
    try:
        df = yf.download(
            symbol, period="6mo", interval="1d", auto_adjust=True, progress=False
        )
        if df is None or len(df) < 50:
            return {}
        df = df.dropna()
        close = _yf_column(df, "Close", symbol)
        high = _yf_column(df, "High", symbol)
        low = _yf_column(df, "Low", symbol)
        if len(close) < 50:
            return {}
        price = float(close.iloc[-1])

        def _pct_change(n: int):
            if len(close) <= n:
                return None
            past = float(close.iloc[-1 - n])
            return round((price / past - 1) * 100.0, 2) if past else None

        def _dist_sma(n: int):
            if len(close) < n:
                return None
            sma = float(close.tail(n).mean())
            return round((price / sma - 1) * 100.0, 2) if sma else None

        # ATR14 as % of price (simple mean of true range)
        prev_close = close.shift(1)
        tr = pd.concat(
            [high - low, (high - prev_close).abs(), (low - prev_close).abs()],
            axis=1,
        ).max(axis=1)
        atr = float(tr.tail(14).mean())
        atr_pct = round(atr / price * 100.0, 2) if price else None

        # 20-day range position: 0.0 = at 20d low, 1.0 = at/above 20d high
        hi20 = float(high.tail(20).max())
        lo20 = float(low.tail(20).min())
        rng = hi20 - lo20
        range_pos_20d = round((price - lo20) / rng, 2) if rng > 0 else None

        rets = close.tail(6).pct_change().dropna()
        up_days_last5 = int((rets > 0).sum())

        return {
            "pct_change_5d": _pct_change(5),
            "pct_change_10d": _pct_change(10),
            "pct_change_20d": _pct_change(20),
            "dist_from_sma20_pct": _dist_sma(20),
            "dist_from_sma50_pct": _dist_sma(50),
            "atr14_pct": atr_pct,
            "range_pos_20d": range_pos_20d,
            "up_days_last5": up_days_last5,
        }
    except Exception:
        return {}


def _append_log(symbol: str, result: dict) -> None:
    _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "ts": datetime.now(ET).isoformat(),
        "symbol": symbol,
        "pass": bool(result["pass"]),
        "reasons": list(result["reasons"]),
        "price": None if result["price"] is None else float(result["price"]),
        "sector": str(result["sector"]),
        "stop_price": None if result["stop_price"] is None else float(result["stop_price"]),
        "gap_pct": None if result["gap_pct"] is None else float(result["gap_pct"]),
    }
    with _LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry) + "\n")
