import asyncio
import json
from pathlib import Path
from unittest.mock import patch

from xreach_radar.daemon import (
    ApiEventSink,
    CaptureFileSource,
    RadarDaemon,
)


def test_capture_file_source_replays_only_new_snapshots(tmp_path: Path):
    capture = tmp_path / "timeline.har.json"
    capture.write_text(json.dumps({"data": {"version": 1}}), encoding="utf-8")
    source = CaptureFileSource([capture])

    first = asyncio.run(source.read_captures())
    second = asyncio.run(source.read_captures())
    capture.write_text(json.dumps({"data": {"version": 2}}), encoding="utf-8")
    third = asyncio.run(source.read_captures())

    assert first == ({"data": {"version": 1}},)
    assert second == ()
    assert third == ({"data": {"version": 2}},)


def test_daemon_deduplicates_then_publishes_changed_seed():
    class Source:
        async def read_captures(self):
            return ({"capture": True},)

    class Scanner:
        views = 1_000

        def scan(self, capture):
            return [
                {
                    "tweet_id": "root-1",
                    "impressions": self.views,
                    "replies": 2,
                    "views_per_minute": self.views / 5,
                    "simclusters": ["Technology"],
                }
            ]

    class Sink:
        def __init__(self):
            self.batches = []

        async def publish(self, events):
            self.batches.append(tuple(events))
            return len(events)

    scanner = Scanner()
    sink = Sink()
    daemon = RadarDaemon(Source(), sink, scanner)

    first = asyncio.run(daemon.run_once())
    duplicate = asyncio.run(daemon.run_once())
    scanner.views = 2_000
    changed = asyncio.run(daemon.run_once())

    assert (first.candidates, first.published) == (1, 1)
    assert duplicate.published == 0
    assert changed.published == 1
    assert sink.batches[0][0]["tweet_id"] == "root-1"
    assert sink.batches[2][0]["impressions"] == 2_000


def test_api_sink_posts_bearer_authenticated_batch():
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self):
            return b'{"published":1}'

    sink = ApiEventSink("http://api:8000/", token="secret")
    with patch("xreach_radar.daemon.urlopen", return_value=Response()) as send:
        published = asyncio.run(sink.publish([{"tweet_id": "root-1"}]))

    assert published == 1
    request = send.call_args.args[0]
    assert request.full_url == "http://api:8000/api/v1/radar/events"
    assert request.headers["Authorization"] == "Bearer secret"
    assert json.loads(request.data) == {"events": [{"tweet_id": "root-1"}]}


def test_daemon_retries_pending_events_after_sink_failure():
    class Source:
        calls = 0

        async def read_captures(self):
            self.calls += 1
            return ({"capture": True},) if self.calls == 1 else ()

    class Scanner:
        def scan(self, capture):
            return [{"tweet_id": "root-1", "impressions": 1_000}]

    class FlakySink:
        calls = 0

        async def publish(self, events):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("API unavailable")
            return len(events)

    daemon = RadarDaemon(Source(), FlakySink(), Scanner())

    try:
        asyncio.run(daemon.run_once())
    except RuntimeError as error:
        assert str(error) == "API unavailable"
    else:
        raise AssertionError("first ingestion should fail")
    retried = asyncio.run(daemon.run_once())

    assert retried.captures == 0
    assert retried.published == 1
