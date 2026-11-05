"""Closed-loop telemetry service backed by the API database repository."""

from __future__ import annotations

import sqlite3

from pydantic import BaseModel, ConfigDict, Field, StrictInt

from .db import Repository
from .db.models import DraftRegistrationRecord
from .models import DraftInputRecord, OutcomeSnapshotRecord, PublicationContextRecord


class DraftTelemetryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    creator_id: str = Field(min_length=1, max_length=64)
    baseline_24h_impressions: StrictInt | None = Field(default=None, ge=0)
    draft: DraftInputRecord


class TelemetryStore:
    """Preserve telemetry endpoint semantics while delegating storage to SQLite."""

    def __init__(self, repository: Repository) -> None:
        self.repository = repository

    def register_draft(self, item: DraftTelemetryRequest) -> DraftTelemetryRequest:
        draft_id = item.draft.draft_id
        existing = self.repository.get_draft_registration(draft_id)
        if existing:
            if self._same_draft_registration(existing, item):
                return item
            raise ValueError("draft_id is already registered with different data")
        try:
            self.repository.create_draft(
                item.draft,
                creator_id=item.creator_id,
                baseline_24h_impressions=item.baseline_24h_impressions,
            )
        except sqlite3.IntegrityError as error:
            existing = self.repository.get_draft_registration(draft_id)
            if existing and self._same_draft_registration(existing, item):
                return item
            raise ValueError("draft_id is already registered") from error
        return item

    def publish(self, item: PublicationContextRecord) -> PublicationContextRecord:
        existing = self.repository.get_attribution(item.draft_id)
        if existing:
            if existing == item:
                return item
            raise ValueError("draft_id is already attributed to another publication")
        if self.repository.get_draft_registration(item.draft_id) is None:
            raise KeyError("draft_id is not registered")
        try:
            self.repository.create_attribution(item)
        except sqlite3.IntegrityError as error:
            existing = self.repository.get_attribution(item.draft_id)
            if existing == item:
                return item
            raise ValueError("published_tweet_id is already attributed") from error
        return item

    def record_outcome(self, item: OutcomeSnapshotRecord) -> OutcomeSnapshotRecord:
        if item.horizon not in {"24h", "7d"}:
            raise ValueError("closed-loop outcomes support only 24h and 7d horizons")
        try:
            self.repository.save_outcome(item)
        except KeyError as error:
            raise KeyError("draft_id has no registered publication") from error
        return item

    def summary(self) -> dict[str, object]:
        drafts = self.repository.list_draft_registrations()
        publications = self.repository.list_attributions()
        outcomes = self.repository.list_outcomes()
        published_ids = {item.draft_id for item in publications}
        outcomes_by_key = {(item.draft_id, item.horizon): item for item in outcomes}
        lifts: list[float] = []
        for registration in drafts:
            draft_id = registration.draft.draft_id
            outcome = outcomes_by_key.get((draft_id, "24h"))
            baseline = registration.baseline_24h_impressions
            if (
                outcome
                and outcome.impressions is not None
                and baseline is not None
                and baseline > 0
            ):
                lifts.append((outcome.impressions - baseline) / baseline * 100)
        return {
            "drafts": len(drafts),
            "published": len(publications),
            "outcomes_24h": sum(
                (draft_id, "24h") in outcomes_by_key for draft_id in published_ids
            ),
            "outcomes_7d": sum(
                (draft_id, "7d") in outcomes_by_key for draft_id in published_ids
            ),
            "mean_creator_lift_delta_percent": (
                sum(lifts) / len(lifts) if lifts else None
            ),
            "creator_lift_sample_size": len(lifts),
        }

    @staticmethod
    def _same_draft_registration(
        existing: DraftRegistrationRecord, requested: DraftTelemetryRequest
    ) -> bool:
        return (
            existing.creator_id == requested.creator_id
            and existing.baseline_24h_impressions == requested.baseline_24h_impressions
            and existing.draft == requested.draft
        )
