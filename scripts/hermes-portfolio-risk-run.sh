#!/bin/sh
set -eu

SCRIPT_PATH=$0
if command -v realpath >/dev/null 2>&1; then
  SCRIPT_PATH=$(realpath "$SCRIPT_PATH")
fi
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$SCRIPT_PATH")" && pwd)
DEFAULT_APP_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
ENV_FILE="${STOCK_AGENT_ENV_FILE:-$DEFAULT_APP_DIR/.env}"

if [ -f "$ENV_FILE" ]; then
  set -a
  . "$ENV_FILE"
  set +a
fi

APP_DIR="${STOCK_AGENT_APP_DIR:-$DEFAULT_APP_DIR}"
if [ ! -f "$APP_DIR/src/stock_recommender/cli.py" ]; then
  echo "Set STOCK_AGENT_APP_DIR to the stock-agent checkout path" >&2
  exit 2
fi

cd "$APP_DIR"

PYTHON_BIN="${STOCK_AGENT_PYTHON:-python3}"
if [ ! -x "$PYTHON_BIN" ]; then
  PYTHON_BIN=python3
fi

exec env \
  STOCK_AGENT_MODE=risk \
  STOCK_AGENT_PORTFOLIO_PATH="${STOCK_AGENT_PORTFOLIO_PATH:-$APP_DIR/data/strategy_portfolios.json}" \
  STOCK_AGENT_PUBLIC_URL="${STOCK_AGENT_PUBLIC_URL:-http://127.0.0.1:8765}" \
  STOCK_AGENT_OUTPUT="${STOCK_AGENT_OUTPUT:-/tmp/stock-agent-risk-action.md}" \
  STOCK_AGENT_SCHEDULE_GUARD="${STOCK_AGENT_SCHEDULE_GUARD:-1}" \
  STOCK_AGENT_PUBLISH_HOURS="${STOCK_AGENT_PUBLISH_HOURS:-9,10,11,13,14,15}" \
  STOCK_AGENT_DELIVERY_RUN=1 \
  PYTHONPATH=src \
  "$PYTHON_BIN" -m stock_recommender.cli
