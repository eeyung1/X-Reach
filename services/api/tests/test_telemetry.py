from datetime import datetime, timezone
from uuid import UUID

import pytest
from pydantic import ValidationError

from xreach_api.models import (
    AccountContextRecord,
    DraftInputRecord,
    OutcomeSnapshotRecord,
    PublicationContextRecord,
)


DRAFT_ID = UUID("97bb1fd8-3e1d-4d46-9c06-812f57f231d5")
OBSERVED_AT = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)


def test_draft_record_is_versioned_and_accepts_text_or_media():
    text_record = DraftInputRecord(draft_id=DRAFT_ID, text="A draft")
    media_record = DraftInputRecord(
        draft_id=DRAFT_ID,
        media_types=["image"],
        local_topic_codes=[1, 2, 3, 4, 5, 255],
    )

    assert text_record.schema_version == "v1"
    assert text_record.recorded_at.tzinfo is not None
    assert media_record.model_dump(mode="json")["draft_id"] == str(DRAFT_ID)


@pytest.mark.parametrize(
    "payload",
    [
        {"draft_id": DRAFT_ID},
        {"draft_id": DRAFT_ID, "text": " ", "media_types": []},
        {"draft_id": DRAFT_ID, "text": "A draft", "schema_version": "v2"},
        {"draft_id": DRAFT_ID, "text": "A draft", "unexpected": True},
        {
            "draft_id": DRAFT_ID,
            "text": "A draft",
            "local_topic_codes": [0, 1, 2, 3, 4],
        },
        {
            "draft_id": DRAFT_ID,
            "text": "A draft",
            "local_topic_codes": [0, 1, 2, 3, 4, 256],
        },
        {
            "draft_id": DRAFT_ID,
            "text": "A draft",
            "local_topic_codes": [0, 1, 2, 3, 4, True],
        },
    ],
)
def test_draft_record_rejects_invalid_contract_values(payload):
    with pytest.raises(ValidationError):
        DraftInputRecord.model_validate(payload)


def test_account_context_rejects_negative_counts():
    account = AccountContextRecord(account_id="123", follower_count=0, verified=False)
    assert account.schema_version == "v1"
    with pytest.raises(ValidationError):
        AccountContextRecord(account_id="123", follower_count=-1)
    with pytest.raises(ValidationError):
        AccountContextRecord(account_id="123", follower_count=True)


def test_publication_context_enforces_direct_root_reply_target():
    record = PublicationContextRecord(
        draft_id=DRAFT_ID,
        published_tweet_id="post-1",
        surface="DIRECT_ROOT_REPLY",
        published_at=OBSERVED_AT,
        conversation_id="root-1",
        in_reply_to_tweet_id="root-1",
    )
    assert record.surface == "DIRECT_ROOT_REPLY"

    with pytest.raises(ValidationError):
        PublicationContextRecord(
            draft_id=DRAFT_ID,
            published_tweet_id="post-2",
            surface="DIRECT_ROOT_REPLY",
            published_at=OBSERVED_AT,
            conversation_id="root-1",
            in_reply_to_tweet_id="reply-1",
        )


@pytest.mark.parametrize("horizon", ["5m", "30m", "2h", "24h", "7d"])
def test_outcome_snapshot_accepts_each_supported_horizon(horizon):
    snapshot = OutcomeSnapshotRecord(
        draft_id=DRAFT_ID,
        published_tweet_id="post-1",
        horizon=horizon,
        observed_at=OBSERVED_AT,
        impressions=100,
        likes=8,
        source="platform_api",
    )

    assert snapshot.schema_version == "v1"
    assert snapshot.horizon == horizon


def test_outcome_snapshot_requires_metrics_and_nonnegative_counters():
    base = {
        "draft_id": DRAFT_ID,
        "published_tweet_id": "post-1",
        "horizon": "5m",
        "observed_at": OBSERVED_AT,
    }
    with pytest.raises(ValidationError):
        OutcomeSnapshotRecord.model_validate(base)
    with pytest.raises(ValidationError):
        OutcomeSnapshotRecord.model_validate({**base, "likes": -1})
    with pytest.raises(ValidationError):
        OutcomeSnapshotRecord.model_validate({**base, "likes": True})


def test_timestamp_fields_must_be_timezone_aware():
    with pytest.raises(ValidationError):
        AccountContextRecord(
            account_id="123", recorded_at=datetime(2026, 11, 4, 12, 0)
        )
