# X-Reach Radar

The radar service has two deliberately separate responsibilities:

1. `ReaClient` runs REA's passive `inspect-web-page` CLI command without a
   shell and validates its JSON result.
2. `SeedScout` reads captured `HomeTimeline`/`TweetDetail` GraphQL response
   bodies and ranks fresh root posts by observed impressions per minute.
3. `SimClusterScout` aligns an extracted six-level Semantic ID with active
   SimCluster prefixes and returns only matching high-velocity root posts.
4. `ArchiveIngestor` parses first-party X `tweets.js` or CSV exports into
   observed impression records, content-format summaries, and an author baseline.
5. `RadarDaemon` replays offline HAR/GraphQL/REA CDP evidence or repeatedly runs
   a live REA browser scenario and publishes changed seeds to the API SSE broker.

`inspect-web-page` does not retain response values by default. Seed scouting
therefore accepts raw GraphQL JSON, HAR/REA historical capture JSON, or REA
`capture-browser-scenario` evidence created with response-body capture enabled.
Missing bodies produce an empty result rather than inferred posts.

```bash
pytest services/radar/tests/

# Inspect a live browser target (the browser must expose a loopback CDP port)
python -m xreach_radar inspect \
  http://127.0.0.1:9222 TARGET_ID --observation-ms 1000

# Scout a saved response/capture. Repeat --cluster to require matching labels.
python -m xreach_radar scout capture.json \
  --max-age-minutes 30 --min-views-per-minute 500 --cluster Technology

# Align a draft SID directly and through an optional active-cluster catalog.
python -m xreach_radar scout capture.json \
  --semantic-id 12,44,91,8,203,17 \
  --simcluster-catalog active-simclusters.json --min-prefix-levels 2

# Build a first-party historical baseline without an X API credential.
python -m xreach_radar ingest-archive /path/to/archive/data/tweets.js
python -m xreach_radar ingest-archive /path/to/tweet-analytics.csv

# One-shot offline replay into a locally running API.
python -m xreach_radar daemon --capture session.har.json \
  --api-url http://127.0.0.1:8000 --once

# Container/live mode: repeat a response-body-enabled REA browser scenario.
xreach-radar daemon --rea-scenario /config/x-timeline-scenario.json
```

The scout only treats a post as a root when its captured conversation ID equals
its post ID and no reply parent is present. Timestamps, view counts, and cluster
labels must also be present in the capture; unknown values are not invented.
Cluster catalogs use `{"clusters": [...]}` with `cluster_id`, `name`, a
one-to-six-code `semantic_prefix`, optional `aliases`, and optional timezone-aware
`active_until`. Expired records are excluded. Direct tweet SIDs and catalog
prefixes use the same transparent prefix-depth score; no vector similarity is
invented when the source capture lacks an SID.

Daemon defaults can be supplied with `XREACH_RADAR_CAPTURE_FILE`,
`XREACH_REA_SCENARIO_FILE`, `XREACH_API_URL`, `XREACH_RADAR_INGEST_TOKEN`, and
`XREACH_RADAR_POLL_SECONDS`. A live REA scenario must explicitly select GraphQL
response bodies; passive `inspect-web-page` shapes alone cannot establish tweet
values. SIGTERM/SIGINT stop the polling loop cleanly. Capture files are bounded
to 25 MB and only replayed again after their size or modification time changes.

## First-party archive ingestion

`ArchiveIngestor` accepts the JavaScript assignment wrapper used by official X
`data/tweets.js` files and header-based CSV exports. A directory input resolves
`data/tweets.js`, `tweets.js`, or a single CSV automatically. Inputs are bounded
to 100 MB and one million records. Posts without both an identifier and an
observed impression/view count are reported as skipped; unknown impressions are
never inferred. Duplicate tweet IDs are counted once.

The report contains each usable post, a breakdown for text, image, video, GIF,
link, poll, mixed, or other formats, and an author baseline. Baseline `mu` and
`sigma` are the population mean and standard deviation of natural-log observed
impressions using a documented floor of one for zero-impression posts. This is
a descriptive first-party baseline, not a forecast or a platform-wide sample.

## Alert rules and webhooks

`AlertRuleEngine` evaluates radar seed mappings against velocity, freshness,
and optional SimCluster thresholds. Matching seeds become `AlertEvent` values.
`WebhookDispatcher` asynchronously delivers events to generic JSON, Discord,
or Slack incoming webhooks. Each request includes an
`X-XReach-Signature: sha256=...` HMAC-SHA256 header over the exact JSON body.
Delivery retries transient failures with bounded exponential backoff; client
errors are returned as failed deliveries without retrying.

```python
from xreach_radar.alerts import AlertRule, AlertRuleEngine, WebhookDispatcher, WebhookTarget

engine = AlertRuleEngine([AlertRule("fast-tech", 500, 30, frozenset({"Technology"}))])
dispatcher = WebhookDispatcher([
    WebhookTarget("https://hooks.example/alerts", "shared-secret", format="slack")
])
events = engine.evaluate(seed)
for event in events:
    await dispatcher.dispatch(event)
```
