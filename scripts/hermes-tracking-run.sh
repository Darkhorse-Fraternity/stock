#!/bin/sh
set -eu

APP_DIR="${STOCK_AGENT_APP_DIR:-/home/aura/internal-tools/apps/stock-agent}"

cd "$APP_DIR"

PYTHON_BIN="${STOCK_AGENT_PYTHON:-/home/aura/.hermes/venv/stock-trading/bin/python}"
if [ ! -x "$PYTHON_BIN" ]; then
  PYTHON_BIN=python3
fi

exec env \
  STOCK_AGENT_MODE=track \
  STOCK_AGENT_STATE_PATH="${STOCK_AGENT_STATE_PATH:-/tmp/stock-agent-daily-selection.json}" \
  STOCK_AGENT_OUTPUT="${STOCK_AGENT_OUTPUT:-/tmp/stock-agent-hourly-tracking.md}" \
  STOCK_AGENT_SCHEDULE_GUARD="${STOCK_AGENT_SCHEDULE_GUARD:-1}" \
  STOCK_AGENT_PUBLISH_HOURS="${STOCK_AGENT_PUBLISH_HOURS:-10,11,13,14,15}" \
  PYTHONPATH=src \
  "$PYTHON_BIN" src/stock_agent.py
