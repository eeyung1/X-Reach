# X-Reach Radar

The radar service has two deliberately separate responsibilities:

1. `ReaClient` runs REA's passive `inspect-web-page` CLI command without a
   shell and validates its JSON result.
2. `SeedScout` reads captured `HomeTimeline`/`TweetDetail` GraphQL response
   bodies and ranks fresh root posts by observed impressions per minute.
3. `SimClusterScout` aligns an extracted six-level Semantic ID with active
   SimCluster prefixes and returns only matching high-velocity root posts.

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
```

The scout only treats a post as a root when its captured conversation ID equals
its post ID and no reply parent is present. Timestamps, view counts, and cluster
labels must also be present in the capture; unknown values are not invented.
Cluster catalogs use `{"clusters": [...]}` with `cluster_id`, `name`, a
one-to-six-code `semantic_prefix`, optional `aliases`, and optional timezone-aware
`active_until`. Expired records are excluded. Direct tweet SIDs and catalog
prefixes use the same transparent prefix-depth score; no vector similarity is
invented when the source capture lacks an SID.
