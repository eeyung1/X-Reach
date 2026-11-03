import asyncio
import hashlib
import hmac
import json
from unittest.mock import patch

from xreach_radar.alerts import (
    AlertEvent,
    AlertRule,
    AlertRuleEngine,
    WebhookDispatcher,
    WebhookTarget,
    format_payload,
)


def sample_seed(**changes):
    return {
        "tweet_id": "123",
        "screen_name": "creator",
        "text": "A useful post",
        "views_per_minute": 800,
        "age_minutes": 5,
        "simclusters": ["Technology"],
        **changes,
    }


async def run_inline(function, *args):
    """Keep HTTP transport assertions deterministic in the unit tests."""
    return function(*args)


def test_rule_engine_filters_by_velocity_age_and_cluster():
    engine = AlertRuleEngine(
        [
            AlertRule(
                "tech",
                min_views_per_minute=500,
                max_age_minutes=20,
                simclusters=frozenset({"Technology"}),
            )
        ]
    )

    assert [event.event_id for event in engine.evaluate(sample_seed())] == ["tech:123"]
    assert engine.evaluate(sample_seed(views_per_minute=499)) == []
    assert engine.evaluate(sample_seed(age_minutes=20)) == []
    assert engine.evaluate(sample_seed(simclusters=["Sports"])) == []


def test_rule_engine_rejects_duplicate_ids_and_missing_tweet_id():
    rule = AlertRule("same")
    try:
        AlertRuleEngine([rule, rule])
    except ValueError as error:
        assert "unique" in str(error)
    else:
        raise AssertionError("duplicate rule IDs must be rejected")

    assert (
        AlertRuleEngine([rule]).evaluate(
            {"views_per_minute": 1, "age_minutes": 1}
        )
        == []
    )


def test_payload_formatters_produce_platform_shapes():
    event = AlertEvent("tech:123", "tech", sample_seed(), "2026-11-03T00:00:00+00:00")

    generic = format_payload("generic", event)
    discord = format_payload("discord", event)
    slack = format_payload("slack", event)

    assert generic["seed"]["tweet_id"] == "123"
    assert discord["embeds"][0]["url"] == "https://x.com/creator/status/123"
    assert slack["blocks"][0]["text"]["type"] == "mrkdwn"


def test_dispatcher_signs_exact_body_and_posts_json():
    class Response:
        status = 204

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

    target = WebhookTarget("https://hooks.example/alert", "secret")
    event = AlertEvent("tech:123", "tech", sample_seed(), "2026-11-03T00:00:00+00:00")
    with (
        patch("xreach_radar.alerts.urlopen", return_value=Response()) as send,
        patch("xreach_radar.alerts.asyncio.to_thread", new=run_inline),
    ):
        result = asyncio.run(WebhookDispatcher([target]).dispatch(event))

    assert result == [True]
    request = send.call_args.args[0]
    expected = hmac.new(b"secret", request.data, hashlib.sha256).hexdigest()
    assert request.headers["X-xreach-signature"] == f"sha256={expected}"
    assert json.loads(request.data)["event_id"] == "tech:123"


def test_dispatcher_retries_transient_failure_and_stops_after_success():
    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

    target = WebhookTarget(
        "https://hooks.example/alert", "secret", max_attempts=3, backoff_seconds=0
    )
    event = AlertEvent("tech:123", "tech", sample_seed())
    with (
        patch(
            "xreach_radar.alerts.urlopen",
            side_effect=[OSError("offline"), Response()],
        ) as send,
        patch("xreach_radar.alerts.asyncio.to_thread", new=run_inline),
    ):
        result = asyncio.run(WebhookDispatcher([target]).dispatch(event))

    assert result == [True]
    assert send.call_count == 2


def test_dispatcher_does_not_retry_client_errors():
    from urllib.error import HTTPError

    target = WebhookTarget(
        "https://hooks.example/alert", "secret", max_attempts=3, backoff_seconds=0
    )
    event = AlertEvent("tech:123", "tech", sample_seed())
    error = HTTPError(target.url, 400, "bad request", {}, None)
    with (
        patch("xreach_radar.alerts.urlopen", side_effect=error) as send,
        patch("xreach_radar.alerts.asyncio.to_thread", new=run_inline),
    ):
        result = asyncio.run(WebhookDispatcher([target]).dispatch(event))

    assert result == [False]
    assert send.call_count == 1
