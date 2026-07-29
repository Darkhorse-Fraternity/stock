#!/bin/sh
set -eu

SCRIPT_PATH=$0
if command -v realpath >/dev/null 2>&1; then
  SCRIPT_PATH=$(realpath "$SCRIPT_PATH")
fi
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$SCRIPT_PATH")" && pwd)
APP_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
ENV_FILE="${STOCK_AGENT_ENV_FILE:-$APP_DIR/.env}"

if [ -f "$ENV_FILE" ]; then
  set -a
  . "$ENV_FILE"
  set +a
fi

PYTHON_BIN="${STOCK_AGENT_PYTHON:-python3}"
if [ ! -x "$PYTHON_BIN" ]; then
  PYTHON_BIN=python3
fi

cd "$APP_DIR"
exec env PYTHONPATH=src "$PYTHON_BIN" -m stock_recommender.warmup
