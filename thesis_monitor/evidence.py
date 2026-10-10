#!/usr/bin/env python3
"""Evidence Engine — V2 Phase 1 (BOT_V2_PROPOSAL.md §2.2)

Reads theses.yaml (human-owned, machine read-only), evaluates each active thesis
against today's data, and emits a STRUCTURED daily record per thesis:

    { ts, health, tickers: {TK: {state, confirmed_state, evidence_for[],
      evidence_against[]}}, falsifier_proximity{}, suggestions[] }

appended to evidence/<thesis_id>.jsonl. This is the machine-readable layer the
old monitor.py (human dashboard) doesn't have. monitor.py stays untouched —
this module reuses its fetch()/verdict() so the two can never disagree.

Hard rules implemented here (see proposal §2.2-2.3):
  - cycle_data.json stale >14 days → cycle signals invalid, NEW ENTRIES BLOCKED
  - eps_trend single-day jump >50pp vs last record → data suspect, no state
    transition suggested that day (dirty data is worse than no data)
  - state needs 3 consecutive daily records agreeing before it is "confirmed" —
    suggestions fire on confirmed state only (damping; falsifier exits excepted)

Phase 1 has NO execution: a human reads the suggestions and records every
decision ex-ante via decide.py. The machine's job is evidence, not orders.

Usage:
    python thesis_monitor/evidence.py             # all active theses
    python thesis_monitor/evidence.py --insider   # + SEC Form 4 read (slower)
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

import monitor  # same-dir import; single source of truth for fetch()/verdict()

ET = ZoneInfo("America/New_York")
HERE = Path(__file__).resolve().parent
THESES_FILE = HERE / "theses.yaml"
EVIDENCE_DIR = HERE / "evidence"
CYCLE_FILE = HERE / "cycle_data.json"

CYCLE_STALE_DAYS = 14
EPS_JUMP_SUSPECT_PP = 50.0   # percentage-point day-over-day jump → dirty data
CONFIRM_DAYS = 3             # state must repeat this many records to confirm

REQUIRED_FIELDS = ["id", "statement", "tickers", "falsifiers", "entry", "add",
                   "trim", "exit", "max_weight", "review_by", "status"]


# ───────────────────────── registry ─────────────────────────

def load_theses() -> list[dict]:
    """Active theses only. A thesis missing any required field is REJECTED
    loudly (proposal §2.1: 欄位全部必填,缺一個就不准啟用)."""
    raw = yaml.safe_load(THESES_FILE.read_text(encoding="utf-8")) or []
    out = []
    for th in raw:
        missing = [f for f in REQUIRED_FIELDS if f not in th]
        if missing:
            print(f"✗ thesis '{th.get('id', '?')}' 缺必填欄位 {missing} — 拒絕載入")
            continue
        if str(th.get("review_by")) < datetime.now(ET).strftime("%Y-%m-%d"):
            print(f"✗ thesis '{th['id']}' 已過 review_by={th['review_by']} — 視為 expired,"
                  "人工重審後改日期才會再載入")
            continue
        if th["status"] == "active":
            out.append(th)
    return out


# ───────────────────────── data health ─────────────────────────

def check_health() -> dict:
    h = {"cycle_ok": False, "cycle_stale_days": None, "warnings": [], "entries_blocked": False}
    cyc = monitor.load_cycle()
    if not cyc or "last_updated" not in cyc:
        h["warnings"].append("cycle_data.json 缺失/無日期 → 循環訊號失效,禁止新進場")
        h["entries_blocked"] = True
        return h
    try:
        age = (datetime.now(ET).date()
               - datetime.strptime(cyc["last_updated"], "%Y-%m-%d").date()).days
    except ValueError:
        age = None
    h["cycle_stale_days"] = age
    if age is None or age > CYCLE_STALE_DAYS:
        h["warnings"].append(f"cycle_data.json 已 {age} 天未更新(上限 {CYCLE_STALE_DAYS})"
                             " → 循環訊號失效,禁止新進場,先去更新現貨價/capex 再說")
        h["entries_blocked"] = True
    else:
        h["cycle_ok"] = True
        if age >= CYCLE_STALE_DAYS - 4:
            h["warnings"].append(f"cycle_data.json 已 {age} 天未更新,再 {CYCLE_STALE_DAYS - age} 天失效")
    return h


def _last_records(thesis_id: str, n: int) -> list[dict]:
    f = EVIDENCE_DIR / f"{thesis_id}.jsonl"
    if not f.exists():
        return []
    lines = f.read_text(encoding="utf-8").strip().splitlines()
    return [json.loads(x) for x in lines[-n:]]


# ───────────────────────── falsifiers ─────────────────────────

def falsifier_proximity(thesis: dict, metrics: dict[str, dict]) -> dict:
    """Per-falsifier status: ok / warning / triggered / unknown, with a reason.
    Keyed by the falsifier ids in theses.yaml. Stale cycle_data → its falsifiers
    go UNKNOWN: a three-week-old "up" must not keep a green light on."""
    cyc = monitor.load_cycle() or {}
    cycle_stale = True
    try:
        cycle_stale = (datetime.now(ET).date()
                       - datetime.strptime(cyc.get("last_updated", ""), "%Y-%m-%d").date()
                       ).days > CYCLE_STALE_DAYS
    except ValueError:
        pass
    prox: dict[str, dict] = {}

    for f in thesis["falsifiers"]:
        fid = f["id"]
        if fid == "spot-price-down":
            trend = str(cyc.get("dram_hbm_spot_trend", "")).lower()
            if cycle_stale:
                prox[fid] = {"status": "unknown", "why": "cycle_data 過期,舊趨勢不可信"}
            elif trend in ("down", "falling", "rolling over"):
                prox[fid] = {"status": "triggered", "why": f"現貨價趨勢={trend}"}
            elif trend == "flat":
                prox[fid] = {"status": "warning", "why": "現貨價 up→flat,開始盯"}
            elif trend == "up":
                prox[fid] = {"status": "ok", "why": "現貨價趨勢 up"}
            else:
                prox[fid] = {"status": "unknown", "why": f"cycle_data 讀不到趨勢({trend or '缺'})"}
        elif fid == "eps-momentum-dead":
            revs = [m["eps_rev_y1"] for m in metrics.values() if m.get("eps_rev_y1") is not None]
            if not revs:
                prox[fid] = {"status": "unknown", "why": "抓不到任何 eps_trend"}
            else:
                avg = sum(revs) / len(revs)
                if avg <= 0:
                    prox[fid] = {"status": "triggered", "why": f"平均 90 天上修 {avg:+.0f}% ≤ 0"}
                elif avg < 5:
                    prox[fid] = {"status": "warning", "why": f"平均 90 天上修只剩 {avg:+.0f}%"}
                else:
                    prox[fid] = {"status": "ok", "why": f"平均 90 天上修 {avg:+.0f}%"}
        elif fid == "capex-cut":
            capex = str(cyc.get("hyperscaler_capex", "")).lower()
            if cycle_stale:
                prox[fid] = {"status": "unknown", "why": "cycle_data 過期,舊指引不可信"}
            elif capex in ("cutting", "down"):
                prox[fid] = {"status": "triggered", "why": f"capex 指引={capex}"}
            elif capex == "flat":
                prox[fid] = {"status": "warning", "why": "capex 指引轉 flat"}
            elif capex == "raising":
                prox[fid] = {"status": "ok", "why": "capex 指引 raising"}
            else:
                prox[fid] = {"status": "unknown", "why": "cycle_data 讀不到 capex"}
        else:
            # falsifiers the rules can't see (e.g. semantic ones) stay unknown here;
            # 判官職位③ (financial-call language scan) is the Phase 2+ owner of these
            prox[fid] = {"status": "unknown", "why": "此否證條件需語意監控(judge ③),規則層看不到"}
    return prox


# ───────────────────────── per-ticker evidence ─────────────────────────

def build_evidence(m: dict, insider: dict | None) -> tuple[list[str], list[str]]:
    for_, against = [], []
    rev, mom = m.get("eps_rev_y1"), m.get("eps_mom_y1")
    if rev is not None:
        (for_ if rev > 5 else against).append(f"+1y EPS 90天上修 {rev:+.0f}%")
    if mom is not None and rev is not None and rev > 5 and mom <= 1:
        against.append(f"近 7 天上修動能只剩 {mom:+.0f}% — Street 可能快追平")
    if (m.get("down30") or 0) > (m.get("up30") or 0):
        against.append(f"30 天內下修 {m['down30']} 位 > 上修 {m['up30']} 位")
    if m.get("rec_mean") is not None and m["rec_mean"] < 1.8:
        against.append(f"共識已 {m.get('rec_key')}(mean {m['rec_mean']:.2f}) — 擁擠,不再孤獨")
    elif m.get("rec_mean") is not None:
        for_.append(f"共識 {m.get('rec_key')} 未極端 — 還有人沒上車")
    if m.get("pct_from_high") is not None:
        if m["pct_from_high"] > -8:
            against.append(f"貼著 52 週高({m['pct_from_high']:+.0f}%)")
        elif m["pct_from_high"] < -15:
            for_.append(f"距 52 週高 {m['pct_from_high']:+.0f}% — 有回檔空間可作 add 評估")
    if m.get("rsi") and m["rsi"] > 70:
        against.append(f"RSI {m['rsi']:.0f} 過熱")
    if insider:
        if insider.get("cluster_buy"):
            for_.append("內部人集體自主買入(罕見硬訊號)")
        elif insider.get("n_buys", 0) == 0 and insider.get("sell_usd", 0) > 0:
            against.append(f"內部人 90 天 0 買/賣 ${insider['sell_usd']/1e6:.0f}M"
                           " — thesis 說被低估,內部人卻沒人買")
    return for_, against


def confirmed_state(thesis_id: str, ticker: str, today_state: str) -> tuple[str, bool]:
    """(state, confirmed). Confirmed = states agree across CONFIRM_DAYS distinct
    dates (incl. today). Suspect (dirty-data) days never count toward
    confirmation — an outage must not fabricate a confirmed LATE. Window is
    generous (40 records) so multiple same-day reruns can't starve it."""
    hist = _last_records(thesis_id, 40)
    states, seen_dates = [today_state], set()
    for rec in reversed(hist):
        d = rec["ts"][:10]
        if d == datetime.now(ET).strftime("%Y-%m-%d") or d in seen_dates:
            continue
        seen_dates.add(d)
        trec = rec.get("tickers", {}).get(ticker, {})
        if trec.get("suspect"):
            continue  # dirty day: date consumed but state not trusted
        if trec.get("state"):
            states.append(trec["state"])
        if len(states) >= CONFIRM_DAYS:
            break
    ok = len(states) >= CONFIRM_DAYS and len(set(states)) == 1
    return today_state, ok


# ───────────────────────── suggestions ─────────────────────────

def suggest(thesis: dict, tk: str, state: str, confirmed: bool, suspect: bool,
            m: dict, prox: dict, health: dict) -> str | None:
    """Rule evaluation → human-readable suggestion, or None (=hold, do nothing).
    Falsifier exits fire immediately (no damping); everything else needs
    confirmed state + clean data. Phase 1: a human executes via decide.py."""
    if any(p["status"] == "triggered" for p in prox.values()):
        why = "; ".join(f"{k}:{v['why']}" for k, v in prox.items() if v["status"] == "triggered")
        return f"🔴 EXIT {tk} — falsifier 觸發({why})。即時執行,不等確認期。"
    if suspect:
        return None  # dirty data day: no transitions suggested
    if not confirmed:
        return None  # damping: wait for 3-day agreement
    dist = m.get("pct_from_high")
    warning = any(p["status"] == "warning" for p in prox.values())
    if state == "LATE":
        return f"🔴 EXIT {tk} — state==LATE(3日確認)。"
    if state == "MID" and dist is not None and dist > -5:
        return f"🟠 TRIM {tk} — MID+距高 {dist:+.0f}%<5%:共識追上+貼高,edge 吃掉一半。"
    if state == "EARLY" and dist is not None and dist < -10:
        if health["entries_blocked"]:
            return f"⛔ {tk} 符合 entry 條件但【禁止新進場】:{'; '.join(health['warnings'])}"
        return f"🟢 ENTRY/ADD 評估 {tk} — EARLY+距高 {dist:+.0f}%。先跑 decide.py 記 ex-ante 理由。"
    if state in ("EARLY", "MID") and dist is not None and dist < -15 and not warning:
        return f"🟢 ADD 評估 {tk} — 回檔 {dist:+.0f}%,無 falsifier warning。"
    return None


# ───────────────────────── main ─────────────────────────

def main() -> int:
    want_insider = "--insider" in sys.argv[1:]
    now = datetime.now(ET)
    EVIDENCE_DIR.mkdir(exist_ok=True)
    theses = load_theses()
    if not theses:
        print("沒有 active thesis — theses.yaml 是空的或全部被拒載/過期")
        return 1
    health = check_health()

    print(f"\n══════ Evidence Engine — {now.strftime('%Y-%m-%d %H:%M ET')} ══════")
    for w in health["warnings"]:
        print(f"⚠ {w}")

    for th in theses:
        prev = _last_records(th["id"], 1)
        prev_tk = prev[0].get("tickers", {}) if prev else {}
        metrics: dict[str, dict] = {}
        record_tk: dict[str, dict] = {}
        suggestions: list[str] = []

        print(f"\n▌thesis: {th['id']} — {th['statement']}")
        for tk in list(th["tickers"]) + list(th.get("watch_only", [])):
            try:
                m = monitor.fetch(tk)
            except Exception as exc:
                print(f"  [{tk}] 抓取失敗: {exc}")
                continue
            metrics[tk] = m

            # dirty-data checks: MISSING data and JUMPING data are both suspect.
            # 缺資料≠LATE — verdict() 抓不到 eps 會判 LATE,連 3 天限流就會騙 executor
            # 全部出場(V1 的 check_regime 教訓同族),所以缺失日一律 suspect、不轉換。
            suspect = False
            if m.get("eps_rev_y1") is None:
                suspect = True
                print(f"  ⚠ [{tk}] eps_trend 缺失 — 資料不足,今日不觸發狀態轉換(缺資料≠LATE)")
            prev_rev = prev_tk.get(tk, {}).get("eps_rev_y1")
            if (m.get("eps_rev_y1") is not None and prev_rev is not None
                    and abs(m["eps_rev_y1"] - prev_rev) > EPS_JUMP_SUSPECT_PP):
                suspect = True
                print(f"  ⚠ [{tk}] eps_rev 單日跳 {prev_rev:+.0f}%→{m['eps_rev_y1']:+.0f}%"
                      " — 資料可疑,今日不觸發狀態轉換")

            state, _read, _notes = monitor.verdict(m)
            state, confirmed = confirmed_state(th["id"], tk, state)

            insider = None
            if want_insider and not tk.endswith(".KS"):
                try:
                    import insider_scan
                    full = insider_scan.scan(tk)
                    insider = insider_scan.compact(full) if full else None
                except Exception:
                    pass

            ev_for, ev_against = build_evidence(m, insider)
            record_tk[tk] = {
                "state": state, "confirmed": confirmed, "suspect": suspect,
                "price": m.get("price"), "pct_from_high": m.get("pct_from_high"),
                "eps_rev_y1": m.get("eps_rev_y1"), "rec_mean": m.get("rec_mean"),
                "insider": insider,
                "evidence_for": ev_for, "evidence_against": ev_against,
            }
            tag = "✓確認" if confirmed else f"未滿{CONFIRM_DAYS}日"
            print(f"  {tk:10s} {state:5s}({tag})  支持:{len(ev_for)}  反方:{len(ev_against)}")
            for e in ev_against:
                print(f"      ⚔ {e}")

        # falsifiers are THESIS-level (任一成立→全出) — suggestions must use the
        # same aggregate the executor acts on, not per-ticker readings
        prox = falsifier_proximity(th, {k: v for k, v in metrics.items() if k in th["tickers"]})
        for tk in th["tickers"]:  # watch_only 不產生動作建議
            d = record_tk.get(tk)
            if not d:
                continue
            s = suggest(th, tk, d["state"], d["confirmed"], d["suspect"],
                        metrics[tk], prox, health)
            if s:
                suggestions.append(s)
        print("  ── falsifier proximity ──")
        for fid, p in prox.items():
            icon = {"ok": "🟢", "warning": "🟡", "triggered": "🔴", "unknown": "⚪"}[p["status"]]
            print(f"  {icon} {fid}: {p['why']}")

        if suggestions:
            print("  ── 建議動作(人執行,先跑 decide.py 記 ex-ante 理由) ──")
            for s in suggestions:
                print(f"  {s}")
        else:
            print("  ── 今日無動作建議(沒有轉換的日子,正確行為是什麼都不做) ──")

        rec = {"ts": now.isoformat(), "thesis_id": th["id"], "health": health,
               "tickers": record_tk, "falsifier_proximity": prox,
               "suggestions": suggestions}
        with (EVIDENCE_DIR / f"{th['id']}.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
