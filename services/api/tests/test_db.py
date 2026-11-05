from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from xreach_api.db import CreatorBaselineRecord, Database, Repository
from xreach_api.models import (
    AlertRuleConfig,
    AlertWebhookConfig,
    DraftInputRecord,
    OutcomeSnapshotRecord,
    PublicationContextRecord,
)

NOW = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)


@pytest.fixture
def database(tmp_path):
    database = Database(tmp_path / "api.sqlite3")
    yield database
    database.close()


def make_draft(draft_id: UUID | None = None) -> DraftInputRecord:
    return DraftInputRecord(draft_id=draft_id or uuid4(), text="A useful draft")


def make_attribution(draft_id: UUID, tweet_id: str = "tweet-1"):
    return PublicationContextRecord(
        draft_id=draft_id,
        published_tweet_id=tweet_id,
        surface="PROFILE_POST",
        published_at=NOW,
    )


def make_outcome(draft_id: UUID, tweet_id: str = "tweet-1", *, observed_at=NOW):
    return OutcomeSnapshotRecord(
        draft_id=draft_id,
        published_tweet_id=tweet_id,
        horizon="24h",
        observed_at=observed_at,
        impressions=1200,
        likes=42,
        source="manual",
    )


def test_file_database_uses_wal_and_idempotent_migrations(tmp_path):
    path = tmp_path / "nested" / "api.sqlite3"
    first = Database(path)
    assert first.journal_mode.lower() == "wal"
    with first.read() as connection:
        versions = connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()
    first.close()

    second = Database(path)
    with second.read() as connection:
        again = connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()
    assert [row[0] for row in versions] == [1]
    assert [row[0] for row in again] == [1]
    assert second.journal_mode.lower() == "wal"
    second.close()


def test_telemetry_crud_round_trip_and_cascade_delete(database):
    repository = Repository(database)
    draft = make_draft()
    repository.create_draft(
        draft, creator_id="creator-1", baseline_24h_impressions=1000
    )
    assert repository.get_draft(draft.draft_id) == draft
    assert repository.list_drafts(creator_id="creator-1") == [draft]
    updated_draft = draft.model_copy(update={"text": "An updated draft"})
    assert repository.update_draft(
        draft.draft_id,
        updated_draft,
        creator_id="creator-1",
        baseline_24h_impressions=1100,
    )
    registration = repository.get_draft_registration(draft.draft_id)
    assert registration is not None
    assert registration.draft == updated_draft
    assert registration.baseline_24h_impressions == 1100
    assert repository.get_draft(uuid4()) is None

    attribution = make_attribution(draft.draft_id)
    repository.create_attribution(attribution)
    assert repository.get_attribution(draft.draft_id) == attribution
    assert repository.list_attributions() == [attribution]
    updated_attribution = attribution.model_copy(
        update={"published_at": NOW + timedelta(minutes=1)}
    )
    assert repository.update_attribution(updated_attribution)
    assert repository.get_attribution(draft.draft_id) == updated_attribution

    outcome = make_outcome(draft.draft_id)
    repository.save_outcome(outcome)
    assert repository.get_outcome(draft.draft_id, "24h") == outcome
    assert repository.list_outcomes(draft_id=draft.draft_id) == [outcome]
    updated = outcome.model_copy(
        update={"observed_at": NOW + timedelta(hours=1), "impressions": 1500}
    )
    repository.save_outcome(updated)
    assert repository.get_outcome(draft.draft_id, "24h") == updated
    assert repository.delete_outcome(draft.draft_id, "24h")
    assert not repository.delete_outcome(draft.draft_id, "24h")
    repository.save_outcome(outcome)

    assert repository.delete_draft(draft.draft_id)
    assert repository.get_draft(draft.draft_id) is None
    assert repository.get_attribution(draft.draft_id) is None
    assert repository.get_outcome(draft.draft_id, "24h") is None


def test_outcomes_require_matching_attribution_and_monotonic_observations(database):
    repository = Repository(database)
    draft = make_draft()
    repository.create_draft(draft, creator_id="creator-1")
    with pytest.raises(KeyError, match="attribution"):
        repository.save_outcome(make_outcome(draft.draft_id))
    repository.create_attribution(make_attribution(draft.draft_id))
    with pytest.raises(ValueError, match="does not match"):
        repository.save_outcome(make_outcome(draft.draft_id, "other-tweet"))
    repository.save_outcome(make_outcome(draft.draft_id))
    with pytest.raises(ValueError, match="backwards"):
        repository.save_outcome(
            make_outcome(draft.draft_id, observed_at=NOW - timedelta(seconds=1))
        )
    changed_tweet = repository.get_attribution(draft.draft_id).model_copy(
        update={"published_tweet_id": "new-tweet"}
    )
    with pytest.raises(ValueError, match="after outcomes"):
        repository.update_attribution(changed_tweet)


def test_creator_baselines_and_alert_configuration_have_crud(database):
    repository = Repository(database)
    baseline = CreatorBaselineRecord(
        creator_id="creator-1",
        observation_count=3,
        mu=7.1,
        sigma=0.4,
        impression_floor=1,
        mean_impressions=1400,
        median_impressions=1200,
        format_breakdown=[{"content_format": "text", "post_count": 3}],
    )
    repository.save_creator_baseline(baseline)
    assert repository.get_creator_baseline("creator-1") == baseline
    changed_baseline = baseline.model_copy(update={"observation_count": 4})
    repository.save_creator_baseline(changed_baseline)
    assert repository.list_creator_baselines() == [changed_baseline]
    assert repository.delete_creator_baseline("creator-1")
    assert repository.get_creator_baseline("creator-1") is None

    rule = AlertRuleConfig(rule_id="fast-tech", simclusters=["Technology"])
    repository.save_alert_rule(rule)
    assert repository.get_alert_rule(rule.rule_id) == rule
    assert repository.list_alert_rules() == [rule]
    assert repository.delete_alert_rule(rule.rule_id)

    webhook = AlertWebhookConfig(
        webhook_id="team-chat",
        url="https://hooks.example.test/events",
        secret="test-secret",
        format="slack",
    )
    repository.save_alert_webhook(webhook)
    assert repository.get_alert_webhook(webhook.webhook_id) == webhook
    assert repository.list_alert_webhooks() == [webhook]
    replacement = webhook.model_copy(update={"secret": "rotated-secret"})
    repository.save_alert_webhook(replacement)
    assert repository.get_alert_webhook(webhook.webhook_id) == replacement
    assert repository.delete_alert_webhook(webhook.webhook_id)
    repository.replace_alert_configuration(rules=[rule], webhooks=[webhook])
    repository.replace_alert_configuration(rules=[], webhooks=[])
    assert repository.list_alert_rules() == []
    assert repository.list_alert_webhooks() == []
    with pytest.raises(ValueError, match="unique"):
        repository.replace_alert_configuration(rules=[rule, rule], webhooks=[])
    assert repository.list_alert_rules() == []


def test_transactions_rollback_and_connection_is_thread_safe(database):
    with (
        pytest.raises(RuntimeError, match="rollback"),
        database.transaction() as connection,
    ):
        connection.execute(
            "INSERT INTO creator_baselines(creator_id, payload_json, updated_at) "
            "VALUES ('bad', '{}', 'now')"
        )
        raise RuntimeError("rollback")
    assert Repository(database).get_creator_baseline("bad") is None

    repository = Repository(database)
    drafts = [make_draft() for _ in range(24)]

    def insert(draft):
        repository.create_draft(draft, creator_id="concurrent-creator")

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(insert, drafts))
    assert len(repository.list_drafts(creator_id="concurrent-creator")) == len(drafts)


def test_database_rejects_operations_after_close(tmp_path):
    database = Database(":memory:")
    database.close()
    with pytest.raises(RuntimeError, match="closed"):
        _ = database.journal_mode
