"""Smoke tests for the Compose gateway and API radar event path."""

from __future__ import annotations

import json
import os
from urllib.request import Request, urlopen
from uuid import uuid4


BASE_URL = os.environ.get("XREACH_E2E_BASE_URL", "http://127.0.0.1:8080").rstrip("/")
INGEST_TOKEN = os.environ.get(
    "XREACH_RADAR_INGEST_TOKEN", "local-compose-token"
)


def _request(
    path: str, *, method: str = "GET", payload: object = None
) -> tuple[int, bytes, str]:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {"Accept": "application/json, text/html"}
    headers["Authorization"] = f"Bearer {INGEST_TOKEN}"
    if body is not None:
        headers["Content-Type"] = "application/json"
    request = Request(f"{BASE_URL}{path}", data=body, headers=headers, method=method)
    with urlopen(request, timeout=15) as response:
        return response.status, response.read(), response.headers.get("Content-Type", "")


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
