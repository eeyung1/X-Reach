"""Smoke tests for the Compose gateway and API radar event path."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen
from uuid import uuid4

BASE_URL = os.environ.get("XREACH_E2E_BASE_URL", "http://127.0.0.1:8080").rstrip("/")
INGEST_TOKEN = os.environ.get("XREACH_RADAR_INGEST_TOKEN", "local-compose-token")
ALERT_ADMIN_TOKEN = os.environ.get(
    "XREACH_ALERT_ADMIN_TOKEN", "local-alert-admin-token"
)
WEBHOOK_PORT = os.environ.get("XREACH_E2E_WEBHOOK_PORT", "18081")
WEBHOOK_DISPATCH_URL = os.environ.get(
    "XREACH_E2E_WEBHOOK_DISPATCH_URL", "http://webhook-listener:9000/events"
)
WEBHOOK_QUERY_URL = os.environ.get(
    "XREACH_E2E_WEBHOOK_QUERY_URL", f"http://127.0.0.1:{WEBHOOK_PORT}/events"
)
WEBHOOK_SECRET = "compose-e2e-secret"
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _request(
    path: str,
    *,
    method: str = "GET",
    payload: object = None,
    extra_headers: dict[str, str] | None = None,
) -> tuple[int, bytes, str]:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request_headers = {"Accept": "application/json, text/html"}
    request_headers["Authorization"] = f"Bearer {INGEST_TOKEN}"
    if body is not None:
        request_headers["Content-Type"] = "application/json"
    request_headers.update(extra_headers or {})
    request = Request(
        f"{BASE_URL}{path}", data=body, headers=request_headers, method=method
    )
    with urlopen(request, timeout=15) as response:
        return (
            response.status,
            response.read(),
            response.headers.get("Content-Type", ""),
        )


def test_gateway_routes_web_and_api_health() -> None:
    web_status, page, content_type = _request("/")
    assert web_status == 200
    assert "text/html" in content_type
    assert b"X-Reach" in page

    api_status, health, _ = _request("/healthz")
    assert api_status == 200
    assert json.loads(health) == {"status": "ok", "scoring_engine": "rust-core"}


def test_radar_ingest_is_visible_in_the_sse_stream() -> None:
    tweet_id = f"compose-e2e-{uuid4().hex}"
    seed = {
        "tweet_id": tweet_id,
        "conversation_id": tweet_id,
        "screen_name": "compose_e2e",
        "text": "Compose end-to-end radar probe",
        "impressions": 1200,
        "views_per_minute": 600,
        "replies": 3,
        "simclusters": ["compose-e2e"],
    }
    status, response_body, _ = _request(
        "/api/v1/radar/events",
        method="POST",
        payload={"events": [seed]},
    )
    assert status == 200
    event_id = json.loads(response_body)["first_event_id"]

    request = Request(
        f"{BASE_URL}/api/v1/radar/stream?after={event_id - 1}",
        headers={"Accept": "text/event-stream"},
    )
    request.add_header("Authorization", f"Bearer {INGEST_TOKEN}")
    lines: list[str] = []
    with urlopen(request, timeout=15) as response:
        assert response.status == 200
        for raw_line in response:
            line = raw_line.decode("utf-8")
            lines.append(line)
            if line == "\n" and "event: seed\n" in "".join(lines):
                break
            if len(lines) > 12:
                break
    stream_event = "".join(lines)
    assert "event: seed\n" in stream_event
    assert tweet_id in stream_event


def test_radar_ingest_dispatches_signed_alert_to_compose_webhook() -> None:
    rule_id = f"compose-e2e-{uuid4().hex}"
    tweet_id = f"compose-alert-{uuid4().hex}"
    admin_headers = {"X-Alert-Admin-Token": ALERT_ADMIN_TOKEN}
    status, _, _ = _request(
        "/api/v1/alerts",
        method="PUT",
        payload={
            "rules": [
                {
                    "rule_id": rule_id,
                    "min_views_per_minute": 600,
                    "max_age_minutes": 15,
                    "simclusters": ["compose-alert-e2e"],
                }
            ],
            "webhooks": [
                {
                    "webhook_id": "compose-e2e-listener",
                    "url": WEBHOOK_DISPATCH_URL,
                    "secret": WEBHOOK_SECRET,
                    "format": "generic",
                }
            ],
        },
        extra_headers=admin_headers,
    )
    assert status == 200

    seed = {
        "tweet_id": tweet_id,
        "conversation_id": tweet_id,
        "screen_name": "compose_alert_e2e",
        "text": "Compose alert delivery probe",
        "impressions": 1800,
        "views_per_minute": 750,
        "age_minutes": 4,
        "replies": 5,
        "simclusters": ["compose-alert-e2e"],
    }
    status, _, _ = _request(
        "/api/v1/radar/events",
        method="POST",
        payload={"events": [seed]},
    )
    assert status == 200

    deadline = time.monotonic() + 20
    deliveries: list[dict[str, object]] = []
    while time.monotonic() < deadline:
        request = Request(WEBHOOK_QUERY_URL)
        try:
            with urlopen(request, timeout=3) as response:
                deliveries = json.loads(response.read())
        except URLError:
            time.sleep(0.25)
            continue
        if any(tweet_id in str(delivery.get("body", "")) for delivery in deliveries):
            break
        time.sleep(0.25)

    matching = [
        delivery for delivery in deliveries if tweet_id in str(delivery.get("body", ""))
    ]
    assert len(matching) == 1
    delivery = matching[0]
    raw_body = str(delivery["body"]).encode("utf-8")
    signature = hmac.new(WEBHOOK_SECRET.encode(), raw_body, hashlib.sha256).hexdigest()
    assert delivery["headers"]["x-xreach-event"] == "radar.alert"
    assert delivery["headers"]["x-xreach-signature"] == f"sha256={signature}"
    event = json.loads(raw_body)
    assert event["event_id"] == f"{rule_id}:{tweet_id}"
    assert event["seed"]["tweet_id"] == tweet_id


def test_api_telemetry_persists_after_compose_container_restart() -> None:
    before_status, before_body, _ = _request("/api/v1/telemetry/summary")
    assert before_status == 200
    before = json.loads(before_body)

    draft_id = str(uuid4())
    published_tweet_id = f"compose-persist-{uuid4().hex}"
    timestamp = datetime.now(UTC).isoformat()
    status, _, _ = _request(
        "/api/v1/telemetry/drafts",
        method="POST",
        payload={
            "creator_id": "compose-persistence-e2e",
            "baseline_24h_impressions": 1000,
            "draft": {"draft_id": draft_id, "text": "Persistence probe"},
        },
    )
    assert status == 201
    status, _, _ = _request(
        "/api/v1/telemetry/publications",
        method="POST",
        payload={
            "draft_id": draft_id,
            "published_tweet_id": published_tweet_id,
            "surface": "PROFILE_POST",
            "published_at": timestamp,
        },
    )
    assert status == 201
    for horizon, impressions in (("24h", 1500), ("7d", 2400)):
        status, _, _ = _request(
            "/api/v1/telemetry/outcomes",
            method="POST",
            payload={
                "draft_id": draft_id,
                "published_tweet_id": published_tweet_id,
                "horizon": horizon,
                "observed_at": timestamp,
                "impressions": impressions,
                "source": "manual",
            },
        )
        assert status == 201

    written_status, written_body, _ = _request("/api/v1/telemetry/summary")
    assert written_status == 200
    written = json.loads(written_body)
    assert written["published"] == before["published"] + 1
    assert written["outcomes_24h"] == before["outcomes_24h"] + 1
    assert written["outcomes_7d"] == before["outcomes_7d"] + 1

    subprocess.run(
        ["docker", "compose", "restart", "api"],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=90,
    )
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        try:
            health_status, _, _ = _request("/healthz")
            if health_status == 200:
                break
        except URLError:
            pass
        time.sleep(1)
    else:
        raise AssertionError("API did not become healthy after its Compose restart")

    restored_status, restored_body, _ = _request("/api/v1/telemetry/summary")
    assert restored_status == 200
    restored = json.loads(restored_body)
    for key in (
        "drafts",
        "published",
        "outcomes_24h",
        "outcomes_7d",
        "mean_creator_lift_delta_percent",
        "creator_lift_sample_size",
    ):
        assert restored[key] == written[key]
