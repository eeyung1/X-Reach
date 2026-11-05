import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime

import pytest

from xreach_radar.archive_ingest import ArchiveIngestor
from xreach_radar.archive_persistence import (
    ArchivePersistenceResult,
    database_path,
    persist_archive_report,
)
from xreach_radar.cli import main


class SQLiteDatabase:
    def __init__(self, path):
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute(
            "CREATE TABLE creator_baselines ("
            "creator_id TEXT PRIMARY KEY, payload_json TEXT NOT NULL, "
            "updated_at TEXT NOT NULL)"
        )

    @contextmanager
    def transaction(self):
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            yield self.connection
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise


class RepositoryStub:
    def __init__(self, database):
        self.database = database


def archive_report(first_impressions=100):
    return ArchiveIngestor().parse_csv(
        "tweet_id,tweet_text,time,impressions,content_format\n"
        f"one,First post,2026-11-01T12:00:00Z,{first_impressions},text\n"
        "two,Second post,,250,image\n"
    )


def test_persistence_upserts_baseline_and_each_historical_post(tmp_path):
    database = SQLiteDatabase(tmp_path / "xreach.sqlite3")
    repository = RepositoryStub(database)
    recorded_at = datetime(2026, 11, 5, 19, 30, tzinfo=UTC)

    result = persist_archive_report(
        archive_report(),
        creator_id=" creator-one ",
        repository=repository,
        now=recorded_at,
    )

    assert result.creator_id == "creator-one"
    assert result.baseline_saved is True
    assert result.posts_saved == 2
    baseline_row = database.connection.execute(
        "SELECT payload_json, updated_at FROM creator_baselines "
        "WHERE creator_id = 'creator-one'"
    ).fetchone()
    baseline = json.loads(baseline_row["payload_json"])
    assert baseline["creator_id"] == "creator-one"
    assert baseline["observation_count"] == 2
    assert baseline["format_breakdown"][0]["content_format"] == "image"
    assert baseline_row["updated_at"] == recorded_at.isoformat()
    rows = database.connection.execute(
        "SELECT tweet_id, impressions, content_format, created_at "
        "FROM archive_post_impressions ORDER BY tweet_id"
    ).fetchall()
    assert [tuple(row) for row in rows] == [
        ("one", 100, "text", "2026-11-01T12:00:00+00:00"),
        ("two", 250, "image", None),
    ]

    persist_archive_report(
        archive_report(first_impressions=400),
        creator_id="creator-one",
        repository=repository,
        now=recorded_at,
    )
    rows = database.connection.execute(
        "SELECT tweet_id, impressions FROM archive_post_impressions ORDER BY tweet_id"
    ).fetchall()
    assert [tuple(row) for row in rows] == [("one", 400), ("two", 250)]


def test_persistence_rolls_back_baseline_when_post_write_fails(tmp_path):
    database = SQLiteDatabase(tmp_path / "xreach.sqlite3")
    database.connection.execute(
        "CREATE TABLE archive_post_impressions ("
        "creator_id TEXT NOT NULL, tweet_id TEXT NOT NULL, "
        "impressions INTEGER NOT NULL CHECK (impressions < 0), "
        "content_format TEXT NOT NULL, text TEXT NOT NULL, created_at TEXT, "
        "updated_at TEXT NOT NULL, PRIMARY KEY (creator_id, tweet_id))"
    )

    with pytest.raises(sqlite3.IntegrityError):
        persist_archive_report(
            archive_report(),
            creator_id="creator-one",
            repository=RepositoryStub(database),
        )

    assert (
        database.connection.execute(
            "SELECT COUNT(*) FROM creator_baselines"
        ).fetchone()[0]
        == 0
    )


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("sqlite:////data/xreach.db", "/data/xreach.db"),
        ("sqlite:///data/xreach.db", "data/xreach.db"),
        ("sqlite:///:memory:", ":memory:"),
        ("local.db", "local.db"),
    ],
)
def test_database_url_resolution(url, expected):
    assert str(database_path(url)) == expected


def test_database_url_rejects_remote_schemes():
    with pytest.raises(ValueError, match="only sqlite"):
        database_path("postgresql://db.example/xreach")


def test_cli_requires_database_and_creator_options_together(tmp_path, capsys):
    archive = tmp_path / "tweets.csv"
    archive.write_text("tweet_id,impressions\none,1\n", encoding="utf-8")

    assert main(["ingest-archive", str(archive), "--creator-id", "creator"]) == 2
    assert "database-url" in capsys.readouterr().err
    assert (
        main(
            [
                "ingest-archive",
                str(archive),
                "--database-url",
                "sqlite:///xreach.db",
            ]
        )
        == 2
    )
    assert "creator-id" in capsys.readouterr().err


def test_cli_exports_archive_to_configured_database(tmp_path, capsys, monkeypatch):
    archive = tmp_path / "tweets.csv"
    archive.write_text("tweet_id,impressions\none,1\n", encoding="utf-8")
    received = {}

    def persist(report, *, creator_id, database_url):
        received.update(report=report, creator_id=creator_id, database_url=database_url)
        return ArchivePersistenceResult(
            creator_id=creator_id, baseline_saved=True, posts_saved=len(report.posts)
        )

    monkeypatch.setattr("xreach_radar.cli.persist_archive_to_database", persist)

    assert (
        main(
            [
                "ingest-archive",
                str(archive),
                "--creator-id",
                "creator-one",
                "--database-url",
                "sqlite:////data/xreach.db",
            ]
        )
        == 0
    )
    output = json.loads(capsys.readouterr().out)
    assert received["creator_id"] == "creator-one"
    assert received["database_url"] == "sqlite:////data/xreach.db"
    assert received["report"].posts[0].tweet_id == "one"
    assert output["persistence"] == {
        "baseline_saved": True,
        "creator_id": "creator-one",
        "posts_saved": 1,
    }
