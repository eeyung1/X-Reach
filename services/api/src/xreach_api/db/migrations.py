"""Versioned, idempotent SQLite schema migrations."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

MIGRATIONS: dict[int, tuple[str, ...]] = {
    1: (
        """CREATE TABLE drafts (
            draft_id TEXT PRIMARY KEY,
            creator_id TEXT NOT NULL,
            baseline_24h_impressions INTEGER CHECK (
                baseline_24h_impressions IS NULL OR baseline_24h_impressions >= 0
            ),
            payload_json TEXT NOT NULL,
            recorded_at TEXT NOT NULL
        )""",
        "CREATE INDEX drafts_creator_id_idx ON drafts(creator_id)",
        """CREATE TABLE attributions (
            draft_id TEXT PRIMARY KEY REFERENCES drafts(draft_id) ON DELETE CASCADE,
            published_tweet_id TEXT NOT NULL UNIQUE,
            payload_json TEXT NOT NULL,
            recorded_at TEXT NOT NULL
        )""",
        """CREATE TABLE outcome_snapshots (
            draft_id TEXT NOT NULL REFERENCES attributions(draft_id) ON DELETE CASCADE,
            horizon TEXT NOT NULL CHECK (horizon IN ('5m', '30m', '2h', '24h', '7d')),
            published_tweet_id TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            PRIMARY KEY (draft_id, horizon)
        )""",
        """CREATE TABLE creator_baselines (
            creator_id TEXT PRIMARY KEY,
            payload_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )""",
        """CREATE TABLE alert_rules (
            rule_id TEXT PRIMARY KEY,
            payload_json TEXT NOT NULL
        )""",
        """CREATE TABLE alert_webhooks (
            webhook_id TEXT PRIMARY KEY,
            payload_json TEXT NOT NULL
        )""",
    ),
}


def apply_migrations(connection: sqlite3.Connection) -> None:
    """Apply each not-yet-recorded migration atomically."""
    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations "
            "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        applied = {
            row[0]
            for row in connection.execute("SELECT version FROM schema_migrations")
        }
        for version, statements in sorted(MIGRATIONS.items()):
            if version in applied:
                continue
            for statement in statements:
                connection.execute(statement)
            connection.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (version, datetime.now(UTC).isoformat()),
            )
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
