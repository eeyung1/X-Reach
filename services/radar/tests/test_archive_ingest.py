import json
import math
from dataclasses import asdict

from xreach_radar.archive_ingest import ArchiveIngestor, ingest_archive
from xreach_radar.cli import main


def tweets_js(records):
    return "window.YTD.tweets.part0 = " + json.dumps(records) + ";"


def test_tweets_js_extracts_observed_impressions_formats_and_baseline():
    content = tweets_js(
        [
            {
                "tweet": {
                    "id_str": "100",
                    "full_text": "An image post",
                    "created_at": "Wed Nov 04 12:30:00 +0000 2026",
                    "views": {"count": "100"},
                    "extended_entities": {
                        "media": [{"type": "photo", "media_url": "https://img/1"}]
                    },
                }
            },
            {
                "tweet": {
                    "id_str": "200",
                    "full_text": "A video post",
                    "impression_count": 400,
                    "extended_entities": {"media": [{"type": "video"}]},
                }
            },
            {"tweet": {"id_str": "300", "full_text": "No observed views"}},
            {"tweet": {"id_str": "200", "impressions": 999}},
            "invalid-wrapper",
        ]
    )

    report = ArchiveIngestor().parse_tweets_js(content)

    assert report.source_format == "tweets_js"
    assert [post.tweet_id for post in report.posts] == ["100", "200"]
    assert [post.content_format for post in report.posts] == ["image", "video"]
    assert report.posts[0].created_at.isoformat() == "2026-11-04T12:30:00+00:00"
    assert report.skipped_records == 3
    assert report.author_baseline.observation_count == 2
    assert math.isclose(report.author_baseline.mu, math.log(200))
    assert math.isclose(report.author_baseline.sigma, math.log(2))
    assert report.author_baseline.mean_impressions == 250
    assert [asdict(item) for item in report.format_breakdown] == [
        {
            "content_format": "image",
            "post_count": 1,
            "total_impressions": 100,
            "mean_impressions": 100.0,
            "impression_share": 0.2,
        },
        {
            "content_format": "video",
            "post_count": 1,
            "total_impressions": 400,
            "mean_impressions": 400.0,
            "impression_share": 0.8,
        },
    ]


def test_csv_supports_official_headers_permalink_ids_and_zero_impressions():
    content = """Tweet id,Tweet permalink,Tweet text,time,impressions,Media Type
10,https://x.com/me/status/10,Plain text,2026-11-01T10:00:00Z,"1,000",text
,https://x.com/me/status/20,Watch this,2026-11-02T10:00:00+00:00,0,video
30,https://x.com/me/status/30,Missing observations,2026-11-03T10:00:00Z,,text
"""

    report = ArchiveIngestor().parse_csv(content)

    assert [post.tweet_id for post in report.posts] == ["10", "20"]
    assert [post.impressions for post in report.posts] == [1000, 0]
    assert [post.content_format for post in report.posts] == ["text", "video"]
    assert report.skipped_records == 1
    assert report.author_baseline.impression_floor == 1
    assert math.isclose(report.author_baseline.mu, math.log(1000) / 2)
    assert math.isclose(report.author_baseline.sigma, math.log(1000) / 2)


def test_archive_directory_and_cli_emit_serializable_report(tmp_path, capsys):
    data = tmp_path / "data"
    data.mkdir()
    archive = data / "tweets.js"
    archive.write_text(
        tweets_js(
            [
                {
                    "tweet": {
                        "id_str": "42",
                        "full_text": "Read https://example.test",
                        "public_metrics": {"impression_count": 25},
                    }
                }
            ]
        ),
        encoding="utf-8",
    )

    report = ingest_archive(tmp_path)
    result = main(["ingest-archive", str(tmp_path)])
    output = json.loads(capsys.readouterr().out)

    assert report.posts[0].content_format == "link"
    assert result == 0
    assert output["posts"][0]["tweet_id"] == "42"
    assert output["author_baseline"]["distribution"] == "log_normal"


def test_mixed_media_and_poll_formats_are_distinct():
    content = tweets_js(
        [
            {
                "tweet": {
                    "id_str": "1",
                    "impressions": 10,
                    "extended_entities": {
                        "media": [{"type": "photo"}, {"type": "video"}]
                    },
                }
            },
            {
                "tweet": {
                    "id_str": "2",
                    "impressions": 20,
                    "card": {"name": "poll4choice_text_only"},
                }
            },
        ]
    )

    report = ArchiveIngestor().parse_tweets_js(content)

    assert [post.content_format for post in report.posts] == ["mixed", "poll"]


def test_invalid_impressions_and_empty_observation_sets_are_rejected():
    try:
        ArchiveIngestor().parse_csv("tweet_id,impressions\n1,-5\n")
    except ValueError as error:
        assert "non-negative integer" in str(error)
    else:
        raise AssertionError("negative impressions must be rejected")

    try:
        ArchiveIngestor().parse_tweets_js(tweets_js([{"tweet": {"id_str": "1"}}]))
    except ValueError as error:
        assert "no posts with observed impressions" in str(error)
    else:
        raise AssertionError("archives without observed impressions must be rejected")


def test_parser_enforces_record_and_file_size_limits(tmp_path):
    parser = ArchiveIngestor(max_bytes=8, max_records=1)
    oversized = tmp_path / "tweets.js"
    oversized.write_text(tweets_js([]), encoding="utf-8")

    try:
        parser.ingest(oversized)
    except ValueError as error:
        assert "exceeds 8 bytes" in str(error)
    else:
        raise AssertionError("oversized files must be rejected")

    parser = ArchiveIngestor(max_bytes=10_000, max_records=1)
    try:
        parser.parse_tweets_js(
            tweets_js(
                [
                    {"tweet": {"id_str": "1", "impressions": 1}},
                    {"tweet": {"id_str": "2", "impressions": 2}},
                ]
            )
        )
    except ValueError as error:
        assert "more than 1 records" in str(error)
    else:
        raise AssertionError("record limits must be enforced")
