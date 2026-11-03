"""Bounded SSE event broker and background REA capture scout."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import threading
from collections import OrderedDict, deque
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

logger = logging.getLogger(__name__)
DEFAULT_RING_CAPACITY = 500
DEFAULT_SUBSCRIBER_CAPACITY = 64


@dataclass(frozen=True)
class RadarEvent:
    event_id: int
    published_at: datetime
    simclusters: tuple[str, ...]
    payload: Mapping[str, Any]


@dataclass
class _Subscriber:
    queue: asyncio.Queue[RadarEvent]
    simclusters: frozenset[str]
    dropped_through_id: int | None = None


class RadarEventBuffer:
    """In-memory event history with bounded subscriber queues."""

    def __init__(
        self,
        capacity: int = DEFAULT_RING_CAPACITY,
        subscriber_capacity: int = DEFAULT_SUBSCRIBER_CAPACITY,
    ) -> None:
        if capacity < 1 or subscriber_capacity < 1:
            raise ValueError("radar buffer capacities must be positive")
        self._events: deque[RadarEvent] = deque(maxlen=capacity)
        self._subscriber_capacity = subscriber_capacity
        self._subscribers: dict[int, _Subscriber] = {}
        self._next_subscriber_id = 1
        self._next_event_id = 1
        self._lock = threading.RLock()

    def publish(self, payload: Mapping[str, Any]) -> RadarEvent:
        clusters = payload.get("simclusters", ())
        labels = (
            tuple(
                str(cluster).strip()
                for cluster in clusters
                if isinstance(cluster, (str, int)) and str(cluster).strip()
            )
            if isinstance(clusters, (list, tuple, set, frozenset))
            else ()
        )
        with self._lock:
            event = RadarEvent(
                event_id=self._next_event_id,
                published_at=datetime.now(timezone.utc),
                simclusters=labels,
                payload=dict(payload),
            )
            self._next_event_id += 1
            self._events.append(event)
            normalized_labels = {label.casefold() for label in labels}
            for subscriber in self._subscribers.values():
                if subscriber.simclusters and not normalized_labels.intersection(
                    subscriber.simclusters
                ):
                    continue
                if subscriber.queue.full():
                    try:
                        dropped = subscriber.queue.get_nowait()
                        subscriber.dropped_through_id = dropped.event_id
                    except asyncio.QueueEmpty:
                        pass
                try:
                    subscriber.queue.put_nowait(event)
                except asyncio.QueueFull:
                    pass
            return event

    def subscribe(
        self,
        *,
        after_id: int | None = None,
        simclusters: Sequence[str] = (),
    ) -> tuple[
        int,
        tuple[RadarEvent, ...],
        asyncio.Queue[RadarEvent],
        bool,
        int | None,
    ]:
        normalized = frozenset(
            label.strip().casefold() for label in simclusters if label.strip()
        )
        with self._lock:
            subscriber_id = self._next_subscriber_id
            self._next_subscriber_id += 1
            queue: asyncio.Queue[RadarEvent] = asyncio.Queue(
                maxsize=self._subscriber_capacity
            )
            self._subscribers[subscriber_id] = _Subscriber(queue, normalized)
            matching = tuple(
                event
                for event in self._events
                if (after_id is None or event.event_id > after_id)
                and (
                    not normalized
                    or normalized.intersection(
                        label.casefold() for label in event.simclusters
                    )
                )
            )
            oldest_id = self._events[0].event_id if self._events else None
            history_lost = (
                after_id is not None
                and oldest_id is not None
                and after_id < oldest_id - 1
            )
            return subscriber_id, matching, queue, history_lost, oldest_id

    def unsubscribe(self, subscriber_id: int) -> None:
        with self._lock:
            self._subscribers.pop(subscriber_id, None)

    def consume_dropped_through(self, subscriber_id: int) -> int | None:
        with self._lock:
            subscriber = self._subscribers.get(subscriber_id)
            if subscriber is None:
                return None
            dropped_id = subscriber.dropped_through_id
            subscriber.dropped_through_id = None
            return dropped_id


class CaptureProvider(Protocol):
    async def read_capture(self) -> Mapping[str, Any] | None: ...


class SeedScanner(Protocol):
    def scan(self, capture: Any) -> Sequence[Any]: ...


class CaptureFileProvider:
    """Read new REA/HAR capture snapshots from an explicitly configured file."""

    def __init__(self, path: str | Path | None) -> None:
        self.path = Path(path).expanduser() if path else None
        self._last_signature: tuple[int, int] | None = None

    async def read_capture(self) -> Mapping[str, Any] | None:
        if self.path is None:
            return None
        return await asyncio.to_thread(self._read_if_changed)

    def _read_if_changed(self) -> Mapping[str, Any] | None:
        try:
            stat = self.path.stat()
        except FileNotFoundError:
            return None
        signature = (stat.st_mtime_ns, stat.st_size)
        if signature == self._last_signature:
            return None
        if stat.st_size > 25 * 1024 * 1024:
            raise ValueError("REA capture snapshot exceeds the 25 MB limit")
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(payload, Mapping):
            raise ValueError("REA capture snapshot must contain a JSON object")
        self._last_signature = signature
        return payload


class RadarSeedProducer:
    """Poll capture snapshots and publish newly observed viral root seeds."""

    def __init__(
        self,
        event_buffer: RadarEventBuffer,
        capture_provider: CaptureProvider,
        scanner: SeedScanner,
        *,
        interval_seconds: float = 5.0,
    ) -> None:
        if not 0.1 <= interval_seconds <= 3600:
            raise ValueError("radar poll interval must be between 0.1 and 3600 seconds")
        self.event_buffer = event_buffer
        self.capture_provider = capture_provider
        self.scanner = scanner
        self.interval_seconds = interval_seconds
        self._published_signatures: OrderedDict[str, tuple[Any, ...]] = OrderedDict()

    async def run(self) -> None:
        while True:
            try:
                capture = await self.capture_provider.read_capture()
                if capture is not None:
                    for seed in self.scanner.scan(capture):
                        payload = _seed_payload(seed)
                        signature = (
                            payload.get("impressions"),
                            payload.get("replies"),
                            payload.get("views_per_minute"),
                            tuple(payload.get("simclusters", ())),
                        )
                        tweet_id = str(payload.get("tweet_id", ""))
                        if (
                            not tweet_id
                            or self._published_signatures.get(tweet_id) == signature
                        ):
                            continue
                        self._published_signatures[tweet_id] = signature
                        self._published_signatures.move_to_end(tweet_id)
                        while len(self._published_signatures) > DEFAULT_RING_CAPACITY * 2:
                            self._published_signatures.popitem(last=False)
                        self.event_buffer.publish(payload)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("radar seed producer failed to process a capture")
            await asyncio.sleep(self.interval_seconds)


def default_radar_producer(event_buffer: RadarEventBuffer) -> RadarSeedProducer:
    source_root = Path(__file__).resolve().parents[3] / "radar" / "src"
    if source_root.is_dir() and str(source_root) not in sys.path:
        sys.path.insert(0, str(source_root))
    try:
        from xreach_radar.seed_scout import SeedScout
    except ImportError as error:
        raise RuntimeError(
            "xreach-radar must be installed with the API service"
        ) from error
    interval = float(os.environ.get("XREACH_RADAR_POLL_SECONDS", "5"))
    return RadarSeedProducer(
        event_buffer,
        CaptureFileProvider(os.environ.get("XREACH_RADAR_CAPTURE_FILE")),
        SeedScout(),
        interval_seconds=interval,
    )


def _seed_payload(seed: Any) -> dict[str, Any]:
    if hasattr(seed, "__dataclass_fields__"):
        payload = asdict(seed)
    elif isinstance(seed, Mapping):
        payload = dict(seed)
    else:
        raise TypeError("seed scanner must return dataclasses or mappings")
    for key, value in tuple(payload.items()):
        if isinstance(value, datetime):
            payload[key] = value.astimezone(timezone.utc).isoformat()
    return payload


def encode_sse(event: RadarEvent) -> str:
    payload = json.dumps(
        {
            **event.payload,
            "event_id": event.event_id,
            "published_at": event.published_at.isoformat(),
        },
        separators=(",", ":"),
        allow_nan=False,
    )
    return f"id: {event.event_id}\nevent: seed\ndata: {payload}\n\n"
