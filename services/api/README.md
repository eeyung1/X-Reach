# X-Reach API

The FastAPI service validates model prediction heads and delegates scoring to
the Rust `xreach-core` crate through the JSON-lines scorer in `rust-bridge/`.
The API does not include a feature extractor or a calibrated reach-percentile
model. Callers must supply predicted action heads; percentile output is `null`
until calibration data is configured.

## Setup

```bash
python -m pip install -e ".[test]"
cargo build --release --manifest-path rust-bridge/Cargo.toml
uvicorn xreach_api.main:app --app-dir src --reload
```

The Rust bridge uses the workspace `crates/xreach-core` via a relative Cargo
path dependency. Set `XREACH_SCORER_BIN` to use a prebuilt scorer executable.
If unset, the API lazily builds the bridge when the first score is requested.

## Analyze a draft

`POST /api/v1/analyze` accepts JSON with `text` and `predictions`, or
`multipart/form-data` with a `text` field, optional media fields and a
JSON-encoded `predictions` field. Media bytes are not interpreted by this
service; the model layer must provide predictions separately.

```json
{
  "text": "A short draft",
  "predictions": {
    "favorite": 0.2,
    "reply": 0.08,
    "share_via_copy_link": 0.03
  },
  "author_id": 42,
  "in_network": false,
  "oon_multiplier": 0.6
}
```

The response includes the weighted and final Phoenix scores, visibility drops,
and an explicit `reach_calibration_status` of `unavailable` with a null
`reach_potential_percentile`.

## Compare placement

`POST /api/v1/placement` accepts `profile_predictions` and
`direct_root_reply_predictions`. Both alternatives are scored independently by
the Rust core. The higher surviving score is recommended; ties favor a profile
post. Reach percentile remains uncalibrated.
