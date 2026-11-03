#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ROOT_DIR=$(dirname "$SCRIPT_DIR")
cd "$ROOT_DIR"

if ! command -v docker >/dev/null 2>&1; then
  echo "Docker is required to run the X-Reach Compose stack." >&2
  exit 127
fi
docker compose version >/dev/null

ACTION=${1:-up}
if [ "$#" -gt 0 ]; then
  shift
fi

case "$ACTION" in
  up)
    mkdir -p "${XREACH_DATA_DIR:-$ROOT_DIR/data}"
    docker compose up --build --detach --wait "$@"
    ;;
  down)
    docker compose down "$@"
    ;;
  logs)
    docker compose logs --follow "$@"
    ;;
  *)
    echo "Usage: scripts/start.sh [up|down|logs] [docker compose options...]" >&2
    exit 2
    ;;
esac
