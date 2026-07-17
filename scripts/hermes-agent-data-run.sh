#!/bin/sh
set -eu

APP_DIR="${STOCK_AGENT_APP_DIR:-/home/aura/internal-tools/apps/stock-agent}"

cd "$APP_DIR"
exec env \
  STOCK_AGENT_MODE=data \
  STOCK_AGENT_SCHEDULE_GUARD="${STOCK_AGENT_SCHEDULE_GUARD:-1}" \
  STOCK_AGENT_PUBLISH_HOURS="${STOCK_AGENT_PUBLISH_HOURS:-9}" \
  PYTHONPATH=src \
  python3 src/stock_agent.py
