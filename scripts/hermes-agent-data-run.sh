#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
DEFAULT_APP_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
ENV_FILE="${STOCK_AGENT_ENV_FILE:-$DEFAULT_APP_DIR/.env}"

if [ -f "$ENV_FILE" ]; then
  set -a
  . "$ENV_FILE"
  set +a
fi

APP_DIR="${STOCK_AGENT_APP_DIR:-$DEFAULT_APP_DIR}"
if [ ! -f "$APP_DIR/src/stock_agent.py" ]; then
  echo "Set STOCK_AGENT_APP_DIR to the stock-agent checkout path" >&2
  exit 2
fi

cd "$APP_DIR"
exec env \
  STOCK_AGENT_MODE=data \
  STOCK_AGENT_SCHEDULE_GUARD="${STOCK_AGENT_SCHEDULE_GUARD:-1}" \
  STOCK_AGENT_PUBLISH_HOURS="${STOCK_AGENT_PUBLISH_HOURS:-9}" \
  PYTHONPATH=src \
  python3 src/stock_agent.py
