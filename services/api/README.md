# X-Reach API

The FastAPI service extracts local multimodal draft features, validates the
resulting action propensities, and delegates scoring to the Rust `xreach-core`
crate through the JSON-lines scorer in `rust-bridge/`. Callers can submit text
and media directly without constructing prediction JSON. Percentile output is
still `null` until calibration data is configured.

## Setup

```bash
python -m pip install -e "../multimodal[inference]" -e ".[test]"
cargo build --release --manifest-path rust-bridge/Cargo.toml
uvicorn xreach_api.main:app --app-dir src --reload
```

The Rust bridge uses the workspace `crates/xreach-core` via a relative Cargo
path dependency. Set `XREACH_SCORER_BIN` to use a prebuilt scorer executable.
If unset, the API lazily builds the bridge when the first score is requested.

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
`reach_potential_percentile`. OpenCLIP loads lazily on the first automatic
request; missing inference dependencies produce a 503 without preventing the
API from starting or manual-prediction requests from working.

## Compare placement

`POST /api/v1/placement` accepts `profile_predictions` and
`direct_root_reply_predictions`. Both alternatives are scored independently by
the Rust core. The higher surviving score is recommended; ties favor a profile
post. Reach percentile remains uncalibrated.
