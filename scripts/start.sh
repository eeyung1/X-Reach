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
    chmod 777 "${XREACH_DATA_DIR:-$ROOT_DIR/data}" 2>/dev/null || true
    docker compose up --build --detach "$@"
    port="${XREACH_HTTP_PORT:-8080}"
    for i in $(seq 1 30); do
      if curl -sf "http://127.0.0.1:$port/healthz" >/dev/null 2>&1; then
        echo "X-Reach Compose stack is healthy and ready on port $port."
        exit 0
      fi
      sleep 2
    done
    echo "Timed out waiting for X-Reach gateway healthz on port $port." >&2
    exit 1
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
