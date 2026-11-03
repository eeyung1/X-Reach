import json
from dataclasses import asdict
from datetime import datetime, timedelta, timezone

import pytest

from xreach_radar.cli import main
from xreach_radar.seed_scout import ScoutConfig
from xreach_radar.simclusters import (
    ActiveSimCluster,
    SimClusterIndex,
    SimClusterScout,
    semantic_id,
    shared_prefix_levels,
)

NOW = datetime(2026, 11, 3, 14, 0, tzinfo=timezone.utc)
DRAFT_SID = (12, 44, 91, 8, 203, 17)


def root(tweet_id, *, sid=None, clusters=(), age=5, views=5_000):
    created = NOW - timedelta(minutes=age)
    tweet = {
        "rest_id": tweet_id,
        "legacy": {
            "conversation_id_str": tweet_id,
            "in_reply_to_status_id_str": None,
            "created_at": created.strftime("%a %b %d %H:%M:%S %z %Y"),
            "full_text": f"root {tweet_id}",
            "reply_count": 2,
        },
        "views": {"count": str(views)},
        "simclusters": list(clusters),
    }
    if sid is not None:
        tweet["semantic_id"] = list(sid)
    return tweet


def timeline(*tweets):
    return {
        "operationName": "HomeTimeline",
        "data": {
            "home": {
                "instructions": [
                    {
                        "entries": [
                            {"tweet_results": {"result": tweet}} for tweet in tweets
                        ]
                    }
                ]
            }
        },
    }


def test_semantic_id_validation_and_prefix_depth():
    assert semantic_id(DRAFT_SID) == DRAFT_SID
    assert shared_prefix_levels(DRAFT_SID, (12, 44, 90, 8, 203, 17)) == 2
    with pytest.raises(ValueError, match="six"):
        semantic_id((1, 2))
    with pytest.raises(ValueError, match="0 through 255"):
        semantic_id((1, 2, 3, 4, 5, 256))
    with pytest.raises(TypeError, match="0 through 255"):
        semantic_id((1, 2, 3, 4, 5, 1.5))


def test_index_maps_only_active_matching_prefixes():
    index = SimClusterIndex(
        [
            ActiveSimCluster("tech", "Technology", (12, 44, 91)),
            ActiveSimCluster(
                "expired",
                "Old topic",
                (12, 44, 91, 8),
                active_until=NOW - timedelta(seconds=1),
            ),
            ActiveSimCluster("sports", "Sports", (9, 2)),
        ]
    )
    matches = index.match(DRAFT_SID, now=NOW, min_prefix_levels=2)
    assert [(item.cluster_id, item.prefix_levels) for item in matches] == [("tech", 3)]


def test_catalog_alias_maps_labelled_root_without_tweet_sid():
    index = SimClusterIndex.from_payload(
        {
            "clusters": [
                {
                    "cluster_id": "tech",
                    "name": "Technology",
                    "aliases": ["Software Engineering"],
                    "semantic_prefix": [12, 44, 91],
                    "active_until": "2026-11-03T15:00:00Z",
                }
            ]
        }
    )
    capture = timeline(
        root("match", clusters=[{"name": "Software Engineering"}]),
        root("other", clusters=[{"name": "Sports"}], views=10_000),
    )
    matches = SimClusterScout(index).query(capture, DRAFT_SID, now=NOW)
    assert [item.seed.tweet_id for item in matches] == ["match"]
    assert matches[0].prefix_levels == 3
    assert matches[0].matched_simclusters[0].cluster_id == "tech"


def test_direct_sid_alignment_filters_and_ranks_live_roots():
    capture = timeline(
        root("deep", sid=(12, 44, 91, 8, 9, 9), views=4_000),
        root("fast", sid=(12, 44, 7, 1, 1, 1), views=10_000),
        root("unrelated", sid=(99, 44, 91, 8, 203, 17), views=100_000),
        root("slow", sid=(12, 44, 91, 8, 203, 17), age=20, views=100),
    )
    config = ScoutConfig(min_views_per_minute=500)
    matches = SimClusterScout(scout_config=config).query(capture, DRAFT_SID, now=NOW)
    assert [item.seed.tweet_id for item in matches] == ["deep", "fast"]
    assert matches[0].prefix_levels == 4
    assert matches[1].prefix_levels == 2
    assert asdict(matches[0])["seed"]["semantic_id"] == (
        12,
        44,
        91,
        8,
        9,
        9,
    )


def test_naive_query_time_is_rejected():
    with pytest.raises(ValueError, match="timezone"):
        SimClusterScout().query(
            timeline(root("match", sid=DRAFT_SID)),
            DRAFT_SID,
            now=datetime(2026, 11, 3, 14),
        )


def test_cli_queries_capture_with_comma_separated_sid(tmp_path, capsys):
    capture_path = tmp_path / "capture.json"
    capture_path.write_text(
        json.dumps(timeline(root("match", sid=DRAFT_SID))), encoding="utf-8"
    )

    exit_code = main(
        [
            "scout",
            str(capture_path),
            "--semantic-id",
            "12,44,91,8,203,17",
            "--now",
            NOW.isoformat(),
        ]
    )

    assert exit_code == 0
    output = json.loads(capsys.readouterr().out)
    assert output[0]["seed"]["tweet_id"] == "match"
    assert output[0]["prefix_levels"] == 6
