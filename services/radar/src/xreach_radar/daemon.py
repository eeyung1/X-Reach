"""Standalone capture-to-API radar daemon for container deployments."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .cdp_client import ReaClient
from .seed_scout import SeedScout

logger = logging.getLogger(__name__)
MAX_CAPTURE_BYTES = 25 * 1024 * 1024
MAX_DEDUPLICATED_SEEDS = 1_000


class CaptureSource(Protocol):
    async def read_captures(self) -> Sequence[Mapping[str, Any]]: ...


class EventSink(Protocol):
    async def publish(self, events: Sequence[Mapping[str, Any]]) -> int: ...


class SeedScanner(Protocol):
    def scan(self, capture: Any) -> Sequence[Any]: ...


class CaptureFileSource:
    """Replay JSON/HAR/CDP evidence once, then only after file changes."""

    def __init__(self, paths: Sequence[str | Path]) -> None:
        if not paths:
            raise ValueError("at least one capture path is required")
        self.paths = tuple(Path(path).expanduser() for path in paths)
        self._signatures: dict[Path, tuple[int, int]] = {}

    async def read_captures(self) -> Sequence[Mapping[str, Any]]:
        return await asyncio.to_thread(self._read_changed)

    def _read_changed(self) -> Sequence[Mapping[str, Any]]:
        captures: list[Mapping[str, Any]] = []
        for path in self.paths:
            try:
                stat = path.stat()
            except FileNotFoundError:
                logger.warning("radar capture is not present: %s", path)
                continue
            signature = (stat.st_mtime_ns, stat.st_size)
            if self._signatures.get(path) == signature:
                continue
            if stat.st_size > MAX_CAPTURE_BYTES:
                raise ValueError(f"capture exceeds the 25 MB limit: {path}")
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, Mapping):
                raise TypeError(f"capture must contain a JSON object: {path}")
            self._signatures[path] = signature
            captures.append(payload)
        return tuple(captures)


class ReaScenarioSource:
    """Capture a live browser timeline through a caller-configured REA scenario."""

    def __init__(self, client: ReaClient, scenario_path: str | Path) -> None:
        self.client = client
        self.scenario_path = Path(scenario_path)

    async def read_captures(self) -> Sequence[Mapping[str, Any]]:
        capture = await asyncio.to_thread(
            self.client.capture_browser_scenario, self.scenario_path
        )
        return (capture,)


class CompositeCaptureSource:
    def __init__(self, sources: Sequence[CaptureSource]) -> None:
        if not sources:
            raise ValueError("configure a capture file or REA scenario")
        self.sources = tuple(sources)

    async def read_captures(self) -> Sequence[Mapping[str, Any]]:
        captures: list[Mapping[str, Any]] = []
        for source in self.sources:
            captures.extend(await source.read_captures())
        return tuple(captures)


class ApiEventSink:
    """Publish seed batches to the API's bounded in-memory SSE broker."""

    def __init__(
        self,
        api_url: str,
        *,
        token: str | None = None,
        timeout_seconds: float = 10.0,
    ) -> None:
        if not api_url.startswith(("http://", "https://")):
            raise ValueError("api_url must be an HTTP(S) URL")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.endpoint = f"{api_url.rstrip('/')}/api/v1/radar/events"
        self.token = token
        self.timeout_seconds = timeout_seconds

    async def publish(self, events: Sequence[Mapping[str, Any]]) -> int:
        if not events:
            return 0
        return await asyncio.to_thread(self._publish_sync, events)

    def _publish_sync(self, events: Sequence[Mapping[str, Any]]) -> int:
        body = json.dumps(
            {"events": [dict(event) for event in events]},
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = Request(self.endpoint, data=body, headers=headers, method="POST")
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")
            raise RuntimeError(
                f"radar ingestion failed with HTTP {error.code}: {detail}"
            ) from error
        except URLError as error:
            raise RuntimeError(
                f"radar ingestion is unavailable: {error.reason}"
            ) from error
        published = payload.get("published") if isinstance(payload, Mapping) else None
        if not isinstance(published, int) or published < 0:
            raise RuntimeError("radar ingestion returned an invalid response")
        return published


@dataclass(frozen=True)
class DaemonCycle:
    captures: int
    candidates: int
    published: int


class RadarDaemon:
    """Continuously scan captures and publish changed root-seed observations."""

    def __init__(
        self,
        source: CaptureSource,
        sink: EventSink,
        scanner: SeedScanner | None = None,
        *,
        poll_seconds: float = 5.0,
    ) -> None:
        if not 0.1 <= poll_seconds <= 3600:
            raise ValueError("poll_seconds must be between 0.1 and 3600")
        self.source = source
        self.sink = sink
        self.scanner = scanner or SeedScout()
        self.poll_seconds = poll_seconds
        self._published: OrderedDict[str, tuple[Any, ...]] = OrderedDict()
        self._pending: OrderedDict[str, tuple[tuple[Any, ...], Mapping[str, Any]]] = (
            OrderedDict()
        )

    async def run_once(self) -> DaemonCycle:
        captures = await self.source.read_captures()
        candidates = 0
        for capture in captures:
            for seed in self.scanner.scan(capture):
                candidates += 1
                payload = _seed_payload(seed)
                tweet_id = str(payload.get("tweet_id", "")).strip()
                if not tweet_id:
                    raise ValueError("seed payload must include tweet_id")
                signature = _seed_signature(payload)
                pending_signature = (
                    self._pending[tweet_id][0] if tweet_id in self._pending else None
                )
                if (
                    self._published.get(tweet_id) == signature
                    or pending_signature == signature
                ):
                    continue
                self._pending[tweet_id] = (signature, payload)
                self._pending.move_to_end(tweet_id)
        changed = [payload for _, payload in self._pending.values()]
        published = await self.sink.publish(changed)
        if published != len(changed):
            raise RuntimeError(
                f"radar ingestion accepted {published} of {len(changed)} events"
            )
        for tweet_id, (signature, _) in self._pending.items():
            self._published[tweet_id] = signature
            self._published.move_to_end(tweet_id)
        self._pending.clear()
        while len(self._published) > MAX_DEDUPLICATED_SEEDS:
            self._published.popitem(last=False)
        return DaemonCycle(len(captures), candidates, published)

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                cycle = await self.run_once()
                if cycle.captures or cycle.published:
                    logger.info(
                        "radar cycle captures=%d candidates=%d published=%d",
                        cycle.captures,
                        cycle.candidates,
                        cycle.published,
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("radar daemon cycle failed")
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.poll_seconds)
            except TimeoutError:
                pass


def daemon_from_environment(
    *,
    capture_paths: Sequence[str | Path] = (),
    scenario_path: str | Path | None = None,
    rea_command: Sequence[str] = ("rea",),
    api_url: str | None = None,
    api_token: str | None = None,
    poll_seconds: float | None = None,
) -> RadarDaemon:
    configured_paths = tuple(capture_paths)
    environment_capture = os.environ.get("XREACH_RADAR_CAPTURE_FILE")
    if environment_capture and not configured_paths:
        configured_paths = (environment_capture,)
    scenario = scenario_path or os.environ.get("XREACH_REA_SCENARIO_FILE")
    sources: list[CaptureSource] = []
    if configured_paths:
        sources.append(CaptureFileSource(configured_paths))
    if scenario:
        sources.append(ReaScenarioSource(ReaClient(rea_command), scenario))
    endpoint = api_url or os.environ.get("XREACH_API_URL", "http://api:8000")
    token = api_token or os.environ.get("XREACH_RADAR_INGEST_TOKEN")
    interval = (
        poll_seconds
        if poll_seconds is not None
        else float(os.environ.get("XREACH_RADAR_POLL_SECONDS", "5"))
    )
    return RadarDaemon(
        CompositeCaptureSource(sources),
        ApiEventSink(endpoint, token=token),
        poll_seconds=interval,
    )


async def run_daemon(daemon: RadarDaemon, *, once: bool = False) -> DaemonCycle | None:
    if once:
        return await daemon.run_once()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for name in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(name, stop.set)
        except NotImplementedError:
            pass
    await daemon.run(stop)
    return None


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
        elif isinstance(value, tuple):
            payload[key] = list(value)
    return payload


def _seed_signature(payload: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        payload.get("impressions"),
        payload.get("replies"),
        payload.get("views_per_minute"),
        tuple(payload.get("simclusters", ())),
        tuple(payload.get("semantic_id") or ()),
    )
