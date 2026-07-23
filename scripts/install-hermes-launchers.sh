#!/bin/sh
set -eu

SCRIPT_PATH=$0
if command -v realpath >/dev/null 2>&1; then
  SCRIPT_PATH=$(realpath "$SCRIPT_PATH")
fi
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$SCRIPT_PATH")" && pwd)
RUNTIME_DIR="${HERMES_SCRIPTS_DIR:-$HOME/.hermes/scripts}"
APP_DIR="${STOCK_AGENT_APP_DIR:-$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)}"
RUNTIME_LAUNCHER="$SCRIPT_DIR/hermes-runtime-launcher.sh"

if [ ! -f "$RUNTIME_LAUNCHER" ]; then
  echo "Runtime launcher is missing: $RUNTIME_LAUNCHER" >&2
  exit 2
fi
if [ ! -f "$APP_DIR/src/stock_agent.py" ]; then
  echo "Stock-agent checkout is invalid: $APP_DIR" >&2
  exit 2
fi

mkdir -p "$RUNTIME_DIR"

pointer="$RUNTIME_DIR/stock-agent-app-dir"
pointer_temporary="$RUNTIME_DIR/.stock-agent-app-dir.tmp.$$"
printf '%s\n' "$APP_DIR" > "$pointer_temporary"
chmod 0600 "$pointer_temporary"
mv -f "$pointer_temporary" "$pointer"

for name in \
  hermes-agent-data-run.sh \
  hermes-ai-run.sh \
  hermes-portfolio-risk-run.sh \
  hermes-tracking-run.sh
do
  target="$RUNTIME_DIR/$name"
  temporary="$RUNTIME_DIR/.$name.tmp.$$"
  install -m 0755 "$RUNTIME_LAUNCHER" "$temporary"
  mv -f "$temporary" "$target"
done

printf 'Installed Hermes runtime launchers in %s for %s\n' "$RUNTIME_DIR" "$APP_DIR"
