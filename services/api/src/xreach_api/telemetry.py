"""Process-local closed-loop attribution and creator lift summaries."""

from __future__ import annotations

from threading import RLock
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictInt

from .models import DraftInputRecord, OutcomeSnapshotRecord, PublicationContextRecord


class DraftTelemetryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    creator_id: str = Field(min_length=1, max_length=64)
    baseline_24h_impressions: StrictInt | None = Field(default=None, ge=0)
    draft: DraftInputRecord


class TelemetryStore:
    """In-memory bridge; callers can replace it with durable storage later."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._drafts: dict[UUID, DraftTelemetryRequest] = {}
        self._publications: dict[UUID, PublicationContextRecord] = {}
        self._outcomes: dict[tuple[UUID, str], OutcomeSnapshotRecord] = {}

    def register_draft(self, item: DraftTelemetryRequest) -> DraftTelemetryRequest:
        with self._lock:
            existing = self._drafts.get(item.draft.draft_id)
            if existing and existing != item:
                raise ValueError("draft_id is already registered with different data")
            self._drafts[item.draft.draft_id] = item
        return item

    def publish(self, item: PublicationContextRecord) -> PublicationContextRecord:
        with self._lock:
            if item.draft_id not in self._drafts:
                raise KeyError("draft_id is not registered")
            existing = self._publications.get(item.draft_id)
            if existing and existing != item:
                raise ValueError("draft_id is already attributed to another publication")
            self._publications[item.draft_id] = item
        return item

    def record_outcome(self, item: OutcomeSnapshotRecord) -> OutcomeSnapshotRecord:
        with self._lock:
            publication = self._publications.get(item.draft_id)
            if publication is None:
                raise KeyError("draft_id has no registered publication")
            if publication.published_tweet_id != item.published_tweet_id:
                raise ValueError("published_tweet_id does not match draft attribution")
            if item.horizon not in {"24h", "7d"}:
                raise ValueError("closed-loop outcomes support only 24h and 7d horizons")
            key = (item.draft_id, item.horizon)
            existing = self._outcomes.get(key)
            if existing and item.observed_at < existing.observed_at:
                raise ValueError("outcome observations must not move backwards in time")
            self._outcomes[key] = item
        return item

    def summary(self) -> dict[str, object]:
        with self._lock:
            drafts = list(self._drafts.values())
            publications = dict(self._publications)
            outcomes = dict(self._outcomes)
        lifts: list[float] = []
        for draft_id, item in {item.draft.draft_id: item for item in drafts}.items():
            outcome = outcomes.get((draft_id, "24h"))
            baseline = item.baseline_24h_impressions
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
                (draft_id, "24h") in outcomes for draft_id in publications
            ),
            "outcomes_7d": sum(
                (draft_id, "7d") in outcomes for draft_id in publications
            ),
            "mean_creator_lift_delta_percent": (
                sum(lifts) / len(lifts) if lifts else None
            ),
            "creator_lift_sample_size": len(lifts),
        }
