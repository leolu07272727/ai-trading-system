#!/usr/bin/env python3
"""EXPERIMENT C — SEC Form 4 insider-transaction scan for the memory thesis monitor.

Insiders (officers/directors/10% owners) must file Form 4 within 2 business days
of trading their own stock. Open-market BUYS with their own money are one of the
few public signals with academic support (cluster buys especially — multiple
insiders buying in the same window). Sells are weak evidence (tax, diversification,
divorce...) — rendered but never treated as a bear signal on their own.

Role in the thesis: a lie detector, not an entry signal. If the AI-memory thesis
is intact, insiders at MU/WDC/STX shouldn't be dumping into the rally — and any
non-plan cluster buying while the Street is still catching up is hard confirmation.

Deliberately SEPARATE from monitor.verdict(): renders a parallel line and logs
into the snapshot, but does NOT change the EARLY/MID/LATE state. Best-effort —
returns None on any failure (no CIK for foreign listings like 000660.KS, network
error, SEC throttling), so the monitor never breaks if this is off.

Data: SEC EDGAR (free, no key; requires a descriptive User-Agent, ~10 req/s max).
Transactions counted: non-derivative open-market only — code P (purchase) and
S (sale). Grants/awards (A), option exercises (M), tax withholding (F) etc. are
compensation plumbing, not conviction, and are ignored. Rule 10b5-1 pre-planned
trades are counted but flagged: a plan buy is scheduled, not a fresh opinion.

Usage:
    python thesis_monitor/insider_scan.py MU        # standalone test
    python thesis_monitor/monitor.py --insider      # folded into the monitor
"""
from __future__ import annotations

import json
import sys
import time
import xml.etree.ElementTree as XET
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
CIK_CACHE = HERE / ".cik_map.json"
CIK_CACHE_DAYS = 30

# SEC asks for a real contact in the UA; anonymous UAs get 403'd.
UA = {"User-Agent": "thesis-monitor/1.0 (contact: luweilinsitang@gmail.com)"}
SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik10}.json"
TICKER_MAP = "https://www.sec.gov/files/company_tickers.json"
ARCHIVES = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{doc}"

LOOKBACK_DAYS = 90
MAX_FILINGS = 40          # request cap per ticker; result carries capped=True if hit
REQ_PAUSE = 0.15          # stay well under SEC's 10 req/s


def _get(url: str, timeout: int = 15):
    import requests

    resp = requests.get(url, headers=UA, timeout=timeout)
    resp.raise_for_status()
    return resp


# ───────────────────────── ticker → CIK ─────────────────────────

def _cik_for(ticker: str) -> str | None:
    """10-digit zero-padded CIK, from a locally cached copy of SEC's ticker map.
    None for anything not US-listed (e.g. 000660.KS) — caller degrades to no-read."""
    mapping: dict[str, str] = {}
    if CIK_CACHE.exists():
        age = time.time() - CIK_CACHE.stat().st_mtime
        if age < CIK_CACHE_DAYS * 86400:
            try:
                mapping = json.loads(CIK_CACHE.read_text(encoding="utf-8"))
            except Exception:
                mapping = {}
    if not mapping:
        raw = _get(TICKER_MAP).json()
        mapping = {v["ticker"].upper(): str(v["cik_str"]).zfill(10) for v in raw.values()}
        CIK_CACHE.write_text(json.dumps(mapping), encoding="utf-8")
    return mapping.get(ticker.upper())


# ───────────────────────── Form 4 fetch + parse ─────────────────────────

def _recent_form4(cik10: str, lookback_days: int) -> tuple[list[dict], bool]:
    """[{acc, doc, filed}] for form-4 filings within lookback. (list, capped)."""
    data = _get(SUBMISSIONS.format(cik10=cik10)).json()
    recent = data.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    accs = recent.get("accessionNumber", [])
    docs = recent.get("primaryDocument", [])
    dates = recent.get("filingDate", [])
    cutoff = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).date().isoformat()

    out, capped = [], False
    for form, acc, doc, filed in zip(forms, accs, docs, dates):
        if form != "4" or filed < cutoff:   # skip 4/A amendments to avoid double counting
            continue
        if len(out) >= MAX_FILINGS:
            capped = True
            break
        out.append({"acc": acc, "doc": doc, "filed": filed})
    return out, capped


def _txt(node, path: str) -> str:
    el = node.find(path)
    return (el.text or "").strip() if el is not None and el.text else ""


def _parse_form4(cik10: str, filing: dict) -> list[dict]:
    """Open-market P/S transactions out of one Form 4. [] if none/unparseable."""
    # primaryDocument is often "xslF345X05/foo.xml" — strip the stylesheet prefix
    # to get the raw XML the stylesheet renders.
    doc = filing["doc"].split("/")[-1]
    url = ARCHIVES.format(cik=int(cik10), acc=filing["acc"].replace("-", ""), doc=doc)
    try:
        root = XET.fromstring(_get(url).content)
    except Exception:
        return []

    owners = []
    for ro in root.findall("reportingOwner"):
        name = _txt(ro, "reportingOwnerId/rptOwnerName")
        rel = ro.find("reportingOwnerRelationship")
        role = ""
        if rel is not None:
            if _txt(rel, "officerTitle"):
                role = _txt(rel, "officerTitle")
            elif _txt(rel, "isDirector") in ("1", "true"):
                role = "Director"
            elif _txt(rel, "isTenPercentOwner") in ("1", "true"):
                role = "10% owner"
        owners.append((name, role))
    owner_name = "; ".join(n for n, _ in owners) or "?"
    owner_role = owners[0][1] if owners else ""
    plan = _txt(root, "aff10b5One") in ("1", "true")  # 10b5-1 checkbox (2023+ forms)

    txns = []
    for t in root.findall("nonDerivativeTable/nonDerivativeTransaction"):
        code = _txt(t, "transactionCoding/transactionCode")
        if code not in ("P", "S"):
            continue
        try:
            shares = float(_txt(t, "transactionAmounts/transactionShares/value") or 0)
            price = float(_txt(t, "transactionAmounts/transactionPricePerShare/value") or 0)
        except ValueError:
            shares = price = 0.0
        txns.append({
            "name": owner_name,
            "role": owner_role,
            "date": _txt(t, "transactionDate/value") or filing["filed"],
            "code": code,
            "shares": shares,
            "value": shares * price,
            "plan": plan,
        })
    return txns


# ───────────────────────── scan + verdict ─────────────────────────

def scan(ticker: str, lookback_days: int = LOOKBACK_DAYS) -> dict | None:
    """Aggregate insider read for one ticker, or None if no data available."""
    try:
        cik10 = _cik_for(ticker)
        if not cik10:
            return None
        filings, capped = _recent_form4(cik10, lookback_days)
    except Exception:
        return None

    txns: list[dict] = []
    for f in filings:
        txns.extend(_parse_form4(cik10, f))
        time.sleep(REQ_PAUSE)

    buys = [t for t in txns if t["code"] == "P"]
    sells = [t for t in txns if t["code"] == "S"]
    conviction_buyers = sorted({t["name"] for t in buys if not t["plan"]})
    return {
        "ticker": ticker.upper(),
        "lookback_days": lookback_days,
        "n_filings": len(filings),
        "capped": capped,
        "n_buys": len(buys),
        "buy_usd": sum(t["value"] for t in buys),
        "plan_buys": sum(1 for t in buys if t["plan"]),
        "buyers": conviction_buyers,
        "cluster_buy": len(conviction_buyers) >= 2,
        "n_sells": len(sells),
        "sell_usd": sum(t["value"] for t in sells),
        "sellers": len({t["name"] for t in sells}),
        "buys_detail": sorted(buys, key=lambda t: t["date"], reverse=True)[:8],
    }


def compact(res: dict) -> dict:
    """Small dict for the history/<ticker>.jsonl snapshot (drift-watching)."""
    return {k: res[k] for k in ("n_buys", "buy_usd", "plan_buys", "cluster_buy",
                                "n_sells", "sell_usd", "sellers", "capped")}


def _usd(v: float) -> str:
    if v >= 1e9:
        return f"${v/1e9:.1f}B"
    if v >= 1e6:
        return f"${v/1e6:.1f}M"
    if v >= 1e3:
        return f"${v/1e3:.0f}K"
    return f"${v:.0f}"


def render(res: dict | None) -> str:
    """One-line human render for the monitor. '' if no read (foreign ticker etc.)."""
    if not res:
        return ""
    if not res["n_buys"] and not res["n_sells"]:
        base = f"🕵️ 內部人{res['lookback_days']}天: 無公開市場買賣(P/S)"
        return base + ("  ⚠ 只掃了最近 %d 份申報" % MAX_FILINGS if res["capped"] else "")

    buy_part = (f"買 {res['n_buys']}筆/{_usd(res['buy_usd'])}"
                + (f"({len(res['buyers'])}人自主" + (f"+{res['plan_buys']}筆計畫" if res["plan_buys"] else "") + ")"
                   if res["n_buys"] else ""))
    sell_part = f"賣 {res['n_sells']}筆/{_usd(res['sell_usd'])}({res['sellers']}人)"

    if res["cluster_buy"]:
        read = "🟢 集體自主買入 — 罕見的看多硬訊號,thesis 獲內部人背書"
    elif res["buyers"]:
        read = "🟡 有人自掏腰包買 — 單人樣本,參考"
    elif res["n_buys"]:
        read = "→ 買單全是 10b5-1 排程,非新觀點"
    elif res["sell_usd"] > 0 and res["sellers"] >= 3:
        read = "→ 多人在賣(高管賣理由百百種,不當空頭訊號;但 thesis 說被低估、他們卻沒人買)"
    else:
        read = "→ 只有零星賣出,中性"
    capped = "  ⚠ 申報量大,只掃最近 %d 份" % MAX_FILINGS if res["capped"] else ""
    return f"🕵️ 內部人{res['lookback_days']}天: {buy_part} vs {sell_part} — {read}{capped}"


if __name__ == "__main__":
    tk = sys.argv[1] if len(sys.argv) > 1 else "MU"
    r = scan(tk)
    print(json.dumps(r, ensure_ascii=False, indent=2, default=str) if r else "(no read)")
    print(render(r))
