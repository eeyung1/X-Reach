from datetime import datetime, timedelta, timezone

import pytest

from xreach_radar.seed_scout import ScoutConfig, SeedScout

NOW = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)


def tweet(
    tweet_id: str,
    *,
    age_minutes: int = 10,
    views: int = 10_000,
    replies: int = 4,
    conversation_id: str | None = None,
    reply_parent: str | None = None,
    cluster: str = "Technology",
):
    created = NOW - timedelta(minutes=age_minutes)
    return {
        "__typename": "Tweet",
        "rest_id": tweet_id,
        "core": {
            "user_results": {
                "result": {
                    "rest_id": "author-1",
                    "legacy": {"screen_name": "builder"},
                }
            }
        },
        "legacy": {
            "conversation_id_str": conversation_id or tweet_id,
            "in_reply_to_status_id_str": reply_parent,
            "created_at": created.strftime("%a %b %d %H:%M:%S %z %Y"),
            "full_text": f"post {tweet_id}",
            "reply_count": replies,
            "favorite_count": 20,
            "retweet_count": 3,
            "quote_count": 2,
        },
        "views": {"count": str(views)},
        "simclusters": [{"id": "42", "name": cluster}],
    }


def timeline(*tweets):
    entries = [
        {"content": {"itemContent": {"tweet_results": {"result": item}}}}
        for item in tweets
    ]
    return {
        "operationName": "HomeTimeline",
        "data": {
            "home": {
                "home_timeline_urt": {
                    "instructions": [{"type": "TimelineAddEntries", "entries": entries}]
                }
            }
        },
    }


def test_ranks_fresh_root_seeds_and_preserves_source_fields():
    faster = tweet("faster", age_minutes=5, views=10_000)
    faster["semantic_id"] = [12, 44, 91, 8, 203, 17]
    capture = timeline(
        tweet("slower", age_minutes=10, views=8_000),
        faster,
    )
    seeds = SeedScout().scan(capture, now=NOW)

    assert [seed.tweet_id for seed in seeds] == ["faster", "slower"]
    assert seeds[0].views_per_minute == 2_000
    assert seeds[0].author_id == "author-1"
    assert seeds[0].screen_name == "builder"
    assert seeds[0].simclusters == ("42", "Technology")
    assert seeds[0].semantic_id == (12, 44, 91, 8, 203, 17)
    assert seeds[0].source_operation == "HomeTimeline"


def test_filters_non_roots_old_slow_and_saturated_posts():
    unknown_root = tweet("unknown-root")
    unknown_root["legacy"].pop("in_reply_to_status_id_str")
    unknown_replies = tweet("unknown-replies")
    unknown_replies["legacy"].pop("reply_count")
    capture = timeline(
        tweet("reply", conversation_id="root", reply_parent="root"),
        tweet("old", age_minutes=30, views=100_000),
        tweet("slow", age_minutes=20, views=2_000),
        tweet("busy", replies=50, views=100_000),
        unknown_root,
        unknown_replies,
        tweet("good", age_minutes=3, views=3_000, replies=49),
    )
    seeds = SeedScout().scan(capture, now=NOW)
    assert [seed.tweet_id for seed in seeds] == ["good"]


def test_cluster_filter_is_case_insensitive_and_requires_explicit_evidence():
    capture = timeline(
        tweet("tech", cluster="Technology"),
        tweet("sports", cluster="Sports"),
    )
    config = ScoutConfig(target_simclusters=frozenset({"technology"}))
    assert [seed.tweet_id for seed in SeedScout(config).scan(capture, now=NOW)] == [
        "tech"
    ]


def test_deduplicates_same_tweet_using_largest_observed_count():
    high = tweet("same", views=12_000)
    low = tweet("same", views=8_000)
    capture = timeline(high, low)
    seeds = SeedScout().scan(capture, now=NOW)
    assert len(seeds) == 1
    assert seeds[0].impressions == 12_000


def test_timezone_is_required_for_reproducible_now():
    with pytest.raises(ValueError, match="timezone"):
        SeedScout().scan(timeline(tweet("post")), now=datetime(2026, 11, 3, 12))
