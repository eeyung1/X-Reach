#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ROOT_DIR=$(dirname "$SCRIPT_DIR")
cd "$ROOT_DIR"

cleanup() {
  docker compose down --remove-orphans
}
trap cleanup EXIT
"$SCRIPT_DIR/start.sh" up
python3 -m pytest tests/e2e/ -q "$@"
