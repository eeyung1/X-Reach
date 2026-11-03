# Compose stack scripts

From the repository root, start the API, web app, radar worker, and local
gateway with:

```sh
scripts/start.sh up
```

The gateway is available at `http://127.0.0.1:8080` by default. It routes the
web app at `/`, the API health check at `/healthz`, and `/api/v1/*` requests
to FastAPI. This keeps the browser's same-origin API and SSE requests working
while the API itself stays off the host-published ports.

Use `scripts/start.sh down` to stop the stack, or
`scripts/start.sh logs [service...]` to follow container logs. The E2E helper
builds and starts the services, runs the gateway and radar SSE smoke tests,
then stops the stack even if a test fails:

```sh
scripts/e2e.sh
```

Install Docker Compose v2 and `pytest` before using these scripts.

The stack reads offline capture files from `./data/radar-capture.json` by
default. Set `XREACH_DATA_DIR` to use another directory. Set
`XREACH_RADAR_INGEST_TOKEN` in the shell before running the E2E helper if you
override the Compose default. The token is used only for the private
daemon-to-API ingestion endpoint; do not expose the API port directly.

For live REA capture, set `XREACH_REA_SCENARIO_FILE` to a scenario under the
shared data directory and provide a radar image with the REA executable
installed. Select it with `XREACH_RADAR_IMAGE` and set `XREACH_REA_COMMAND` if
the executable is not named `rea`. Offline HAR or captured JSON replay works
with the default API image.
