"""Map hierarchical Semantic IDs to active SimClusters and viral roots."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .seed_scout import ScoutConfig, SeedScout, ViralSeed

SID_LEVELS = 6
SID_CODEBOOK_SIZE = 256
SemanticId = tuple[int, int, int, int, int, int]


def semantic_id(value: Sequence[object]) -> SemanticId:
    """Validate and normalize one six-level, 256-codebook Semantic ID."""

    if isinstance(value, (str, bytes)) or len(value) != SID_LEVELS:
        raise ValueError("semantic ID must contain exactly six codes")
    codes: list[int] = []
    for item in value:
        if not isinstance(item, int) or isinstance(item, bool):
            raise TypeError("semantic ID codes must be integers from 0 through 255")
        if not 0 <= item < SID_CODEBOOK_SIZE:
            raise ValueError("semantic ID codes must be integers from 0 through 255")
        codes.append(item)
    return tuple(codes)  # type: ignore[return-value]


def shared_prefix_levels(left: Sequence[int], right: Sequence[int]) -> int:
    """Return the number of identical leading hierarchy levels."""

    return next(
        (index for index, pair in enumerate(zip(left, right)) if pair[0] != pair[1]),
        min(len(left), len(right)),
    )


@dataclass(frozen=True)
class ActiveSimCluster:
    cluster_id: str
    name: str
    semantic_prefix: tuple[int, ...]
    aliases: tuple[str, ...] = ()
    active_until: datetime | None = None

    def __post_init__(self) -> None:
        if not self.cluster_id.strip() or not self.name.strip():
            raise ValueError("cluster_id and name must not be empty")
        if not 1 <= len(self.semantic_prefix) <= SID_LEVELS:
            raise ValueError("semantic_prefix must contain one through six codes")
        if any(
            isinstance(code, bool)
            or not isinstance(code, int)
            or not 0 <= code < SID_CODEBOOK_SIZE
            for code in self.semantic_prefix
        ):
            raise ValueError(
                "semantic_prefix codes must be integers from 0 through 255"
            )
        if self.active_until is not None and self.active_until.tzinfo is None:
            raise ValueError("active_until must include a timezone")

    def is_active(self, now: datetime) -> bool:
        return self.active_until is None or now < self.active_until.astimezone(
            timezone.utc
        )

    @property
    def labels(self) -> frozenset[str]:
        return frozenset(
            label.strip().casefold()
            for label in (self.cluster_id, self.name, *self.aliases)
            if label.strip()
        )


@dataclass(frozen=True)
class ClusterMatch:
    cluster_id: str
    name: str
    prefix_levels: int
    alignment_score: float


class SimClusterIndex:
    """An immutable catalog of explicit active-cluster SID prefixes."""

    def __init__(self, clusters: Iterable[ActiveSimCluster] = ()) -> None:
        materialized = tuple(clusters)
        identifiers = [cluster.cluster_id.casefold() for cluster in materialized]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("cluster IDs must be unique")
        self._clusters = materialized

    @classmethod
    def from_payload(cls, payload: Any) -> SimClusterIndex:
        records = payload.get("clusters") if isinstance(payload, Mapping) else payload
        if not isinstance(records, list):
            raise TypeError("SimCluster catalog must be a list or a clusters object")
        return cls(_cluster_from_mapping(record) for record in records)

    def match(
        self,
        draft_semantic_id: Sequence[object],
        *,
        now: datetime | None = None,
        min_prefix_levels: int = 1,
    ) -> list[ClusterMatch]:
        query = semantic_id(draft_semantic_id)
        observed_at = _utc(now or datetime.now(timezone.utc))
        if not 1 <= min_prefix_levels <= SID_LEVELS:
            raise ValueError("min_prefix_levels must be between one and six")
        matches = [
            ClusterMatch(
                cluster_id=cluster.cluster_id,
                name=cluster.name,
                prefix_levels=len(cluster.semantic_prefix),
                alignment_score=len(cluster.semantic_prefix) / SID_LEVELS,
            )
            for cluster in self._clusters
            if cluster.is_active(observed_at)
            and len(cluster.semantic_prefix) >= min_prefix_levels
            and tuple(query[: len(cluster.semantic_prefix)]) == cluster.semantic_prefix
        ]
        return sorted(matches, key=lambda item: (-item.prefix_levels, item.cluster_id))

    def matching_clusters(
        self,
        draft_semantic_id: Sequence[object],
        labels: Iterable[str],
        *,
        now: datetime,
        min_prefix_levels: int,
    ) -> tuple[ClusterMatch, ...]:
        normalized_labels = {
            label.strip().casefold() for label in labels if label.strip()
        }
        matches_by_id = {
            match.cluster_id.casefold(): match
            for match in self.match(
                draft_semantic_id,
                now=now,
                min_prefix_levels=min_prefix_levels,
            )
        }
        result = []
        for cluster in self._clusters:
            match = matches_by_id.get(cluster.cluster_id.casefold())
            if match is not None and normalized_labels.intersection(cluster.labels):
                result.append(match)
        return tuple(
            sorted(result, key=lambda item: (-item.prefix_levels, item.cluster_id))
        )


@dataclass(frozen=True)
class AlignedViralSeed:
    seed: ViralSeed
    prefix_levels: int
    alignment_score: float
    matched_simclusters: tuple[ClusterMatch, ...]


class SimClusterScout:
    """Query captured live roots aligned to one extracted draft Semantic ID."""

    def __init__(
        self,
        index: SimClusterIndex | None = None,
        scout_config: ScoutConfig | None = None,
        *,
        min_prefix_levels: int = 2,
    ) -> None:
        if not 1 <= min_prefix_levels <= SID_LEVELS:
            raise ValueError("min_prefix_levels must be between one and six")
        self.index = index or SimClusterIndex()
        self.seed_scout = SeedScout(scout_config)
        self.min_prefix_levels = min_prefix_levels

    def query(
        self,
        capture: Any,
        draft_semantic_id: Sequence[object],
        *,
        now: datetime | None = None,
    ) -> list[AlignedViralSeed]:
        query = semantic_id(draft_semantic_id)
        observed_at = _utc(now or datetime.now(timezone.utc))
        aligned: list[AlignedViralSeed] = []
        for seed in self.seed_scout.scan(capture, now=observed_at):
            direct_levels = (
                shared_prefix_levels(query, seed.semantic_id)
                if seed.semantic_id is not None
                else 0
            )
            clusters = self.index.matching_clusters(
                query,
                seed.simclusters,
                now=observed_at,
                min_prefix_levels=self.min_prefix_levels,
            )
            catalog_levels = max((match.prefix_levels for match in clusters), default=0)
            levels = max(direct_levels, catalog_levels)
            if levels < self.min_prefix_levels:
                continue
            aligned.append(
                AlignedViralSeed(
                    seed=seed,
                    prefix_levels=levels,
                    alignment_score=levels / SID_LEVELS,
                    matched_simclusters=clusters,
                )
            )
        return sorted(
            aligned,
            key=lambda item: (
                -item.prefix_levels,
                -item.seed.views_per_minute,
                -item.seed.impressions,
                item.seed.tweet_id,
            ),
        )


def _cluster_from_mapping(value: Any) -> ActiveSimCluster:
    if not isinstance(value, Mapping):
        raise TypeError("each SimCluster must be an object")
    cluster_id = value.get("cluster_id", value.get("id"))
    name = value.get("name")
    prefix = value.get("semantic_prefix", value.get("semantic_id"))
    aliases = value.get("aliases", ())
    if not isinstance(cluster_id, str) or not isinstance(name, str):
        raise TypeError("each SimCluster requires string cluster_id and name")
    if not isinstance(prefix, list):
        raise TypeError("each SimCluster requires a semantic_prefix list")
    if not isinstance(aliases, list | tuple) or not all(
        isinstance(alias, str) for alias in aliases
    ):
        raise TypeError("SimCluster aliases must be strings")
    active_until = _timestamp(value.get("active_until"))
    return ActiveSimCluster(
        cluster_id=cluster_id,
        name=name,
        semantic_prefix=tuple(prefix),
        aliases=tuple(aliases),
        active_until=active_until,
    )


def _timestamp(value: Any) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError("active_until must be an ISO timestamp")
    try:
        return _utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError as error:
        raise ValueError("active_until must be an ISO timestamp") from error


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("timestamps must include a timezone")
    return value.astimezone(timezone.utc)
