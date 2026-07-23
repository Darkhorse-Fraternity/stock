#!/bin/sh
set -eu

LAUNCHER_NAME=$(basename -- "$0")
case "$LAUNCHER_NAME" in
  hermes-agent-data-run.sh|hermes-ai-run.sh|hermes-portfolio-risk-run.sh|hermes-tracking-run.sh)
    ;;
  *)
    echo "Unsupported Hermes launcher name: $LAUNCHER_NAME" >&2
    exit 2
    ;;
esac

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
APP_DIR_FILE="${STOCK_AGENT_APP_DIR_FILE:-$SCRIPT_DIR/stock-agent-app-dir}"

if [ -n "${STOCK_AGENT_APP_DIR:-}" ]; then
  APP_DIR=$STOCK_AGENT_APP_DIR
elif [ -r "$APP_DIR_FILE" ]; then
  APP_DIR=
  IFS= read -r APP_DIR < "$APP_DIR_FILE" || true
else
  APP_DIR=$PWD
fi

if [ -z "$APP_DIR" ]; then
  echo "Stock-agent checkout pointer is empty: $APP_DIR_FILE" >&2
  exit 2
fi
if command -v realpath >/dev/null 2>&1; then
  APP_DIR=$(realpath "$APP_DIR")
fi

if [ ! -f "$APP_DIR/src/stock_agent.py" ]; then
  echo "Hermes workdir is not a stock-agent checkout: $APP_DIR" >&2
  echo "Re-run install-hermes-launchers.sh or set STOCK_AGENT_APP_DIR" >&2
  exit 2
fi

TARGET="$APP_DIR/scripts/$LAUNCHER_NAME"
if [ ! -x "$TARGET" ]; then
  echo "Stock-agent launcher is missing or not executable: $TARGET" >&2
  exit 2
fi

ENV_FILE="${STOCK_AGENT_ENV_FILE:-$APP_DIR/.env}"
exec env \
  STOCK_AGENT_APP_DIR="$APP_DIR" \
  STOCK_AGENT_ENV_FILE="$ENV_FILE" \
  "$TARGET"
