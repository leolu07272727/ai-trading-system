"""Fire-and-forget Telegram notifier. Never raises."""
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

_TIMEOUT = 5
_STATE_FILE = Path(__file__).resolve().parent.parent / "logs" / ".notify_state.json"
# Telegram hard limit is 4096 chars per message; leave headroom for the title.
_MAX_LEN = 4000


def _truncate(text: str) -> str:
    return text if len(text) <= _MAX_LEN else text[:_MAX_LEN] + "\n…(截斷)"


def notify(title: str, body: str, priority: str = "default") -> None:
    # NOTIFY_SILENT=1 (set per cron job) mutes routine reports but never
    # high-priority alerts (notify_error) — silence must not hide failures.
    if priority != "high" and os.environ.get("NOTIFY_SILENT") == "1":
        print(f"[notify] NOTIFY_SILENT=1 — suppressed: {title}")
        return
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        message = _truncate(f"*{title}*\n{body}")
        resp = requests.post(
            url,
            json={"chat_id": chat_id, "text": message, "parse_mode": "Markdown"},
            timeout=_TIMEOUT,
        )
        if not resp.ok:
            # Markdown parse failure (unescaped _ * [ in symbols/risk-flags →
            # HTTP 400 "can't parse entities") used to DROP the message
            # silently. Resend as plain text — delivery beats bold titles.
            requests.post(
                url,
                json={"chat_id": chat_id, "text": _truncate(f"{title}\n{body}")},
                timeout=_TIMEOUT,
            )
    except Exception as exc:  # noqa: BLE001
        print(f"[notify] failed to send Telegram message: {exc}", file=sys.stderr)


def notify_error(msg: str) -> None:
    notify("ERROR", msg, priority="high")


def notify_changed(key: str, title: str, body: str, per_day_only: bool = False) -> None:
    """Send only when this heartbeat's state changes within the (UTC) day.

    Dedupes repetitive heartbeats (regime block, morning scan) so the same
    message isn't pushed every 5-min cycle. `key` groups related messages;
    `per_day_only=True` sends at most once per key per day regardless of body.
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    sig = today if per_day_only else hashlib.md5(f"{title}|{body}".encode()).hexdigest()
    try:
        state = json.loads(_STATE_FILE.read_text())
    except Exception:
        state = {}
    prev = state.get(key)
    if prev and prev.get("date") == today and prev.get("sig") == sig:
        return  # unchanged today → stay quiet
    state[key] = {"date": today, "sig": sig}
    try:
        _STATE_FILE.parent.mkdir(exist_ok=True)
        _STATE_FILE.write_text(json.dumps(state))
    except Exception:
        pass
    notify(title, body)
