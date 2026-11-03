"""Alert rules and signed webhook delivery for radar opportunities."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

WebhookFormat = Literal["generic", "discord", "slack"]


@dataclass(frozen=True)
class AlertRule:
    """A named filter for high-velocity radar seeds."""

    rule_id: str
    min_views_per_minute: float = 0.0
    max_age_minutes: float = 30.0
    simclusters: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if not self.rule_id.strip():
            raise ValueError("rule_id must not be empty")
        if self.min_views_per_minute < 0:
            raise ValueError("min_views_per_minute must be non-negative")
        if self.max_age_minutes <= 0:
            raise ValueError("max_age_minutes must be positive")

    def matches(self, seed: Mapping[str, Any]) -> bool:
        velocity = _number(seed.get("views_per_minute"))
        age = _number(seed.get("age_minutes"))
        clusters = seed.get("simclusters", ())
        if not isinstance(clusters, (list, tuple, set, frozenset)):
            clusters = ()
        if velocity is None or age is None:
            return False
        return (
            velocity >= self.min_views_per_minute
            and 0 <= age < self.max_age_minutes
            and (
                not self.simclusters
                or bool(self.simclusters.intersection(map(str, clusters)))
            )
        )


@dataclass(frozen=True)
class AlertEvent:
    event_id: str
    rule_id: str
    seed: Mapping[str, Any]
    triggered_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "rule_id": self.rule_id,
            "triggered_at": self.triggered_at,
            "seed": dict(self.seed),
        }


@dataclass(frozen=True)
class WebhookTarget:
    """A webhook destination and its signing key."""

    url: str
    secret: str
    format: WebhookFormat = "generic"
    timeout_seconds: float = 5.0
    max_attempts: int = 3
    backoff_seconds: float = 0.25

    def __post_init__(self) -> None:
        if not self.url.startswith(("http://", "https://")):
            raise ValueError("webhook URL must use HTTP or HTTPS")
        if not self.secret:
            raise ValueError("webhook secret must not be empty")
        if self.format not in ("generic", "discord", "slack"):
            raise ValueError("unsupported webhook format")
        if (
            self.timeout_seconds <= 0
            or self.max_attempts < 1
            or self.backoff_seconds < 0
        ):
            raise ValueError("timeout, attempts, and backoff must be positive")


class AlertRuleEngine:
    """Evaluate radar seed mappings against configured alert rules."""

    def __init__(self, rules: Sequence[AlertRule]) -> None:
        if not rules:
            raise ValueError("at least one alert rule is required")
        if len({rule.rule_id for rule in rules}) != len(rules):
            raise ValueError("alert rule IDs must be unique")
        self.rules = tuple(rules)

    def evaluate(self, seed: Mapping[str, Any]) -> list[AlertEvent]:
        tweet_id = seed.get("tweet_id")
        if not isinstance(tweet_id, str) or not tweet_id.strip():
            return []
        return [
            AlertEvent(
                event_id=f"{rule.rule_id}:{tweet_id}",
                rule_id=rule.rule_id,
                seed=dict(seed),
            )
            for rule in self.rules
            if rule.matches(seed)
        ]


class WebhookDispatcher:
    """Deliver alert events with bounded retries and an HMAC-SHA256 signature."""

    def __init__(self, targets: Sequence[WebhookTarget]) -> None:
        self.targets = tuple(targets)

    async def dispatch(self, event: AlertEvent) -> list[bool]:
        return list(
            await asyncio.gather(
                *(self._deliver(target, event) for target in self.targets)
            )
        )

    async def _deliver(self, target: WebhookTarget, event: AlertEvent) -> bool:
        body = json.dumps(
            format_payload(target.format, event),
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        signature = hmac.new(target.secret.encode(), body, hashlib.sha256).hexdigest()
        for attempt in range(target.max_attempts):
            try:
                await asyncio.to_thread(_post, target, body, signature)
                return True
            except (HTTPError, URLError, TimeoutError, OSError) as error:
                if isinstance(error, HTTPError) and 400 <= error.code < 500:
                    return False
                if attempt + 1 == target.max_attempts:
                    return False
                delay = target.backoff_seconds * (2**attempt)
                if delay:
                    await asyncio.sleep(delay + random.uniform(0, delay * 0.1))
        return False


def format_payload(format: WebhookFormat, event: AlertEvent) -> dict[str, Any]:
    """Format one alert for generic JSON, Discord, or Slack incoming hooks."""

    data = event.to_dict()
    if format == "generic":
        return data
    seed = event.seed
    text = str(seed.get("text") or f"Radar opportunity {seed.get('tweet_id', '')}")
    details = (
        f"{text}\n{seed.get('views_per_minute', 0)} views/min · "
        f"{seed.get('age_minutes', 0)} minutes old"
    )
    if format == "discord":
        return {
            "content": details,
            "embeds": [
                {
                    "title": "X-Reach radar alert",
                    "description": details,
                    "url": _tweet_url(seed),
                }
            ],
        }
    if format == "slack":
        return {
            "text": "X-Reach radar alert",
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            f"*X-Reach radar alert*\n<{_tweet_url(seed)}|{text}>\n"
                            f"{details}"
                        ),
                    },
                }
            ],
        }
    raise ValueError(f"unsupported webhook format: {format}")


def _post(target: WebhookTarget, body: bytes, signature: str) -> None:
    request = Request(
        target.url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-XReach-Signature": f"sha256={signature}",
            "X-XReach-Event": "radar.alert",
        },
        method="POST",
    )
    with urlopen(request, timeout=target.timeout_seconds) as response:
        status = getattr(response, "status", 200)
        if not 200 <= status < 300:
            raise URLError(f"webhook returned HTTP {status}")


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _tweet_url(seed: Mapping[str, Any]) -> str:
    tweet_id = str(seed.get("tweet_id", ""))
    screen_name = seed.get("screen_name")
    author = str(screen_name) if screen_name else "i"
    return f"https://x.com/{author}/status/{tweet_id}"
