#!/usr/bin/env bash
#
# cleanup_cron.sh — Remove all cron jobs installed by setup_cron.sh
#
# Usage:   ./cleanup_cron.sh
#          chmod +x cleanup_cron.sh   # if not already executable
#
# Idempotent: safe to run multiple times. If no bot jobs (or no crontab at all)
# exist, it exits cleanly without error.
#
# -----------------------------------------------------------------------------
# DST CAVEAT (informational — matches setup_cron.sh)
# -----------------------------------------------------------------------------
# VPS cron uses UTC. During EDT (summer) ET = UTC-4, during EST (winter) ET = UTC-5.
# The 1-hour shift means cron windows should be set wide enough to cover both.
# Python scripts handle precise ET timing internally.
# -----------------------------------------------------------------------------

set -euo pipefail

# Same marker used by setup_cron.sh to tag every job line.
MARKER="# ibkr-bot-cron"

# Grab the current crontab; if there is none, there is nothing to clean up.
CURRENT="$(crontab -l 2>/dev/null || true)"

if [[ -z "${CURRENT}" ]]; then
    echo "No crontab found for current user — nothing to clean up."
    exit 0
fi

# Filter out every line carrying our marker.
FILTERED="$(printf '%s\n' "${CURRENT}" | grep -v "${MARKER}" || true)"

if [[ "${FILTERED}" == "${CURRENT}" ]]; then
    echo "No IBKR bot cron jobs found (marker: ${MARKER}) — nothing removed."
    exit 0
fi

if [[ -z "${FILTERED}" ]]; then
    # Removing our jobs leaves the crontab empty — clear it entirely.
    crontab -r 2>/dev/null || true
    echo "Removed all IBKR bot cron jobs; crontab is now empty."
else
    printf '%s\n' "${FILTERED}" | crontab -
    echo "Removed all IBKR bot cron jobs (marker: ${MARKER})."
fi

echo
echo "Remaining crontab:"
crontab -l 2>/dev/null || echo "(empty)"
