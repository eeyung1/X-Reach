"""Detect and rank fresh viral root posts in captured X GraphQL responses."""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .graphql_sniffer import GraphQLDocument, iter_graphql_documents


@dataclass(frozen=True)
class ScoutConfig:
    max_age_minutes: float = 30.0
    min_views_per_minute: float = 500.0
    max_replies: int | None = 50
    target_simclusters: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if self.max_age_minutes <= 0:
            raise ValueError("max_age_minutes must be positive")
        if self.min_views_per_minute < 0:
            raise ValueError("min_views_per_minute must be non-negative")
        if self.max_replies is not None and self.max_replies < 0:
            raise ValueError("max_replies must be non-negative or None")


@dataclass(frozen=True)
class ViralSeed:
    tweet_id: str
    conversation_id: str
    author_id: str | None
    screen_name: str | None
    text: str | None
    created_at: datetime
    age_minutes: float
    impressions: int
    views_per_minute: float
    replies: int
    likes: int | None
    reposts: int | None
    quotes: int | None
    semantic_id: tuple[int, ...] | None
    simclusters: tuple[str, ...]
    source_operation: str | None
    source: str


class SeedScout:
    """Project captured timeline evidence into ranked root-post candidates."""

    def __init__(self, config: ScoutConfig | None = None) -> None:
        self.config = config or ScoutConfig()

    def scan(self, capture: Any, *, now: datetime | None = None) -> list[ViralSeed]:
        observed_at = _as_utc(now or datetime.now(timezone.utc))
        candidates: dict[str, ViralSeed] = {}
        for document in iter_graphql_documents(capture):
            for tweet in _iter_tweets(document):
                seed = self._candidate(tweet, document, observed_at)
                if seed is None:
                    continue
                previous = candidates.get(seed.tweet_id)
                if previous is None or seed.impressions > previous.impressions:
                    candidates[seed.tweet_id] = seed
        return sorted(
            candidates.values(),
            key=lambda item: (-item.views_per_minute, -item.impressions, item.tweet_id),
        )

    def _candidate(
        self,
        tweet: Mapping[str, Any],
        document: GraphQLDocument,
        now: datetime,
    ) -> ViralSeed | None:
        legacy = tweet.get("legacy")
        if not isinstance(legacy, Mapping):
            legacy = tweet

        tweet_id = _first_string(tweet, "rest_id", "id", "id_str")
        conversation_id = _first_string(
            legacy, "conversation_id_str", "conversation_id"
        )
        reply_parent_known, reply_parent = _reply_parent(
            legacy,
        )
        if (
            not tweet_id
            or conversation_id != tweet_id
            or not reply_parent_known
            or reply_parent is not None
        ):
            return None

        created_at = _parse_timestamp(
            _first_string(legacy, "created_at") or _first_string(tweet, "created_at")
        )
        impressions = _view_count(tweet, legacy)
        if created_at is None or impressions is None:
            return None
        age_minutes = (now - created_at).total_seconds() / 60.0
        if age_minutes < 0 or age_minutes >= self.config.max_age_minutes:
            return None
        views_per_minute = impressions / max(age_minutes, 1.0)
        if views_per_minute < self.config.min_views_per_minute:
            return None

        replies = _integer(legacy.get("reply_count"), default=None)
        if replies is None:
            return None
        if self.config.max_replies is not None and replies >= self.config.max_replies:
            return None
        clusters = _simclusters(tweet)
        if self.config.target_simclusters and not _clusters_match(
            clusters, self.config.target_simclusters
        ):
            return None

        author = _author(tweet)
        return ViralSeed(
            tweet_id=tweet_id,
            conversation_id=conversation_id,
            author_id=author[0],
            screen_name=author[1],
            text=_first_string(legacy, "full_text", "text"),
            created_at=created_at,
            age_minutes=age_minutes,
            impressions=impressions,
            views_per_minute=views_per_minute,
            replies=replies,
            likes=_integer(legacy.get("favorite_count"), default=None),
            reposts=_integer(
                legacy.get("retweet_count", legacy.get("repost_count")), default=None
            ),
            quotes=_integer(legacy.get("quote_count"), default=None),
            semantic_id=_semantic_id(tweet),
            simclusters=clusters,
            source_operation=document.operation,
            source=document.source,
        )


def _iter_tweets(document: GraphQLDocument) -> Iterator[Mapping[str, Any]]:
    stack: list[Any] = [document.payload]
    while stack:
        value = stack.pop()
        if isinstance(value, Mapping):
            candidate: Mapping[str, Any] = value
            wrapped = value.get("tweet")
            if isinstance(wrapped, Mapping):
                candidate = wrapped
            if _looks_like_tweet(candidate):
                tweet_id = _first_string(candidate, "rest_id", "id", "id_str")
                if tweet_id is not None:
                    yield candidate
            stack.extend(value.values())
        elif isinstance(value, list):
            stack.extend(value)


def _looks_like_tweet(value: Mapping[str, Any]) -> bool:
    legacy = value.get("legacy")
    return (
        _first_string(value, "rest_id", "id", "id_str") is not None
        and isinstance(legacy, Mapping)
        and any(
            key in legacy for key in ("created_at", "full_text", "conversation_id_str")
        )
    )


def _view_count(tweet: Mapping[str, Any], legacy: Mapping[str, Any]) -> int | None:
    views = tweet.get("views")
    if isinstance(views, Mapping):
        count = _integer(views.get("count"), default=None)
        if count is not None:
            return count
    public_metrics = tweet.get("public_metrics")
    if isinstance(public_metrics, Mapping):
        count = _integer(public_metrics.get("impression_count"), default=None)
        if count is not None:
            return count
    for owner in (tweet, legacy):
        for key in ("view_count", "impression_count", "views_count"):
            count = _integer(owner.get(key), default=None)
            if count is not None:
                return count
    return None


def _reply_parent(legacy: Mapping[str, Any]) -> tuple[bool, str | None]:
    keys = (
        "in_reply_to_status_id_str",
        "in_reply_to_tweet_id",
        "in_reply_to_status_id",
    )
    known = False
    for key in keys:
        if key not in legacy:
            continue
        value = legacy[key]
        if value is None:
            known = True
            continue
        parent = _first_string(legacy, key)
        if parent is None:
            return False, None
        return True, parent
    return known, None


def _author(tweet: Mapping[str, Any]) -> tuple[str | None, str | None]:
    core = tweet.get("core")
    if not isinstance(core, Mapping):
        return _first_string(tweet, "author_id"), None
    users = core.get("user_results")
    result = users.get("result") if isinstance(users, Mapping) else None
    if not isinstance(result, Mapping):
        return _first_string(tweet, "author_id"), None
    legacy = result.get("legacy")
    screen_name = (
        _first_string(legacy, "screen_name") if isinstance(legacy, Mapping) else None
    )
    return _first_string(result, "rest_id", "id", "id_str"), screen_name


def _simclusters(tweet: Mapping[str, Any]) -> tuple[str, ...]:
    labels: set[str] = set()
    cluster_keys = {
        "simcluster",
        "simclusters",
        "sim_cluster",
        "sim_clusters",
        "simcluster_id",
        "simcluster_ids",
    }
    stack: list[Any] = [tweet]
    while stack:
        value = stack.pop()
        if isinstance(value, Mapping):
            for key, child in value.items():
                if key.lower() in cluster_keys:
                    labels.update(_label_values(child))
                elif isinstance(child, (Mapping, list)):
                    stack.append(child)
        elif isinstance(value, list):
            stack.extend(value)
    return tuple(sorted(labels, key=str.casefold))


def _semantic_id(tweet: Mapping[str, Any]) -> tuple[int, ...] | None:
    stack: list[Any] = [tweet]
    while stack:
        value = stack.pop()
        if isinstance(value, Mapping):
            for key in ("semantic_id", "semantic_ids"):
                codes = value.get(key)
                if (
                    isinstance(codes, (list, tuple))
                    and len(codes) == 6
                    and all(
                        isinstance(code, int)
                        and not isinstance(code, bool)
                        and 0 <= code < 256
                        for code in codes
                    )
                ):
                    return tuple(codes)
            stack.extend(
                child
                for key, child in value.items()
                if key
                not in {"simcluster", "simclusters", "sim_cluster", "sim_clusters"}
                and isinstance(child, (Mapping, list))
            )
        elif isinstance(value, list):
            stack.extend(value)
    return None


def _label_values(value: Any) -> Iterable[str]:
    if isinstance(value, (str, int)) and not isinstance(value, bool):
        text = str(value).strip()
        if text:
            yield text
    elif isinstance(value, list):
        for item in value:
            yield from _label_values(item)
    elif isinstance(value, Mapping):
        for key in ("id", "name", "label", "value"):
            if key in value:
                yield from _label_values(value[key])


def _clusters_match(observed: tuple[str, ...], requested: frozenset[str]) -> bool:
    normalized = {value.casefold() for value in observed}
    return any(value.casefold() in normalized for value in requested)


def _parse_timestamp(value: str | None) -> datetime | None:
    if value is None:
        return None
    for parser in (
        lambda text: datetime.fromisoformat(text.replace("Z", "+00:00")),
        lambda text: datetime.strptime(text, "%a %b %d %H:%M:%S %z %Y"),
    ):
        try:
            return _as_utc(parser(value))
        except ValueError:
            continue
    return None


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("timestamps must include a timezone")
    return value.astimezone(timezone.utc)


def _first_string(owner: Mapping[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = owner.get(key)
        if isinstance(value, (str, int)) and not isinstance(value, bool):
            text = str(value).strip()
            if text and text.lower() not in {"none", "null"}:
                return text
    return None


def _integer(value: Any, *, default: int | None) -> int | None:
    if isinstance(value, bool):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 0 else default
