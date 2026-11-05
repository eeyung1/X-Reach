"""Transactional CRUD repository for API telemetry and alert configuration."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from pydantic import BaseModel

from ..models import (
    AlertRuleConfig,
    AlertWebhookConfig,
    DraftInputRecord,
    OutcomeSnapshotRecord,
    PublicationContextRecord,
)
from .connection import Database
from .models import CreatorBaselineRecord, DraftRegistrationRecord


def _json(model: BaseModel) -> str:
    return model.model_dump_json()


def _load(model: type[BaseModel], payload: str):
    return model.model_validate_json(payload)


class Repository:
    """Typed data operations over a migrated :class:`Database`."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def create_draft(
        self,
        draft: DraftInputRecord,
        *,
        creator_id: str,
        baseline_24h_impressions: int | None = None,
    ) -> DraftInputRecord:
        DraftRegistrationRecord(
            creator_id=creator_id,
            baseline_24h_impressions=baseline_24h_impressions,
            draft=draft,
        )
        with self.database.transaction() as connection:
            connection.execute(
                "INSERT INTO drafts(draft_id, creator_id, baseline_24h_impressions, "
                "payload_json, recorded_at) VALUES (?, ?, ?, ?, ?)",
                (
                    str(draft.draft_id),
                    creator_id,
                    baseline_24h_impressions,
                    _json(draft),
                    draft.recorded_at.isoformat(),
                ),
            )
        return draft

    def get_draft(self, draft_id: UUID | str) -> DraftInputRecord | None:
        with self.database.read() as connection:
            row = connection.execute(
                "SELECT payload_json FROM drafts WHERE draft_id = ?", (str(draft_id),)
            ).fetchone()
        return _load(DraftInputRecord, row[0]) if row else None

    def get_draft_registration(
        self, draft_id: UUID | str
    ) -> DraftRegistrationRecord | None:
        with self.database.read() as connection:
            row = connection.execute(
                "SELECT creator_id, baseline_24h_impressions, payload_json "
                "FROM drafts WHERE draft_id = ?",
                (str(draft_id),),
            ).fetchone()
        if row is None:
            return None
        return DraftRegistrationRecord(
            creator_id=row[0],
            baseline_24h_impressions=row[1],
            draft=_load(DraftInputRecord, row[2]),
        )

    def list_drafts(self, *, creator_id: str | None = None) -> list[DraftInputRecord]:
        query = "SELECT payload_json FROM drafts"
        params: tuple[str, ...] = ()
        if creator_id is not None:
            query += " WHERE creator_id = ?"
            params = (creator_id,)
        query += " ORDER BY recorded_at, draft_id"
        with self.database.read() as connection:
            rows = connection.execute(query, params).fetchall()
        return [_load(DraftInputRecord, row[0]) for row in rows]

    def list_draft_registrations(
        self, *, creator_id: str | None = None
    ) -> list[DraftRegistrationRecord]:
        query = "SELECT creator_id, baseline_24h_impressions, payload_json FROM drafts"
        params: tuple[str, ...] = ()
        if creator_id is not None:
            query += " WHERE creator_id = ?"
            params = (creator_id,)
        query += " ORDER BY recorded_at, draft_id"
        with self.database.read() as connection:
            rows = connection.execute(query, params).fetchall()
        return [
            DraftRegistrationRecord(
                creator_id=row[0],
                baseline_24h_impressions=row[1],
                draft=_load(DraftInputRecord, row[2]),
            )
            for row in rows
        ]

    def delete_draft(self, draft_id: UUID | str) -> bool:
        with self.database.transaction() as connection:
            cursor = connection.execute(
                "DELETE FROM drafts WHERE draft_id = ?", (str(draft_id),)
            )
        return cursor.rowcount > 0

    def update_draft(
        self,
        draft_id: UUID | str,
        draft: DraftInputRecord,
        *,
        creator_id: str,
        baseline_24h_impressions: int | None = None,
    ) -> bool:
        if str(draft.draft_id) != str(draft_id):
            raise ValueError("draft_id must match the stored draft")
        DraftRegistrationRecord(
            creator_id=creator_id,
            baseline_24h_impressions=baseline_24h_impressions,
            draft=draft,
        )
        with self.database.transaction() as connection:
            cursor = connection.execute(
                "UPDATE drafts SET creator_id = ?, baseline_24h_impressions = ?, "
                "payload_json = ?, recorded_at = ? WHERE draft_id = ?",
                (
                    creator_id,
                    baseline_24h_impressions,
                    _json(draft),
                    draft.recorded_at.isoformat(),
                    str(draft_id),
                ),
            )
        return cursor.rowcount > 0

    def create_attribution(
        self, attribution: PublicationContextRecord
    ) -> PublicationContextRecord:
        with self.database.transaction() as connection:
            connection.execute(
                "INSERT INTO attributions(draft_id, published_tweet_id, payload_json, "
                "recorded_at) VALUES (?, ?, ?, ?)",
                (
                    str(attribution.draft_id),
                    attribution.published_tweet_id,
                    _json(attribution),
                    attribution.recorded_at.isoformat(),
                ),
            )
        return attribution

    def get_attribution(self, draft_id: UUID | str) -> PublicationContextRecord | None:
        with self.database.read() as connection:
            row = connection.execute(
                "SELECT payload_json FROM attributions WHERE draft_id = ?",
                (str(draft_id),),
            ).fetchone()
        return _load(PublicationContextRecord, row[0]) if row else None

    def list_attributions(self) -> list[PublicationContextRecord]:
        with self.database.read() as connection:
            rows = connection.execute(
                "SELECT payload_json FROM attributions ORDER BY recorded_at, draft_id"
            ).fetchall()
        return [_load(PublicationContextRecord, row[0]) for row in rows]

    def delete_attribution(self, draft_id: UUID | str) -> bool:
        with self.database.transaction() as connection:
            cursor = connection.execute(
                "DELETE FROM attributions WHERE draft_id = ?", (str(draft_id),)
            )
        return cursor.rowcount > 0

    def update_attribution(self, attribution: PublicationContextRecord) -> bool:
        with self.database.transaction() as connection:
            current = connection.execute(
                "SELECT published_tweet_id FROM attributions WHERE draft_id = ?",
                (str(attribution.draft_id),),
            ).fetchone()
            if current is None:
                return False
            if current[0] != attribution.published_tweet_id:
                has_outcomes = connection.execute(
                    "SELECT 1 FROM outcome_snapshots WHERE draft_id = ? LIMIT 1",
                    (str(attribution.draft_id),),
                ).fetchone()
                if has_outcomes:
                    raise ValueError(
                        "published_tweet_id cannot change after outcomes are recorded"
                    )
            connection.execute(
                "UPDATE attributions SET published_tweet_id = ?, payload_json = ?, "
                "recorded_at = ? WHERE draft_id = ?",
                (
                    attribution.published_tweet_id,
                    _json(attribution),
                    attribution.recorded_at.isoformat(),
                    str(attribution.draft_id),
                ),
            )
        return True

    def save_outcome(self, outcome: OutcomeSnapshotRecord) -> OutcomeSnapshotRecord:
        with self.database.transaction() as connection:
            attribution = connection.execute(
                "SELECT published_tweet_id FROM attributions WHERE draft_id = ?",
                (str(outcome.draft_id),),
            ).fetchone()
            if attribution is None:
                raise KeyError("draft_id has no registered attribution")
            if attribution[0] != outcome.published_tweet_id:
                raise ValueError("published_tweet_id does not match draft attribution")
            prior = connection.execute(
                "SELECT observed_at FROM outcome_snapshots "
                "WHERE draft_id = ? AND horizon = ?",
                (str(outcome.draft_id), outcome.horizon),
            ).fetchone()
            observed_at = outcome.observed_at.isoformat()
            if prior and datetime.fromisoformat(prior[0]) > outcome.observed_at:
                raise ValueError("outcome observations must not move backwards in time")
            connection.execute(
                "INSERT INTO outcome_snapshots(draft_id, horizon, published_tweet_id, "
                "observed_at, payload_json) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(draft_id, horizon) DO UPDATE SET "
                "published_tweet_id=excluded.published_tweet_id, "
                "observed_at=excluded.observed_at, payload_json=excluded.payload_json",
                (
                    str(outcome.draft_id),
                    outcome.horizon,
                    outcome.published_tweet_id,
                    observed_at,
                    _json(outcome),
                ),
            )
        return outcome

    def get_outcome(
        self, draft_id: UUID | str, horizon: str
    ) -> OutcomeSnapshotRecord | None:
        with self.database.read() as connection:
            row = connection.execute(
                "SELECT payload_json FROM outcome_snapshots "
                "WHERE draft_id = ? AND horizon = ?",
                (str(draft_id), horizon),
            ).fetchone()
        return _load(OutcomeSnapshotRecord, row[0]) if row else None

    def list_outcomes(
        self, *, draft_id: UUID | str | None = None
    ) -> list[OutcomeSnapshotRecord]:
        query = "SELECT payload_json FROM outcome_snapshots"
        params: tuple[str, ...] = ()
        if draft_id is not None:
            query += " WHERE draft_id = ?"
            params = (str(draft_id),)
        query += " ORDER BY observed_at, draft_id, horizon"
        with self.database.read() as connection:
            rows = connection.execute(query, params).fetchall()
        return [_load(OutcomeSnapshotRecord, row[0]) for row in rows]

    def delete_outcome(self, draft_id: UUID | str, horizon: str) -> bool:
        with self.database.transaction() as connection:
            cursor = connection.execute(
                "DELETE FROM outcome_snapshots WHERE draft_id = ? AND horizon = ?",
                (str(draft_id), horizon),
            )
        return cursor.rowcount > 0

    def save_creator_baseline(
        self, baseline: CreatorBaselineRecord
    ) -> CreatorBaselineRecord:
        with self.database.transaction() as connection:
            connection.execute(
                "INSERT INTO creator_baselines(creator_id, payload_json, updated_at) "
                "VALUES (?, ?, ?) ON CONFLICT(creator_id) DO UPDATE SET "
                "payload_json=excluded.payload_json, updated_at=excluded.updated_at",
                (
                    baseline.creator_id,
                    _json(baseline),
                    datetime.now(UTC).isoformat(),
                ),
            )
        return baseline

    def get_creator_baseline(self, creator_id: str) -> CreatorBaselineRecord | None:
        with self.database.read() as connection:
            row = connection.execute(
                "SELECT payload_json FROM creator_baselines WHERE creator_id = ?",
                (creator_id,),
            ).fetchone()
        return _load(CreatorBaselineRecord, row[0]) if row else None

    def list_creator_baselines(self) -> list[CreatorBaselineRecord]:
        with self.database.read() as connection:
            rows = connection.execute(
                "SELECT payload_json FROM creator_baselines ORDER BY creator_id"
            ).fetchall()
        return [_load(CreatorBaselineRecord, row[0]) for row in rows]

    def delete_creator_baseline(self, creator_id: str) -> bool:
        with self.database.transaction() as connection:
            cursor = connection.execute(
                "DELETE FROM creator_baselines WHERE creator_id = ?", (creator_id,)
            )
        return cursor.rowcount > 0

    def save_alert_rule(self, rule: AlertRuleConfig) -> AlertRuleConfig:
        self._save_alert_config("alert_rules", "rule_id", rule.rule_id, rule)
        return rule

    def get_alert_rule(self, rule_id: str) -> AlertRuleConfig | None:
        return self._get_alert_config(
            "alert_rules", "rule_id", rule_id, AlertRuleConfig
        )

    def list_alert_rules(self) -> list[AlertRuleConfig]:
        return self._list_alert_config("alert_rules", "rule_id", AlertRuleConfig)

    def delete_alert_rule(self, rule_id: str) -> bool:
        return self._delete_alert_config("alert_rules", "rule_id", rule_id)

    def save_alert_webhook(self, webhook: AlertWebhookConfig) -> AlertWebhookConfig:
        self._save_alert_config(
            "alert_webhooks", "webhook_id", webhook.webhook_id, webhook
        )
        return webhook

    def get_alert_webhook(self, webhook_id: str) -> AlertWebhookConfig | None:
        return self._get_alert_config(
            "alert_webhooks", "webhook_id", webhook_id, AlertWebhookConfig
        )

    def list_alert_webhooks(self) -> list[AlertWebhookConfig]:
        return self._list_alert_config(
            "alert_webhooks", "webhook_id", AlertWebhookConfig
        )

    def delete_alert_webhook(self, webhook_id: str) -> bool:
        return self._delete_alert_config("alert_webhooks", "webhook_id", webhook_id)

    def replace_alert_configuration(
        self,
        *,
        rules: list[AlertRuleConfig],
        webhooks: list[AlertWebhookConfig],
    ) -> None:
        """Atomically replace configured rules and webhook targets."""
        if len({rule.rule_id for rule in rules}) != len(rules):
            raise ValueError("alert rule IDs must be unique")
        if len({webhook.webhook_id for webhook in webhooks}) != len(webhooks):
            raise ValueError("webhook IDs must be unique")
        with self.database.transaction() as connection:
            connection.execute("DELETE FROM alert_rules")
            connection.execute("DELETE FROM alert_webhooks")
            connection.executemany(
                "INSERT INTO alert_rules(rule_id, payload_json) VALUES (?, ?)",
                [(rule.rule_id, _json(rule)) for rule in rules],
            )
            connection.executemany(
                "INSERT INTO alert_webhooks(webhook_id, payload_json) VALUES (?, ?)",
                [(webhook.webhook_id, _json(webhook)) for webhook in webhooks],
            )

    def _save_alert_config(
        self, table: str, key: str, value: str, model: BaseModel
    ) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                f"INSERT INTO {table}({key}, payload_json) VALUES (?, ?) "
                f"ON CONFLICT({key}) DO UPDATE SET payload_json=excluded.payload_json",
                (value, _json(model)),
            )

    def _get_alert_config(
        self, table: str, key: str, value: str, model: type[BaseModel]
    ):
        with self.database.read() as connection:
            row = connection.execute(
                f"SELECT payload_json FROM {table} WHERE {key} = ?", (value,)
            ).fetchone()
        return _load(model, row[0]) if row else None

    def _list_alert_config(self, table: str, key: str, model: type[BaseModel]) -> list:
        with self.database.read() as connection:
            rows = connection.execute(
                f"SELECT payload_json FROM {table} ORDER BY {key}"
            ).fetchall()
        return [_load(model, row[0]) for row in rows]

    def _delete_alert_config(self, table: str, key: str, value: str) -> bool:
        with self.database.transaction() as connection:
            cursor = connection.execute(
                f"DELETE FROM {table} WHERE {key} = ?", (value,)
            )
        return cursor.rowcount > 0
