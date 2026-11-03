# API container

Build from the repository root so the image can compile the Rust scorer and
install the API's sibling multimodal and radar packages:

```sh
docker build -f docker/Dockerfile.api -t x-reach-api .
```

Run the API on port 8000 with persistent storage for downloaded OpenCLIP
weights and a data directory for optional radar captures or reach calibration:

```sh
mkdir -p data
docker run --rm --name x-reach-api \
  -p 8000:8000 \
  --mount type=volume,source=xreach-model-cache,target=/models \
  --mount type=bind,source="$(pwd)/data",target=/data \
  -e XREACH_RADAR_CAPTURE_FILE=/data/radar-capture.json \
  x-reach-api
```

The first automatic analysis downloads the configured OpenCLIP weights under
`/models`; keeping the named volume avoids downloading them again when the
container is replaced. The container runs as the unprivileged `xreach` user,
so a bind-mounted data directory must be writable by UID 10001 if another
process updates radar capture files there. Calibration is optional; to enable
it, place the JSON file in the mounted data directory and set
`XREACH_REACH_CALIBRATION_FILE=/data/reach-calibration.json`.

Docker checks `GET /healthz` and marks the container healthy when the API
responds. The endpoint checks service availability without forcing the Rust
scorer or OpenCLIP model to load during startup.
