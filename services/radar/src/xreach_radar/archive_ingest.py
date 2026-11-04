"""Parse first-party X archive exports into observed impression baselines."""

from __future__ import annotations

import csv
import io
import json
import math
import re
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

MAX_ARCHIVE_BYTES = 100 * 1024 * 1024
MAX_ARCHIVE_RECORDS = 1_000_000
IMPRESSION_FLOOR = 1

ArchiveSourceFormat = Literal["tweets_js", "csv"]
ContentFormat = Literal[
    "text",
    "image",
    "video",
    "animated_gif",
    "link",
    "poll",
    "mixed",
    "other",
]

_ID_FIELDS = ("tweet_id", "tweetid", "id", "id_str")
_PERMALINK_FIELDS = ("tweet_permalink", "permalink", "url", "tweet_url")
_TEXT_FIELDS = ("tweet_text", "full_text", "text")
_DATE_FIELDS = ("time", "date", "created_at", "created")
_IMPRESSION_FIELDS = (
    "impressions",
    "impression_count",
    "organic_impressions",
    "views",
    "view_count",
)
_FORMAT_FIELDS = ("content_format", "format", "media_type", "tweet_type")
_STATUS_ID = re.compile(r"/(?:status|statuses)/(\d+)(?:[/?#]|$)")


@dataclass(frozen=True)
class ArchivePost:
    """One archive post with an observed, never inferred, impression count."""

    tweet_id: str
    impressions: int
    content_format: ContentFormat
    text: str = ""
    created_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.tweet_id.strip():
            raise ValueError("tweet_id must not be empty")
        if self.impressions < 0:
            raise ValueError("impressions must be non-negative")
        if self.created_at is not None and self.created_at.tzinfo is None:
            raise ValueError("created_at must include a timezone")


@dataclass(frozen=True)
class FormatBreakdown:
    content_format: ContentFormat
    post_count: int
    total_impressions: int
    mean_impressions: float
    impression_share: float


@dataclass(frozen=True)
class AuthorBaseline:
    """Log-normal baseline fitted to observed impressions with a floor of one."""

    distribution: Literal["log_normal"]
    observation_count: int
    mu: float
    sigma: float
    impression_floor: int
    mean_impressions: float
    median_impressions: float


@dataclass(frozen=True)
class ArchiveIngestReport:
    source_format: ArchiveSourceFormat
    posts: tuple[ArchivePost, ...]
    format_breakdown: tuple[FormatBreakdown, ...]
    author_baseline: AuthorBaseline
    skipped_records: int


class ArchiveIngestor:
    """Read official ``tweets.js`` or CSV exports without external APIs."""

    def __init__(
        self,
        *,
        max_bytes: int = MAX_ARCHIVE_BYTES,
        max_records: int = MAX_ARCHIVE_RECORDS,
    ) -> None:
        if max_bytes < 1 or max_records < 1:
            raise ValueError("archive limits must be positive")
        self.max_bytes = max_bytes
        self.max_records = max_records

    def ingest(self, path: str | Path) -> ArchiveIngestReport:
        source = Path(path)
        if source.is_dir():
            source = _archive_file(source)
        size = source.stat().st_size
        if size > self.max_bytes:
            raise ValueError(f"archive file exceeds {self.max_bytes} bytes")
        text = source.read_text(encoding="utf-8-sig")
        if source.suffix.casefold() == ".csv":
            return self.parse_csv(text)
        if source.suffix.casefold() == ".js" or source.name.casefold() == "tweets.js":
            return self.parse_tweets_js(text)
        raise ValueError(
            "archive input must be tweets.js, a CSV file, or an archive directory"
        )

    def parse_tweets_js(self, text: str) -> ArchiveIngestReport:
        self._check_text_size(text)
        payload = _javascript_payload(text)
        if not isinstance(payload, list):
            raise TypeError("tweets.js must contain a JSON array")
        if len(payload) > self.max_records:
            raise ValueError(f"archive contains more than {self.max_records} records")

        posts: list[ArchivePost] = []
        skipped = 0
        seen: set[str] = set()
        for index, wrapper in enumerate(payload, start=1):
            if not isinstance(wrapper, Mapping):
                skipped += 1
                continue
            value = wrapper.get("tweet", wrapper)
            if not isinstance(value, Mapping):
                skipped += 1
                continue
            post = _post_from_tweet(value, index)
            if post is None or post.tweet_id in seen:
                skipped += 1
                continue
            posts.append(post)
            seen.add(post.tweet_id)
        return _report("tweets_js", posts, skipped)

    def parse_csv(self, text: str) -> ArchiveIngestReport:
        self._check_text_size(text)
        stream = io.StringIO(text.lstrip("\ufeff"), newline="")
        sample = text[:8192]
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",\t;")
        except csv.Error:
            dialect = csv.excel
        reader = csv.DictReader(stream, dialect=dialect)
        if not reader.fieldnames:
            raise ValueError("archive CSV must include a header row")

        posts: list[ArchivePost] = []
        skipped = 0
        seen: set[str] = set()
        for row_number, raw in enumerate(reader, start=2):
            if row_number - 1 > self.max_records:
                raise ValueError(
                    f"archive contains more than {self.max_records} records"
                )
            row = _normalized_row(raw)
            post = _post_from_csv(row, row_number)
            if post is None or post.tweet_id in seen:
                skipped += 1
                continue
            posts.append(post)
            seen.add(post.tweet_id)
        return _report("csv", posts, skipped)

    def _check_text_size(self, text: str) -> None:
        if len(text.encode("utf-8")) > self.max_bytes:
            raise ValueError(f"archive content exceeds {self.max_bytes} bytes")


def ingest_archive(path: str | Path) -> ArchiveIngestReport:
    """Convenience wrapper using the default bounded archive parser."""

    return ArchiveIngestor().ingest(path)


def _archive_file(directory: Path) -> Path:
    candidates = (
        directory / "data" / "tweets.js",
        directory / "tweets.js",
        directory / "data" / "tweets.csv",
        directory / "tweets.csv",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    csv_files = sorted(directory.glob("*.csv"))
    if len(csv_files) == 1:
        return csv_files[0]
    raise ValueError(
        "archive directory does not contain data/tweets.js, tweets.js, or one CSV"
    )


def _javascript_payload(text: str) -> Any:
    start = text.find("[")
    if start < 0:
        raise ValueError("tweets.js does not contain an array payload")
    try:
        value, end = json.JSONDecoder().raw_decode(text[start:])
    except json.JSONDecodeError as error:
        raise ValueError(f"tweets.js contains invalid JSON: {error.msg}") from error
    trailing = text[start + end :].strip()
    if trailing not in {"", ";"}:
        raise ValueError("tweets.js contains unexpected data after its array payload")
    return value


def _post_from_tweet(value: Mapping[str, Any], index: int) -> ArchivePost | None:
    tweet_id = _text(value.get("id_str", value.get("id")))
    impressions_value = _nested_impressions(value)
    if not tweet_id or impressions_value is None:
        return None
    return ArchivePost(
        tweet_id=tweet_id,
        impressions=_impressions(impressions_value, f"tweets.js record {index}"),
        content_format=_tweet_format(value),
        text=_text(value.get("full_text", value.get("text"))),
        created_at=_timestamp(value.get("created_at"), f"tweets.js record {index}"),
    )


def _nested_impressions(value: Mapping[str, Any]) -> Any:
    for key in ("impression_count", "impressions", "view_count"):
        if key in value and value[key] not in (None, ""):
            return value[key]
    for container_name in ("views", "public_metrics"):
        container = value.get(container_name)
        if not isinstance(container, Mapping):
            continue
        for key in ("count", "impression_count", "views", "view_count"):
            if key in container and container[key] not in (None, ""):
                return container[key]
    return None


def _post_from_csv(row: Mapping[str, str], row_number: int) -> ArchivePost | None:
    tweet_id = _field(row, _ID_FIELDS)
    if not tweet_id:
        permalink = _field(row, _PERMALINK_FIELDS)
        match = _STATUS_ID.search(permalink)
        tweet_id = match.group(1) if match else ""
    impressions_value = _field(row, _IMPRESSION_FIELDS)
    if not tweet_id or impressions_value == "":
        return None
    text = _field(row, _TEXT_FIELDS)
    return ArchivePost(
        tweet_id=tweet_id,
        impressions=_impressions(impressions_value, f"CSV row {row_number}"),
        content_format=_csv_format(row, text),
        text=text,
        created_at=_timestamp(_field(row, _DATE_FIELDS), f"CSV row {row_number}"),
    )


def _normalized_row(row: Mapping[str | None, Any]) -> dict[str, str]:
    normalized: dict[str, str] = {}
    for key, value in row.items():
        if key is None:
            continue
        normalized[_header(key)] = _text(value).strip()
    return normalized


def _header(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.strip().casefold()).strip("_")


def _field(row: Mapping[str, str], names: Sequence[str]) -> str:
    return next((row[name] for name in names if row.get(name, "") != ""), "")


def _impressions(value: Any, location: str) -> int:
    if isinstance(value, bool):
        raise TypeError(f"{location} impressions must be a non-negative integer")
    if isinstance(value, int):
        result = value
    elif isinstance(value, float) and value.is_integer():
        result = int(value)
    elif isinstance(value, str):
        normalized = value.strip().replace(",", "").replace("_", "")
        try:
            result = int(normalized)
        except ValueError as error:
            raise ValueError(
                f"{location} impressions must be a non-negative integer"
            ) from error
    else:
        raise ValueError(f"{location} impressions must be a non-negative integer")
    if result < 0:
        raise ValueError(f"{location} impressions must be a non-negative integer")
    return result


def _timestamp(value: Any, location: str) -> datetime | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise TypeError(f"{location} timestamp must be text")
    raw = value.strip()
    parsed: datetime | None = None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = datetime.strptime(raw, "%a %b %d %H:%M:%S %z %Y")
        except ValueError as error:
            raise ValueError(f"{location} contains an invalid timestamp") from error
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _tweet_format(value: Mapping[str, Any]) -> ContentFormat:
    if value.get("poll"):
        return "poll"
    card = value.get("card")
    if isinstance(card, Mapping) and _text(card.get("name")).casefold().startswith(
        "poll"
    ):
        return "poll"
    media_types: set[str] = set()
    for container_name in ("extended_entities", "entities"):
        container = value.get(container_name)
        if not isinstance(container, Mapping):
            continue
        media = container.get("media", ())
        if isinstance(media, list | tuple):
            for item in media:
                if isinstance(item, Mapping):
                    media_type = _text(item.get("type")).casefold()
                    if media_type:
                        media_types.add(media_type)
    if len(media_types) > 1:
        return "mixed"
    if media_types:
        return _normalized_format(next(iter(media_types)))
    entities = value.get("entities")
    if isinstance(entities, Mapping) and entities.get("urls"):
        return "link"
    if re.search(r"https?://", _text(value.get("full_text", value.get("text")))):
        return "link"
    return "text"


def _csv_format(row: Mapping[str, str], text: str) -> ContentFormat:
    explicit = _field(row, _FORMAT_FIELDS)
    if explicit:
        return _normalized_format(explicit)
    media_url = _field(row, ("media_url", "media_urls", "media"))
    lowered = media_url.casefold()
    if re.search(r"\.(?:mp4|mov|webm)(?:[?#]|$)", lowered):
        return "video"
    if re.search(r"\.gif(?:[?#]|$)", lowered):
        return "animated_gif"
    if media_url:
        return "image"
    if re.search(r"https?://", text):
        return "link"
    return "text"


def _normalized_format(value: str) -> ContentFormat:
    normalized = _header(value)
    if normalized in {"photo", "photos", "image", "images"}:
        return "image"
    if normalized in {"video", "videos"}:
        return "video"
    if normalized in {"animated_gif", "gif"}:
        return "animated_gif"
    if normalized in {"text", "text_only", "post"}:
        return "text"
    if normalized in {"url", "link", "links"}:
        return "link"
    if normalized in {"poll", "polls"}:
        return "poll"
    if normalized in {"mixed", "multi_media", "carousel"}:
        return "mixed"
    return "other"


def _report(
    source_format: ArchiveSourceFormat,
    posts: Sequence[ArchivePost],
    skipped_records: int,
) -> ArchiveIngestReport:
    if not posts:
        raise ValueError("archive contains no posts with observed impressions")
    impressions = [post.impressions for post in posts]
    logged = [math.log(max(IMPRESSION_FLOOR, value)) for value in impressions]
    baseline = AuthorBaseline(
        distribution="log_normal",
        observation_count=len(posts),
        mu=statistics.fmean(logged),
        sigma=statistics.pstdev(logged),
        impression_floor=IMPRESSION_FLOOR,
        mean_impressions=statistics.fmean(impressions),
        median_impressions=statistics.median(impressions),
    )
    total = sum(impressions)
    breakdown: list[FormatBreakdown] = []
    for content_format in sorted({post.content_format for post in posts}):
        values = [
            post.impressions for post in posts if post.content_format == content_format
        ]
        subtotal = sum(values)
        breakdown.append(
            FormatBreakdown(
                content_format=content_format,
                post_count=len(values),
                total_impressions=subtotal,
                mean_impressions=statistics.fmean(values),
                impression_share=subtotal / total if total else 0.0,
            )
        )
    return ArchiveIngestReport(
        source_format=source_format,
        posts=tuple(posts),
        format_breakdown=tuple(breakdown),
        author_baseline=baseline,
        skipped_records=skipped_records,
    )


def _text(value: Any) -> str:
    return value if isinstance(value, str) else "" if value is None else str(value)
