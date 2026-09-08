#!/usr/bin/env bash
# Cron-friendly Dhan SELF token refresh (RenewToken while still active).
# Example crontab (every 6 hours):
#   15 */6 * * * /path/to/AI-TRADING/scripts/renew-dhan-token.sh
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
export PYTHONPATH="${ROOT}/src${PYTHONPATH:+:$PYTHONPATH}"
LOG_DIR="${ROOT}/data/logs"
mkdir -p "$LOG_DIR"
LOG="${LOG_DIR}/dhan-renew.log"
{
  echo "---- $(date -u +%Y-%m-%dT%H:%M:%SZ) ----"
  "${ROOT}/.venv/bin/python" -m algo.cli dhan renew --if-needed
} >>"$LOG" 2>&1
