"""Persist first-party archive observations through the shared DB repository."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import AbstractContextManager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import unquote, urlsplit

from .archive_ingest import ArchiveIngestReport


class _Connection(Protocol):
    def execute(self, statement: str, parameters: tuple[Any, ...] = ()) -> Any: ...

    def executemany(
        self, statement: str, parameters: Iterator[tuple[Any, ...]]
    ) -> Any: ...


class _Database(Protocol):
    def transaction(self) -> AbstractContextManager[_Connection]: ...


class _Repository(Protocol):
    database: _Database


@dataclass(frozen=True)
class ArchivePersistenceResult:
    """Summary of one idempotent archive persistence operation."""

    creator_id: str
    baseline_saved: bool
    posts_saved: int


_CREATE_ARCHIVE_POSTS = """CREATE TABLE IF NOT EXISTS archive_post_impressions (
    creator_id TEXT NOT NULL,
    tweet_id TEXT NOT NULL,
    impressions INTEGER NOT NULL CHECK (impressions >= 0),
    content_format TEXT NOT NULL,
    text TEXT NOT NULL,
    created_at TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (creator_id, tweet_id)
)"""


def persist_archive_report(
    report: ArchiveIngestReport,
    *,
    creator_id: str,
    repository: _Repository,
    now: datetime | None = None,
) -> ArchivePersistenceResult:
    """Atomically upsert a computed baseline and its observed post impressions."""

    creator_id = creator_id.strip()
    if not creator_id:
        raise ValueError("creator_id must not be blank")
    if len(creator_id) > 64:
        raise ValueError("creator_id must not exceed 64 characters")
    updated_at = now or datetime.now(UTC)
    if updated_at.tzinfo is None:
        raise ValueError("persistence timestamp must include a timezone")
    updated_at_text = updated_at.astimezone(UTC).isoformat()

    baseline = asdict(report.author_baseline)
    baseline["creator_id"] = creator_id
    baseline["format_breakdown"] = [asdict(item) for item in report.format_breakdown]
    baseline_json = json.dumps(
        baseline, separators=(",", ":"), sort_keys=True, allow_nan=False
    )

    with repository.database.transaction() as connection:
        connection.execute(_CREATE_ARCHIVE_POSTS)
        connection.execute(
            "INSERT INTO creator_baselines(creator_id, payload_json, updated_at) "
            "VALUES (?, ?, ?) ON CONFLICT(creator_id) DO UPDATE SET "
            "payload_json=excluded.payload_json, updated_at=excluded.updated_at",
            (creator_id, baseline_json, updated_at_text),
        )
        connection.executemany(
            "INSERT INTO archive_post_impressions(creator_id, tweet_id, "
            "impressions, content_format, text, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(creator_id, tweet_id) DO UPDATE SET "
            "impressions=excluded.impressions, "
            "content_format=excluded.content_format, text=excluded.text, "
            "created_at=excluded.created_at, updated_at=excluded.updated_at",
            (
                (
                    creator_id,
                    post.tweet_id,
                    post.impressions,
                    post.content_format,
                    post.text,
                    post.created_at.isoformat() if post.created_at else None,
                    updated_at_text,
                )
                for post in report.posts
            ),
        )

    return ArchivePersistenceResult(
        creator_id=creator_id,
        baseline_saved=True,
        posts_saved=len(report.posts),
    )


def persist_archive_to_database(
    report: ArchiveIngestReport,
    *,
    creator_id: str,
    database_url: str,
) -> ArchivePersistenceResult:
    """Open the configured API database repository and persist an archive report."""

    try:
        from xreach_api.db import Database, Repository
    except ImportError as error:
        raise RuntimeError(
            "archive database export requires the xreach-api package"
        ) from error

    with Database(database_path(database_url)) as database:
        return persist_archive_report(
            report,
            creator_id=creator_id,
            repository=Repository(database),
        )


def database_path(database_url: str) -> str | Path:
    """Resolve a SQLite ``DATABASE_URL`` without accepting remote DB schemes."""

    value = database_url.strip()
    if not value:
        raise ValueError("database URL must not be blank")
    if "://" not in value:
        return Path(value).expanduser()

    parsed = urlsplit(value)
    if parsed.scheme != "sqlite":
        raise ValueError("archive export currently supports only sqlite DATABASE_URLs")
    if parsed.netloc or parsed.query or parsed.fragment:
        raise ValueError("SQLite DATABASE_URL must contain only a database path")
    path = unquote(parsed.path)
    if path == "/:memory:":
        return ":memory:"
    if value.startswith("sqlite:////"):
        return Path("/" + path.lstrip("/")).expanduser()
    relative = path.removeprefix("/")
    if not relative:
        raise ValueError("SQLite DATABASE_URL must include a database path")
    return Path(relative).expanduser()
