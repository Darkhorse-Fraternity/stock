#!/bin/sh
set -eu

APP_DIR="${STOCK_AGENT_APP_DIR:-/opt/stock-agent}"

cd "$APP_DIR"
exec env STOCK_AGENT_MODE=data PYTHONPATH=src python3 src/stock_agent.py
