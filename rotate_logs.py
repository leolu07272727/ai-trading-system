"""Log rotation (§13).

Runs before market open each trading day. Trims trades.csv to a rolling
window and gzips oversized log files so disk usage stays bounded. Every
operation is best-effort: a missing file is normal (nothing has been written
yet) and must never crash the pre-open routine.
"""
from __future__ import annotations

import gzip
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent
TRADES_CSV = PROJECT_ROOT / "trades.csv"
DECISION_LOG = PROJECT_ROOT / "logs" / "decision_log.jsonl"
CYCLE_ERRORS = PROJECT_ROOT / "logs" / "cycle_errors.log"

TRADES_KEEP_DAYS = 90
DECISION_LOG_MAX_BYTES = 50 * 1024 * 1024
CYCLE_ERRORS_MAX_BYTES = 10 * 1024 * 1024


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d")


def trim_trades_csv(path: Path = TRADES_CSV, keep_days: int = TRADES_KEEP_DAYS):
    """Keep only the last `keep_days` of rows in trades.csv."""
    if not path.exists():
        return None
    try:
        df = pd.read_csv(path)
    except Exception as exc:  # noqa: BLE001
        return f"trades.csv: could not parse ({exc})"

    if "timestamp" not in df.columns or df.empty:
        return None

    ts = pd.to_datetime(df["timestamp"], errors="coerce", utc=True)
    cutoff = pd.Timestamp(datetime.now(timezone.utc) - timedelta(days=keep_days))

    keep_mask = ts >= cutoff
    removed = int((~keep_mask).sum())
    if removed == 0:
        return None

    df.loc[keep_mask].to_csv(path, index=False)
    return f"trades.csv: dropped {removed} rows older than {keep_days}d"


def _gzip_and_reset(path: Path, archive: Path) -> None:
    with path.open("rb") as src, gzip.open(archive, "wb") as dst:
        shutil.copyfileobj(src, dst)
    path.unlink()


def rotate_if_large(path: Path, max_bytes: int, suffix: str):
    """Gzip `path` to a dated archive and start fresh if it exceeds max_bytes."""
    if not path.exists():
        return None
    size = path.stat().st_size
    if size <= max_bytes:
        return None

    archive = path.with_name(f"{path.stem}.{_stamp()}{suffix}")
    try:
        _gzip_and_reset(path, archive)
    except Exception as exc:  # noqa: BLE001
        return f"{path.name}: rotation failed ({exc})"
    return f"{path.name}: rotated to {archive.name} ({size / 1024 / 1024:.1f}MB)"


def main() -> int:
    actions = [
        trim_trades_csv(),
        rotate_if_large(
            DECISION_LOG, DECISION_LOG_MAX_BYTES, ".jsonl.gz"
        ),
        rotate_if_large(
            CYCLE_ERRORS, CYCLE_ERRORS_MAX_BYTES, ".log.gz"
        ),
    ]
    done = [a for a in actions if a]

    print("Log rotation summary:")
    if done:
        for line in done:
            print(f"  - {line}")
    else:
        print("  - nothing to rotate")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
