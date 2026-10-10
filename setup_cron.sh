#!/usr/bin/env bash
#
# setup_cron.sh — Install all cron jobs for the IBKR paper-trading bot (§14)
#
# Usage:   ./setup_cron.sh
#          chmod +x setup_cron.sh   # if not already executable
#
# -----------------------------------------------------------------------------
# DST CAVEAT
# -----------------------------------------------------------------------------
# VPS cron uses UTC. During EDT (summer) ET = UTC-4, during EST (winter) ET = UTC-5.
# The 1-hour shift means cron windows should be set wide enough to cover both.
# Python scripts handle precise ET timing internally.
#
# Market hours: 9:30-16:00 ET = 13:30-20:00 UTC (EST) / 14:30-21:00 UTC (EDT).
# Cron windows below are deliberately widened so they cover both EST and EDT;
# cycle.py and friends apply their own ET-based gate and exit instantly when
# the market is closed.
# -----------------------------------------------------------------------------

set -euo pipefail

# --- Paths (auto-detected from this script's location) ----------------------
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="${PROJECT_DIR}/.venv/bin/python"
# ----------------------------------------------------------------------------

LOG_DIR="${PROJECT_DIR}/logs"

# Unique marker so cleanup_cron.sh can find and remove exactly our jobs.
MARKER="# ibkr-bot-cron"

mkdir -p "${LOG_DIR}"

# Helper: build one cron line.
#   $1 = cron schedule (5 fields)
#   $2 = script filename (relative to PROJECT_DIR)
#   $3 = trailing comment (ET equivalent / notes)
job() {
    local schedule="$1"
    local script="$2"
    local note="$3"
    local logfile="${LOG_DIR}/cron_${script%.py}.log"
    # Prepend an ISO date to every log line via `date` + the script output.
    printf '%s cd %s && { echo "[$(date -u +\\%%Y-\\%%m-\\%%dT\\%%H:\\%%M:\\%%SZ)] start %s"; %s %s/%s; } >> %s 2>&1 %s  %s\n' \
        "${schedule}" "${PROJECT_DIR}" "${script}" "${PYTHON}" "${PROJECT_DIR}" "${script}" "${logfile}" "${MARKER}" "${note}"
}

# Read existing crontab (empty if none), strip any of our previous jobs so this
# script is safely re-runnable, then append a fresh set.
EXISTING="$(crontab -l 2>/dev/null | grep -v "${MARKER}" || true)"

NEW_JOBS="$(cat <<EOF
$(job "0 8 * * 1-5"        "rotate_logs.py"        "# 08:00 UTC Mon-Fri (~03:00-04:00 ET, before open)")
$(job "*/30 13-18 * * 1-5" "morning_prefilter.py"  "# every 30m 13:00-18:00 UTC Mon-Fri (pre-market to midday ET)")
$(job "*/5 13-21 * * 1-5"  "cycle.py"              "# every 5m 13:00-21:00 UTC Mon-Fri (self-gated to ET market hours)")
$(job "0 14-20 * * 1-5"    "portfolio_snapshot.py" "# top of hour 14:00-20:00 UTC Mon-Fri (~10:00-16:00 ET)")
$(job "*/3 19-20 * * 1-5"  "force_close.py"        "# every 3m 19:00-20:59 UTC Mon-Fri; covers 15:51-16:10 ET under both EDT (~19:51-20:10 UTC) and EST (~20:51-21:10 UTC); force_close.py's own ET gate decides whether to actually act")
$(job "30 20 * * 1-5"      "compute_perf.py"       "# 20:30 UTC Mon-Fri (after market close)")
$(job "35 21 * * 1-5"      "eod_summary.py"        "# 21:35 UTC Mon-Fri (today funnel + AI verdicts, DST-safe after close)")
$(job "40 21 * * 1-5"      "compute_experiment.py" "# 21:40 UTC Mon-Fri (shadow tracking + A/B + judge scorecard)")
$(job "47 21 * * 1-5"      "daily_llm_check.py"    "# 21:47 UTC Mon-Fri (LLM 解讀, 在 eod_summary/experiment 之後)")
EOF
)"

# Combine and install. Ensure a trailing newline.
{
    if [[ -n "${EXISTING}" ]]; then
        printf '%s\n' "${EXISTING}"
    fi
    printf '%s\n' "${NEW_JOBS}"
} | crontab -

echo "Installed IBKR bot cron jobs (marker: ${MARKER})."
echo "PROJECT_DIR = ${PROJECT_DIR}"
echo "PYTHON      = ${PYTHON}"
echo "Logs        = ${LOG_DIR}/cron_*.log"
echo
echo "Current crontab:"
crontab -l | grep "${MARKER}" || true
