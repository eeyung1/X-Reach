# X-Reach API

The FastAPI service extracts local multimodal draft features, validates the
resulting action propensities, and delegates scoring and optional reach
calibration to the Rust `xreach-core` crate through the JSON-lines scorer in
`rust-bridge/`. Callers can submit text and media directly without constructing
prediction JSON. Percentile output is `null` until historical score calibration
data is configured.

## Setup

```bash
python -m pip install -e "../multimodal[inference]" -e "../radar" -e ".[test]"
cargo build --release --manifest-path rust-bridge/Cargo.toml
uvicorn xreach_api.main:app --app-dir src --reload
```

The Rust bridge uses the workspace `crates/xreach-core` via a relative Cargo
path dependency. Set `XREACH_SCORER_BIN` to use a prebuilt scorer executable.
If unset, the API lazily builds the bridge when the first score is requested.

## Live radar stream

`GET /api/v1/radar/stream` is a server-sent event stream of fresh viral root
posts found by the radar scout. Configure `XREACH_RADAR_CAPTURE_FILE` to point
to a JSON HAR or REA capture snapshot that an external capture process refreshes;
the API background producer scans changed snapshots every five seconds by
default. Set `XREACH_RADAR_POLL_SECONDS` to change that interval. With no capture
file configured, the endpoint stays open and sends heartbeat events while
waiting for seeds.

Events include a monotonically increasing `id`, `event: seed`, and JSON seed
data. Pass `Last-Event-ID` to replay newer entries from the in-memory ring, or
filter by cluster with `?simcluster=Technology` (repeat the parameter for more
labels). If a reconnect asks for history older than the ring, the stream sends
an `event: reset` notice before the available events. The buffer holds the most
recent 500 seeds per API process; run a single API worker when clients need one
continuous event history.

## Analyze a draft

`POST /api/v1/analyze` accepts JSON text or `multipart/form-data` with a `text`
field and up to four `media` files. It invokes the local multimodal package and
returns Semantic IDs, keyframe timing, inferred action propensities and the Rust
score. Existing callers may continue supplying `predictions` in JSON or as a
JSON-encoded multipart field to bypass automatic extraction.

```json
{
  "text": "A short draft",
  "text": "A short draft"
}
```

The response includes the weighted and final Phoenix scores, visibility drops,
the six-level `semantic_id`, compact multimodal metadata (never raw embeddings),
and an explicit `reach_calibration_status` of `unavailable` with a null
`reach_potential_percentile` by default. OpenCLIP loads lazily on the first automatic
request; missing inference dependencies produce a 503 without preventing the
API from starting or manual-prediction requests from working.

## Radar daemon ingestion

The standalone `xreach-radar daemon` process publishes seed batches to
`POST /api/v1/radar/events`; the API stores them in the same bounded ring used by
`GET /api/v1/radar/stream`. Set the same `XREACH_RADAR_INGEST_TOKEN` in both
containers to require a bearer token. If the token is omitted, ingestion is
unauthenticated and should only be exposed on a private container network.

## Reach calibration

Set `XREACH_REACH_CALIBRATION_FILE` to a JSON calibration file to enable
percentiles. The empirical method uses historical final Phoenix scores as a
reference distribution; equal scores receive their average rank and values
outside the sample map to 0 or 100. For example:

```json
{"method":"empirical","reference_scores":[0.001,0.04,0.12,0.8]}
```

The parametric method fits a log-normal distribution to positive historical
Phoenix scores. Supply the mean and population standard deviation of their
natural logarithms:

```json
{"method":"log_normal","log_mean":-1.2,"log_std_dev":0.8}
```

Both methods return a percentile of the configured historical score
distribution. Populate the reference data from a representative set of
observed posts before using the result as a reach estimate; the service does
not ship with a fabricated calibration dataset. `/api/v1/placement` includes
the percentile for the recommended surface and for each candidate score.

## Compare placement

`POST /api/v1/placement` accepts `profile_predictions` and
`direct_root_reply_predictions`. Both alternatives are scored independently by
the Rust core. The higher surviving score is recommended; ties favor a profile
post. Reach percentile remains uncalibrated.
